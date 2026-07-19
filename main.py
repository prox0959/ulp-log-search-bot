"""
Telegram -> Discord Bridge Bot
--------------------------------
Discord'da /search <kelime> yazınca, Telethon (senin Telegram hesabın) üzerinden
hedef Telegram botuna /search <kelime> gönderir, gelen inline butonlu seçenekleri
Discord'a taşır, kullanıcı seçim yapınca Telegram tarafında butona tıklar,
gelen .txt/.zip dosyasını indirip Discord'a yükler.

KOMUTLAR
--------
/search <site>  -> arama yapar (1 hak harcar, hakkı yoksa yapamaz)
/deneme         -> tek seferlik 3 hak verir
/hakkım         -> kalan hakkını gösterir
/help           -> kullanıcı komutlarını gösterir
/admin          -> sadece ADMIN_ID; hak ekle/al, kullanıcıları listele (butonlu panel)

Haklar rights.json dosyasında kalıcı tutulur.

KURULUM
-------
pip install telethon discord.py aiohttp

1) https://my.telegram.org adresine git, giriş yap, "API development tools" kısmından
   api_id ve api_hash al.
2) Discord Developer Portal'dan bot oluştur, token al, "Message Content Intent"i aç.
3) Aşağıdaki CONFIG kısmını doldur.
4) İlk çalıştırmada Telethon senden telefon no + Telegram doğrulama kodu isteyecek
   (tek seferlik, sonra session dosyası kalıcı oluyor).

python main.py
"""

import asyncio
import io
import re
import json
import os
import sys
import string
import secrets
import sqlite3
import contextlib

# Windows konsolu (cp1254 vb.) emoji'li print'lerde çökebilir; UTF-8'e sabitle
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from telethon import TelegramClient, events

DISCORD_FILE_LIMIT = 10 * 1024 * 1024  # 10 MB, Discord ücretsiz limiti (2026 itibarıyla)

# Basit domain/URL kontrolü: "vk.com", "https://vk.com", "www.example.com" gibi formatları kabul eder
DOMAIN_PATTERN = re.compile(
    r"^(https?://)?([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(/.*)?$"
)

# ------------------- CONFIG -------------------
TELEGRAM_API_ID = 0                                     # my.telegram.org'dan al (örn: 123456)
TELEGRAM_API_HASH = ""                                  # my.telegram.org'dan al (örn: "abcdef123456...")
TELEGRAM_BOT_USERNAME = "@dead_handbot"                 # köprü kurulacak Telegram botunun kullanıcı adı
DISCORD_BOT_TOKEN = ""                                  # Discord bot tokenini girin
TELETHON_SESSION_NAME = "bridge_session"                # session dosyası adı, sabit kalsın

ADMIN_ID = 0                                            # admin paneline erişebilecek tek Discord ID
DENEME_AMOUNT = 3               # /deneme ile verilecek hak sayısı (tek seferlik)
SEARCH_COST = 1                # her başarılı /search kaç hak harcar
RIGHTS_FILE = "rights.json"    # eski JSON dosyası (varsa DB'ye taşınır, sonra kullanılmaz)
DB_FILE = "database.db"        # hakların tutulduğu kalıcı SQLite veritabanı

# Kullanıcı komutlarının çalışabileceği kanal (DM'de her yerde çalışır)
ALLOWED_CHANNEL_ID = 0
# Kullanıcıların aradığı sitelerin loglanacağı Discord webhook'u
SEARCH_LOG_WEBHOOK = ""

# ------------------- REFERANS SİSTEMİ AYARLARI -------------------
# /refgir yapan (davet edilen) kişinin ÜYE olması gereken sunucu.
REFERRAL_GUILD_ID = 0
REF_CODE_LENGTH = 8                  # otomatik üretilen referans kodunun karakter sayısı
REF_JOIN_REWARD = 1                  # bir referans kodunu KULLANANA verilecek hak
# Kullanım sayısı -> o eşiğe ULAŞINCA sahibe verilecek EKSTRA hak (tek seferlik).
# NOT: taban +1 hak YOK; sadece bu milestone'larda hak verilir. Her kullanım
# yalnızca "kullanım sayısı"nı (haneyi) +1 artırır.
REF_MILESTONES = {3: 1, 5: 3}
# Referanslı kullanıcı bu miktar ve ÜZERİ hak satın alırsa, referansçıya
# alınanın 1/4'ü kadar hak verilir (admin onayına bağlı).
PURCHASE_SHARE_MIN = 25
# ------------------------------------------------

# ------------------- FİYATLANDIRMA (/fiyat) -------------------
PRICE_PER_RIGHT_TL = 5        # 1 arama hakkı fiyatı (TL)
MIN_RIGHTS_PURCHASE = 50      # minimum hak alımı (adet)
PRICE_PER_API_REQ_TL = 7      # 1 API isteği (req) fiyatı (TL)
MIN_API_PURCHASE = 125        # minimum API alımı (req)
# ------------------------------------------------

tg_client = TelegramClient(TELETHON_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)

intents = discord.Intents.default()
intents.message_content = True
# Komut öneki: "/", "!" ve bot'u @etiketleme birlikte çalışır.
# ("/" tek başına Discord'un slash-komut menüsüyle çakışıp mesajın bota
#  ulaşmamasına yol açabildiği için "!" de ekliyoruz.)
discord_bot = commands.Bot(
    command_prefix=commands.when_mentioned_or("/", "!"),
    intents=intents,
    help_command=None,
)

# Aynı anda tek arama işlemi varsayımıyla basit bir kilit/kuyruk
search_lock = asyncio.Lock()
# Hak dosyasına eşzamanlı yazmayı engelleyen kilit
data_lock = asyncio.Lock()


# ------------------- HAK (kredi) SİSTEMİ (SQLite) -------------------
# Veriler database.db içinde iki tabloda tutulur:
#   rights(user_id TEXT PK, amount INTEGER)  -> kalan haklar
#   deneme_used(user_id TEXT PK)             -> /deneme'yi kullanmış id'ler
# SQLite atomik commit yaptığı için ani kapanmada JSON gibi bozulmaz.

def _connect() -> sqlite3.Connection:
    return sqlite3.connect(DB_FILE)


def _db_init() -> None:
    """Tabloları oluşturur ve (varsa) eski rights.json verisini bir kereye
    mahsus DB'ye taşır. Program başında bir kez çağrılır."""
    conn = _connect()
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS rights ("
            "user_id TEXT PRIMARY KEY, amount INTEGER NOT NULL)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS deneme_used (user_id TEXT PRIMARY KEY)"
        )
        # Referans sistemi:
        #   referral_codes: her kullanıcının kendi kodu + kaç kez kullanıldığı +
        #                   en son ödüllendirilen milestone eşiği (tekrar ödül vermemek için).
        #   referral_uses : bir kodu KULLANMIŞ kişiler (PK sayesinde herkes tek sefer girebilir),
        #                   ve kimin kodunu girdiği (referrer_id).
        conn.execute(
            "CREATE TABLE IF NOT EXISTS referral_codes ("
            "user_id TEXT PRIMARY KEY, code TEXT UNIQUE NOT NULL, "
            "used_count INTEGER NOT NULL DEFAULT 0, "
            "milestone_hw INTEGER NOT NULL DEFAULT 0)"
        )
        conn.execute(
            "CREATE TABLE IF NOT EXISTS referral_uses ("
            "user_id TEXT PRIMARY KEY, referrer_id TEXT NOT NULL)"
        )
        conn.commit()
        _migrate_from_json(conn)
    finally:
        conn.close()


def _migrate_from_json(conn: sqlite3.Connection) -> None:
    """Eski rights.json varsa ve DB henüz boşsa verileri taşır."""
    if not os.path.exists(RIGHTS_FILE):
        return
    already = conn.execute("SELECT COUNT(*) FROM rights").fetchone()[0]
    already += conn.execute("SELECT COUNT(*) FROM deneme_used").fetchone()[0]
    if already:
        return  # DB'de zaten veri var, üzerine yazma
    try:
        with open(RIGHTS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (json.JSONDecodeError, OSError):
        return
    for uid, amount in (data.get("rights") or {}).items():
        if amount:
            conn.execute(
                "INSERT OR REPLACE INTO rights (user_id, amount) VALUES (?, ?)",
                (str(uid), int(amount)),
            )
    for uid in (data.get("deneme_used") or []):
        conn.execute(
            "INSERT OR IGNORE INTO deneme_used (user_id) VALUES (?)", (str(uid),)
        )
    conn.commit()
    print(f"ℹ️ {RIGHTS_FILE} verileri {DB_FILE} veritabanına taşındı.")


def get_rights(user_id) -> int:
    """Kullanıcının kalan hakkını döner."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT amount FROM rights WHERE user_id = ?", (str(user_id),)
        ).fetchone()
        return row[0] if row else 0
    finally:
        conn.close()


def modify_rights(user_id, delta: int) -> int:
    """Hakkı delta kadar değiştirir (negatif = düş). 0'ın altına inmez.
    Yeni bakiyeyi döner."""
    uid = str(user_id)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT amount FROM rights WHERE user_id = ?", (uid,)
        ).fetchone()
        new = max(0, (row[0] if row else 0) + delta)
        if new == 0:
            conn.execute("DELETE FROM rights WHERE user_id = ?", (uid,))
        else:
            conn.execute(
                "INSERT INTO rights (user_id, amount) VALUES (?, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET amount = excluded.amount",
                (uid, new),
            )
        conn.commit()
        return new
    finally:
        conn.close()


def claim_deneme(user_id):
    """/deneme: tek seferlik DENEME_AMOUNT hak verir.
    (yeni_bakiye, True) döner; zaten kullanılmışsa (mevcut_bakiye, False)."""
    uid = str(user_id)
    conn = _connect()
    try:
        used = conn.execute(
            "SELECT 1 FROM deneme_used WHERE user_id = ?", (uid,)
        ).fetchone()
        cur = conn.execute(
            "SELECT amount FROM rights WHERE user_id = ?", (uid,)
        ).fetchone()
        current = cur[0] if cur else 0
        if used:
            return current, False
        conn.execute("INSERT INTO deneme_used (user_id) VALUES (?)", (uid,))
        new = current + DENEME_AMOUNT
        conn.execute(
            "INSERT INTO rights (user_id, amount) VALUES (?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET amount = excluded.amount",
            (uid, new),
        )
        conn.commit()
        return new, True
    finally:
        conn.close()


def all_rights() -> dict:
    """Hak verilmiş tüm kullanıcıları {user_id: amount} olarak döner (admin listesi)."""
    conn = _connect()
    try:
        rows = conn.execute(
            "SELECT user_id, amount FROM rights ORDER BY amount DESC"
        ).fetchall()
        return {uid: amount for uid, amount in rows}
    finally:
        conn.close()


# ------------------- REFERANS SİSTEMİ (SQLite) -------------------

def _gen_ref_code() -> str:
    """Karışık harf+rakamdan rastgele, okunması kolay bir referans kodu üretir
    (karıştıran 0/O, 1/I/L gibi karakterler çıkarıldı)."""
    alphabet = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(REF_CODE_LENGTH))


def get_or_create_ref_code(user_id) -> str:
    """Kullanıcının referans kodunu döner; yoksa bu ilk çağrıda rastgele üretip
    kaydeder (her kullanıcıya default olarak kod verilmesini sağlar)."""
    uid = str(user_id)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT code FROM referral_codes WHERE user_id = ?", (uid,)
        ).fetchone()
        if row:
            return row[0]
        # Çakışma ihtimaline karşı benzersiz kod bulana dek dene
        for _ in range(20):
            code = _gen_ref_code()
            try:
                conn.execute(
                    "INSERT INTO referral_codes (user_id, code) VALUES (?, ?)",
                    (uid, code),
                )
                conn.commit()
                return code
            except sqlite3.IntegrityError:
                continue  # kod (nadiren) çakıştı, yeniden üret
        raise RuntimeError("Benzersiz referans kodu üretilemedi")
    finally:
        conn.close()


def get_ref_stats(user_id):
    """Kullanıcının (kod, kullanım_sayısı) bilgisini döner. Kod yoksa üretir."""
    code = get_or_create_ref_code(user_id)
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT used_count FROM referral_codes WHERE user_id = ?", (str(user_id),)
        ).fetchone()
        return code, (row[0] if row else 0)
    finally:
        conn.close()


def get_referrer(user_id):
    """Bu kullanıcıyı kim davet etmiş (kimin kodunu girmiş)? user_id (str) veya None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT referrer_id FROM referral_uses WHERE user_id = ?", (str(user_id),)
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def redeem_referral(user_id, code: str) -> dict:
    """/refgir mantığı. Sonucu bir sözlük olarak döner:
      {"ok": False, "reason": "already_used" | "bad_code" | "self"}
      {"ok": True, "owner_id": str, "new_count": int, "bonus": int, "join_reward": int}
    Kurallar:
      - Her kullanıcı YALNIZCA bir kez referans girebilir (referral_uses PK).
      - Kodu KULLANAN kişiye REF_JOIN_REWARD kadar hak verilir.
      - Kod sahibinin "kullanım sayısı" (hane) +1 artar.
      - Sahibe taban hak yoktur; sadece REF_MILESTONES eşiklerine ULAŞINCA
        o eşiğe ait ekstra hak (tek seferlik) verilir.
    """
    uid = str(user_id)
    code = (code or "").strip().upper()
    conn = _connect()
    try:
        if conn.execute(
            "SELECT 1 FROM referral_uses WHERE user_id = ?", (uid,)
        ).fetchone():
            return {"ok": False, "reason": "already_used"}

        row = conn.execute(
            "SELECT user_id, used_count, milestone_hw FROM referral_codes WHERE code = ?",
            (code,),
        ).fetchone()
        if not row:
            return {"ok": False, "reason": "bad_code"}
        owner_id, used_count, milestone_hw = row
        if owner_id == uid:
            return {"ok": False, "reason": "self"}

        # Kullanımı kaydet (bu kişi artık tekrar giremez) ve sayacı artır.
        conn.execute(
            "INSERT INTO referral_uses (user_id, referrer_id) VALUES (?, ?)",
            (uid, owner_id),
        )
        new_count = used_count + 1

        # Yeni ulaşılan milestone'lar için ekstra hak topla (tekrar vermemek için
        # milestone_hw high-water mark'ının üstündekileri say).
        bonus = 0
        new_hw = milestone_hw
        for threshold in sorted(REF_MILESTONES):
            if milestone_hw < threshold <= new_count:
                bonus += REF_MILESTONES[threshold]
                new_hw = threshold
        conn.execute(
            "UPDATE referral_codes SET used_count = ?, milestone_hw = ? WHERE user_id = ?",
            (new_count, new_hw, owner_id),
        )
        conn.commit()
    finally:
        conn.close()

    # Ödülleri ayrı bağlantı kullanan modify_rights ile uygula (commit sonrası güvenli).
    # Kodu kullanana taban hak:
    if REF_JOIN_REWARD:
        modify_rights(uid, REF_JOIN_REWARD)
    # Kod sahibine milestone ödülü (varsa):
    if bonus:
        modify_rights(owner_id, bonus)
    return {
        "ok": True,
        "owner_id": owner_id,
        "new_count": new_count,
        "bonus": bonus,
        "join_reward": REF_JOIN_REWARD,
    }


# Tabloları hazırla + eski JSON'u (varsa) taşı
_db_init()


# ------------------- KANAL KISITI + LOG -------------------
def in_allowed_channel(ctx: commands.Context) -> bool:
    """Kullanıcı komutları sadece izinli kanalda veya DM'de çalışsın."""
    if ctx.guild is None:  # DM
        return True
    return ctx.channel.id == ALLOWED_CHANNEL_ID


async def log_search(user, query: str, where: str) -> None:
    """Yapılan aramayı webhook'a gönderir. Hata olsa da botu düşürmez."""
    payload = {
        "username": "Arama Log",
        "embeds": [
            {
                "title": "🔎 Yeni Arama",
                "color": 3447003,
                "fields": [
                    {"name": "Kullanıcı", "value": f"{user} (`{user.id}`)", "inline": False},
                    {"name": "Aranan site", "value": query, "inline": False},
                    {"name": "Nereden", "value": where, "inline": False},
                ],
            }
        ],
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(SEARCH_LOG_WEBHOOK, json=payload) as resp:
                if resp.status >= 400:
                    print(f"webhook log başarısız: HTTP {resp.status}")
    except Exception as e:
        print(f"webhook log hatası: {e}")


@discord_bot.tree.command(name="search", description="Bir site için arama yapar (1 hak harcar).")
@app_commands.describe(site="Aranacak site adresi, örn: vk.com")
async def search(interaction: discord.Interaction, site: str):
    query = site.strip()

    is_admin = interaction.user.id == ADMIN_ID

    # Hak kontrolü (admin sınırsız)
    if not is_admin and get_rights(interaction.user.id) < SEARCH_COST:
        await interaction.response.send_message(
            "❌ Arama hakkın kalmadı.\n"
            "`/deneme` ile 3 hak alabilir ya da adminden hak isteyebilirsin.\n"
            "Kalan hakkını `/hakkim` ile görebilirsin."
        )
        return

    if not DOMAIN_PATTERN.match(query):
        await interaction.response.send_message(
            "❌ Geçersiz format. Bir site adresi gir, örnek:\n"
            "`/search vk.com` veya `/search https://vk.com`"
        )
        return

    # Uzun sürebilir; önce "düşünüyor" durumuna geç ki 3 sn'lik yanıt limitine takılmayalım
    await interaction.response.defer(thinking=True)

    # Aramayı webhook'a logla (nerede yapıldığını da yaz)
    where = "DM" if interaction.guild is None else f"#{interaction.channel} ({interaction.guild})"
    await log_search(interaction.user, query, where)

    channel = interaction.channel

    async with search_lock:
        await interaction.edit_original_response(content=f"🔎 Aranıyor: **{query}** ...")

        try:
            # 1) Handler'ı ÖNCE kur, sonra komutu gönder (yarış durumunu önler:
            #    bot çok hızlı cevap verirse mesaj kaçmasın)
            async with telegram_waiter(with_buttons=True, timeout=20) as waiter:
                await tg_client.send_message(TELEGRAM_BOT_USERNAME, f"/search {query}")
                response = await waiter
        except asyncio.TimeoutError:
            await interaction.edit_original_response(
                content="❌ Telegram botundan cevap gelmedi (zaman aşımı)."
            )
            return

        # Buraya geldiysek arama başarılı sayılır -> 1 hak düş (admin hariç)
        if not is_admin:
            async with data_lock:
                remaining = modify_rights(interaction.user.id, -SEARCH_COST)
            await interaction.followup.send(f"🎫 1 hak kullanıldı. Kalan hakkın: **{remaining}**")

        buttons = response.buttons  # [[Button, Button], ...] şeklinde 2D liste
        if not buttons:
            # Buton yoksa muhtemelen direkt dosya gönderdi, onu yakala
            await forward_result_to_dm(interaction.user, channel, response)
            return

        # 3) Buton etiketlerini Discord'da numaralandırarak göster
        flat_buttons = [b for row in buttons for b in row]
        options_text = "\n".join(f"{i+1}️⃣ {b.text}" for i, b in enumerate(flat_buttons))

        # Botun "Free/Premium" satırlarını gizle, sadece bulunan adedi göster
        count = extract_found_count(response.text)
        header = f"✅ {count} adet bulundu\n\n" if count else ""

        await interaction.edit_original_response(
            content=f"{header}Seçenekler:\n{options_text}\n\nCevap olarak sayı yaz (örn: 1)"
        )

        def check(m):
            return (
                m.author == interaction.user
                and m.channel == channel
                and m.content.isdigit()
            )

        try:
            choice_msg = await discord_bot.wait_for("message", check=check, timeout=30)
            idx = int(choice_msg.content) - 1
            if idx < 0 or idx >= len(flat_buttons):
                await interaction.followup.send("❌ Geçersiz seçim.")
                return
        except asyncio.TimeoutError:
            await interaction.followup.send("❌ Zaman aşımı, seçim yapılmadı.")
            return

        # Seçim alındı; dosya hazırlanırken kullanıcıyı bekletme uyarısıyla bilgilendir.
        await interaction.followup.send("⏳ Lütfen bekleyin, bu işlem biraz zaman alabilir...")

        # 4) Dosyayı bekleyecek handler'ı tıklamadan ÖNCE kur, sonra butona tıkla.
        #    Tıklama metin yerine index ile: aynı metinli iki buton varsa şaşmaz.
        try:
            async with telegram_waiter(with_file=True, timeout=30) as waiter:
                await response.click(idx)
                file_msg = await waiter
        except asyncio.TimeoutError:
            await interaction.followup.send("❌ Dosya gelmedi (zaman aşımı).")
            return

        await forward_result_to_dm(interaction.user, channel, file_msg)


# ------------------- KULLANICI KOMUTLARI -------------------
@discord_bot.tree.command(name="deneme", description="Tek seferlik 3 deneme hakkı verir.")
async def deneme(interaction: discord.Interaction):
    """Tek seferlik 3 deneme hakkı verir."""
    async with data_lock:
        remaining, ok = claim_deneme(interaction.user.id)
    if ok:
        await interaction.response.send_message(
            f"✅ {DENEME_AMOUNT} deneme hakkı hesabına eklendi! "
            f"Kalan hakkın: **{remaining}**"
        )
    else:
        await interaction.response.send_message(
            f"❌ Deneme hakkını zaten kullandın. Kalan hakkın: **{remaining}**"
        )


@discord_bot.tree.command(name="hakkim", description="Kalan arama hakkını gösterir.")
async def hakkim(interaction: discord.Interaction):
    """Kullanıcının kalan hakkını gösterir."""
    r = get_rights(interaction.user.id)
    await interaction.response.send_message(
        f"🎫 {interaction.user.mention} kalan arama hakkın: **{r}**"
    )


@discord_bot.tree.command(name="referans", description="Kendi referans kodunu ve istatistiklerini gösterir.")
async def referans(interaction: discord.Interaction):
    """Kullanıcıya kendi referans kodunu ve kaç kişinin kullandığını gösterir.
    İlk kez çağrıldığında otomatik olarak rastgele bir kod atanır."""
    code, count = get_ref_stats(interaction.user.id)

    # Bir sonraki milestone'a ne kaldığını göster
    next_ms = None
    for threshold in sorted(REF_MILESTONES):
        if count < threshold:
            next_ms = threshold
            break

    embed = discord.Embed(
        title="🔗 Referans Bilgin",
        color=discord.Color.green(),
    )
    embed.add_field(name="Referans kodun", value=f"`{code}`", inline=False)
    embed.add_field(name="Kullanım sayısı", value=f"**{count}**", inline=True)
    milestones_txt = " • ".join(
        f"{t} kullanım → +{r} hak" for t, r in sorted(REF_MILESTONES.items())
    )
    embed.add_field(
        name="Ödül eşikleri",
        value=milestones_txt + "\n(Bu ödüller yalnızca **tek seferliktir**.)",
        inline=False,
    )
    if next_ms is not None:
        embed.add_field(
            name="Sonraki ödül",
            value=f"{next_ms - count} kullanım daha → +{REF_MILESTONES[next_ms]} hak",
            inline=False,
        )
    else:
        embed.add_field(
            name="Daha fazla ödül",
            value="Tüm eşik ödüllerini aldın. **Daha üst davet ödülleri için admin ile iletişime geç.**",
            inline=False,
        )
    embed.set_footer(
        text="Arkadaşların /refgir <kod> yazınca sayacın artar. Üst ödüller için admin ile iletişime geçebilirsiniz."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@discord_bot.tree.command(name="refgir", description="Bir referans kodu girersin (tek sefer).")
@app_commands.describe(kod="Girmek istediğin referans kodu")
async def refgir(interaction: discord.Interaction, kod: str):
    """Bir referans kodunu kullanır. Kurallar:
      - Kişi bu sunucunun (REFERRAL_GUILD_ID) üyesi olmalı.
      - Herkes yalnızca bir kez, bir kod girebilir.
      - Kodu giren kişiye bir şey verilmez; kod SAHİBİNİN hanesi +1 artar ve
        milestone'lara ulaşılırsa sahibe ekstra hak yazılır."""
    # 1) Zorunlu sunucu üyeliği kontrolü
    guild = discord_bot.get_guild(REFERRAL_GUILD_ID)
    if guild is None:
        await interaction.response.send_message(
            "⚠️ Referans doğrulama sunucusuna ulaşılamadı. Lütfen sonra tekrar dene.",
            ephemeral=True,
        )
        return
    member = guild.get_member(interaction.user.id)
    if member is None:
        try:
            member = await guild.fetch_member(interaction.user.id)
        except discord.NotFound:
            member = None
        except discord.HTTPException:
            member = None
    if member is None:
        await interaction.response.send_message(
            "❌ Referans kodu girebilmek için önce gerekli sunucuya katılmalısın.",
            ephemeral=True,
        )
        return

    # 2) Kodu kullan (yazma işlemi; hak sistemiyle tutarlı olsun diye kilit altında)
    async with data_lock:
        result = redeem_referral(interaction.user.id, kod)
    if not result["ok"]:
        reasons = {
            "already_used": "❌ Zaten bir referans kodu kullanmışsın. Yalnızca bir kez girebilirsin.",
            "bad_code": "❌ Böyle bir referans kodu yok. Kodu kontrol et.",
            "self": "❌ Kendi referans kodunu giremezsin.",
        }
        await interaction.response.send_message(
            reasons.get(result["reason"], "❌ Referans kodu kullanılamadı."),
            ephemeral=True,
        )
        return

    owner_id = result["owner_id"]
    join_reward = result.get("join_reward", 0)
    my_balance = get_rights(interaction.user.id)
    reward_line = (
        f"\n🎫 Sana **+{join_reward} hak** eklendi. Kalan hakkın: **{my_balance}**"
        if join_reward else ""
    )
    await interaction.response.send_message(
        f"✅ Referans kodu kabul edildi! <@{owner_id}> kişisinin referansı sayıldı."
        + reward_line,
        ephemeral=True,
    )

    # 3) Kod sahibine milestone ödülü çıktıysa DM ile haber ver (best-effort)
    if result["bonus"]:
        try:
            owner = discord_bot.get_user(int(owner_id)) or await discord_bot.fetch_user(int(owner_id))
            if owner:
                await owner.send(
                    f"🎉 Referansın {result['new_count']} kullanıma ulaştı ve "
                    f"**+{result['bonus']} hak** kazandın! `/hakkim` ile kontrol edebilirsin."
                )
        except Exception:
            pass


@discord_bot.tree.command(name="fiyat", description="Hak ve API satış fiyatlarını gösterir.")
async def fiyat(interaction: discord.Interaction):
    """Hakların ve API'nin fiyatlarını tek bir panelde gösterir."""
    embed = discord.Embed(
        title="💰 Fiyat Listesi",
        description="Satın alım için ticket açabilirsiniz.",
        color=discord.Color.gold(),
    )
    embed.add_field(
        name="🎫 Arama Hakkı",
        value=(
            f"• **1 hak = {PRICE_PER_RIGHT_TL} TL**\n"
            f"• Minimum alım: **{MIN_RIGHTS_PURCHASE} hak** "
            f"(= {MIN_RIGHTS_PURCHASE * PRICE_PER_RIGHT_TL} TL)\n"
            "• Toplu alımlarda **indirim** uygulanır."
        ),
        inline=False,
    )
    embed.add_field(
        name="🔌 API Satışı",
        value=(
            f"• **1 istek (req) = {PRICE_PER_API_REQ_TL} TL**\n"
            f"• Minimum alım: **{MIN_API_PURCHASE} req** "
            f"(= {MIN_API_PURCHASE * PRICE_PER_API_REQ_TL} TL)\n"
            "• Toplu alımlarda **indirim** uygulanır."
        ),
        inline=False,
    )
    embed.set_footer(
        text="Fiyatlar ve toplu alım indirimleri için satın alım ticket'ı açabilirsiniz."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


@discord_bot.tree.command(name="help", description="Komutları gösterir.")
async def help_cmd(interaction: discord.Interaction):
    """Tüm komutları ve sistemin nasıl çalıştığını detaylıca anlatır."""
    milestones_txt = ", ".join(
        f"**{t}** kullanımda **+{r} hak**" for t, r in sorted(REF_MILESTONES.items())
    )

    embed = discord.Embed(
        title="📖 Yardım — Tüm Komutlar ve Sistem",
        description=(
            "Aramalar **hak** harcar; hakları `/deneme`, **referans sistemi** "
            "veya satın alım ile alabilirsin. Aşağıda her şey detaylıca anlatıldı."
        ),
        color=discord.Color.blurple(),
    )

    # --- Arama ---
    embed.add_field(
        name="🔎 /search <site>",
        value=(
            "Verilen site için arama yapar ve **1 hak** harcar.\n"
            "• Örnek: `/search vk.com` veya `/search https://vk.com`\n"
            "• Sonuç dosyası (**.zip / .txt**) **DM'ine** gönderilir; kullandığın "
            "kanala ise herkesin göreceği \"DM'ine iletildi\" bildirimi düşer.\n"
            "• DM'in kapalıysa sonuç kanala gönderilir. (Almak için sunucu "
            "üyelerinden DM açık olmalı.)\n"
            "• Hakkın yoksa arama yapılamaz."
        ),
        inline=False,
    )

    # --- Hak alma ---
    embed.add_field(
        name="🎁 /deneme",
        value=f"Tek seferlik **{DENEME_AMOUNT} deneme hakkı** verir. Her hesap yalnızca bir kez kullanabilir.",
        inline=False,
    )
    embed.add_field(
        name="🎫 /hakkim",
        value="Kalan arama hakkını gösterir.",
        inline=False,
    )

    # --- Referans ---
    embed.add_field(
        name="🔗 /referans",
        value=(
            "Kendi **referans kodunu** ve istatistiklerini gösterir.\n"
            "• Her kullanıcıya **ilk kullanımda otomatik, rastgele** bir kod atanır.\n"
            "• Bu kodu arkadaşlarınla paylaşırsın; onlar `/refgir` ile girer."
        ),
        inline=False,
    )
    embed.add_field(
        name="🎯 /refgir <kod>",
        value=(
            "Bir referans kodunu kullanırsın. **Tek sefer** girebilirsin, sonradan değiştirilemez.\n"
            "• Kodu girebilmek için önce **gerekli sunucuya katılmış** olman gerekir.\n"
            f"• Kodu **girene {REF_JOIN_REWARD} hak** eklenir; ayrıca **kod sahibinin kullanım sayısı +1** artar.\n"
            f"• Kod sahibinin ödülleri: {milestones_txt}. Bu ödüller **yalnızca tek seferliktir**; "
            "**daha üst davet ödülleri için admin ile iletişime geçmeniz gerekir.**\n"
            f"• **Alım payı:** Senin davet ettiğin biri **{PURCHASE_SHARE_MIN}+ hak** satın alırsa, "
            "sana o alımın **1/4'ü** kadar hak yazılır (admin onayına bağlıdır)."
        ),
        inline=False,
    )

    # --- Örnek akış ---
    embed.add_field(
        name="🧭 Referans nasıl işler? (örnek)",
        value=(
            "1) `/referans` yaz → kodunu al (örn. `ABCD2345`).\n"
            "2) Arkadaşların sunucuya girip `/refgir ABCD2345` yazsın.\n"
            "3) Her giren için **kullanım sayın +1** olur.\n"
            f"4) Sayı {min(REF_MILESTONES)}'e ulaşınca +{REF_MILESTONES[min(REF_MILESTONES)]} hak, "
            f"{max(REF_MILESTONES)}'e ulaşınca +{REF_MILESTONES[max(REF_MILESTONES)]} hak alırsın.\n"
            f"5) Davetlin {PURCHASE_SHARE_MIN}+ hak alırsa 1/4'ü sana yazılır."
        ),
        inline=False,
    )

    embed.add_field(
        name="💰 /fiyat",
        value=(
            f"Satış fiyatlarını gösterir. Hak: **{PRICE_PER_RIGHT_TL} TL/adet** "
            f"(min {MIN_RIGHTS_PURCHASE}), API: **{PRICE_PER_API_REQ_TL} TL/req** "
            f"(min {MIN_API_PURCHASE}). Toplu alımda indirim."
        ),
        inline=False,
    )
    embed.add_field(
        name="ℹ️ /help",
        value="Bu menüyü gösterir.",
        inline=False,
    )

    embed.set_footer(
        text="Herhangi başka bir sorunda admin ile iletişime geçebilirsiniz."
    )
    await interaction.response.send_message(embed=embed, ephemeral=True)


# ------------------- ADMIN PANEL -------------------
@discord_bot.tree.command(name="admin", description="Admin paneli (sadece admin).")
@app_commands.guild_only()
@app_commands.default_permissions(administrator=True)
async def admin_cmd(interaction: discord.Interaction):
    """Admin panelini açar (sadece ADMIN_ID).

    default_permissions(administrator=True): komut, "Yönetici" izni olmayan
    üyelerin slash menüsünde HİÇ görünmez. Aşağıdaki ADMIN_ID kontrolü ise
    gerçek güvenlik kilidi (biri yine de çalıştırmayı denerse engeller)."""
    if interaction.user.id != ADMIN_ID:
        await interaction.response.send_message(
            "⛔ Bu komutu kullanma yetkin yok.", ephemeral=True
        )
        return

    embed = discord.Embed(
        title="🛠️ Admin Panel",
        description=(
            "Aşağıdaki butonları kullan:\n"
            "➕ **Hak Ekle** — bir kullanıcıya hak ekle\n"
            "➖ **Hak Al** — bir kullanıcıdan hak geri al\n"
            "📋 **Kullanıcılar** — hak verilmiş tüm kullanıcıları gör"
        ),
        color=discord.Color.orange(),
    )
    await interaction.response.send_message(embed=embed, view=AdminView())


class AdminView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=300)  # 5 dk sonra butonlar pasifleşir

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Panel butonlarına sadece admin dokunabilsin."""
        if interaction.user.id != ADMIN_ID:
            await interaction.response.send_message(
                "⛔ Bu panel sana ait değil.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Hak Ekle", emoji="➕", style=discord.ButtonStyle.success)
    async def add_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RightsModal(mode="add"))

    @discord.ui.button(label="Hak Al", emoji="➖", style=discord.ButtonStyle.danger)
    async def remove_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RightsModal(mode="remove"))

    @discord.ui.button(label="Kullanıcılar", emoji="📋", style=discord.ButtonStyle.secondary)
    async def list_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        rights = all_rights()
        if not rights:
            await interaction.response.send_message(
                "📭 Henüz kimseye hak verilmemiş.", ephemeral=True
            )
            return
        lines = [f"• <@{uid}> (`{uid}`) — **{cnt}** hak" for uid, cnt in rights.items()]
        text = "📋 **Hak verilmiş kullanıcılar:**\n" + "\n".join(lines)
        # Discord 2000 karakter limiti; uzun listeyi böl
        if len(text) > 1900:
            text = text[:1900] + "\n… (liste uzun)"
        await interaction.response.send_message(text, ephemeral=True)


def _parse_user_id(raw: str):
    """'123', '<@123>', '<@!123>' formatlarından sayısal ID çıkarır. Geçersizse None."""
    digits = re.sub(r"\D", "", raw or "")
    return int(digits) if digits else None


class RightsModal(discord.ui.Modal):
    def __init__(self, mode: str):
        self.mode = mode  # "add" veya "remove"
        title = "Hak Ekle" if mode == "add" else "Hak Al"
        super().__init__(title=title)
        self.user_field = discord.ui.TextInput(
            label="Kullanıcı ID (veya @etiket)",
            placeholder="Örn: 123456789012345678",
        )
        self.amount_field = discord.ui.TextInput(
            label="Miktar",
            placeholder="Örn: 5",
        )
        self.add_item(self.user_field)
        self.add_item(self.amount_field)

    async def on_submit(self, interaction: discord.Interaction):
        uid = _parse_user_id(self.user_field.value)
        try:
            amount = int(re.sub(r"\D", "", self.amount_field.value or ""))
        except ValueError:
            amount = 0

        if uid is None or amount <= 0:
            await interaction.response.send_message(
                "❌ Geçersiz kullanıcı ID veya miktar.", ephemeral=True
            )
            return

        delta = amount if self.mode == "add" else -amount
        async with data_lock:
            new_balance = modify_rights(uid, delta)

        if self.mode == "add":
            msg = f"✅ <@{uid}> kullanıcısına **{amount}** hak eklendi. Yeni bakiye: **{new_balance}**"
            # Alım (hak ekleme) PURCHASE_SHARE_MIN ve üzeriyse ve bu kullanıcının
            # bir referansçısı varsa, referansçıya 1/4 pay verip vermeyeceğini
            # admine sor. Karar admine ait.
            referrer = get_referrer(uid)
            if amount >= PURCHASE_SHARE_MIN and referrer and referrer != str(uid):
                share = amount // 4
                if share > 0:
                    view = ReferralShareView(referrer_id=referrer, share=share, buyer_id=str(uid))
                    await interaction.response.send_message(
                        msg
                        + f"\n\n🔗 Bu kullanıcının referansçısı <@{referrer}>. "
                        f"Alım **{PURCHASE_SHARE_MIN}+** olduğu için ona **{share}** hak "
                        f"(1/4) gitmeli. Vermek istiyor musun?",
                        view=view,
                        ephemeral=True,
                    )
                    return
        else:
            msg = f"➖ <@{uid}> kullanıcısından **{amount}** hak alındı. Yeni bakiye: **{new_balance}**"
        await interaction.response.send_message(msg, ephemeral=True)


class ReferralShareView(discord.ui.View):
    """Admin bir kullanıcıya 25+ hak eklediğinde, referansçıya 1/4 pay verilip
    verilmeyeceğini admin karar versin diye çıkan onay paneli."""

    def __init__(self, referrer_id: str, share: int, buyer_id: str):
        super().__init__(timeout=120)
        self.referrer_id = referrer_id
        self.share = share
        self.buyer_id = buyer_id

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != ADMIN_ID:
            await interaction.response.send_message(
                "⛔ Bu onay sana ait değil.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="Referansa Ver", emoji="✅", style=discord.ButtonStyle.success)
    async def give_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        async with data_lock:
            new_bal = modify_rights(self.referrer_id, self.share)
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=(
                f"✅ Referansçı <@{self.referrer_id}> kullanıcısına **{self.share}** hak "
                f"(1/4 pay) verildi. Yeni bakiyesi: **{new_bal}**"
            ),
            view=self,
        )
        # Referansçıya haber ver (best-effort)
        try:
            owner = discord_bot.get_user(int(self.referrer_id)) or await discord_bot.fetch_user(int(self.referrer_id))
            if owner:
                await owner.send(
                    f"💸 Davet ettiğin <@{self.buyer_id}> alım yaptı ve sana "
                    f"**+{self.share} hak** (1/4 pay) yazıldı! `/hakkim` ile bakabilirsin."
                )
        except Exception:
            pass

    @discord.ui.button(label="Verme", emoji="✖️", style=discord.ButtonStyle.secondary)
    async def skip_btn(self, interaction: discord.Interaction, button: discord.ui.Button):
        for item in self.children:
            item.disabled = True
        await interaction.response.edit_message(
            content=f"✖️ Referans payı verilmedi. (<@{self.referrer_id}> için 1/4 atlandı.)",
            view=self,
        )


@contextlib.asynccontextmanager
async def telegram_waiter(with_buttons=False, with_file=False, timeout=20):
    """Hedef bottan yeni mesaj gelmesini bekleyen yardımcı.

    Handler `async with` bloğuna girer girmez kurulur; böylece komutu/tıklamayı
    blok içinde gönderip cevabı `await waiter` ile beklersin, mesaj kaçmaz.
    Zaman aşımında asyncio.TimeoutError yükseltir.
    """
    future = asyncio.get_event_loop().create_future()

    async def handler(event):
        if with_buttons and not event.message.buttons:
            return
        if with_file and not event.message.file:
            return
        if not future.done():
            future.set_result(event.message)

    tg_client.add_event_handler(
        handler, events.NewMessage(from_users=TELEGRAM_BOT_USERNAME)
    )
    try:
        yield asyncio.wait_for(future, timeout=timeout)
    finally:
        tg_client.remove_event_handler(handler)


def extract_found_count(text: str):
    """Hedef botun cevabındaki 'Free version shows: 250 000' satırından
    sadece adedi çeker -> '250000'. Böyle bir satır yoksa None döner.
    'Free/Premium' kelimelerini hiç göstermeyiz, sadece sayıyı."""
    if not text:
        return None
    m = re.search(r"free version shows:\s*([\d\s.,]+)", text, re.IGNORECASE)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))  # boşluk/nokta/virgülü at, sadece rakam
    return digits or None


async def forward_result(dest, tg_message):
    """Telegram'dan gelen sonucu Discord kanalına (dest) yollar.
    `dest` .send() olan herhangi bir hedef olabilir (ör. interaction.channel)."""
    if tg_message.file:
        raw = await tg_client.download_media(tg_message, file=bytes)
        filename = tg_message.file.name or "sonuc.txt"

        if len(raw) <= DISCORD_FILE_LIMIT:
            await dest.send(
                content="✅ İşte sonuç:",
                file=discord.File(io.BytesIO(raw), filename=filename),
            )
        else:
            # 10MB üstü -> gofile.io'ya yükle, linki paylaş
            status = await dest.send("📦 Dosya 10MB üstü, harici linke yükleniyor...")
            link = await upload_to_gofile(raw, filename)
            if link:
                await status.edit(content=f"✅ Dosya hazır (10MB üstü olduğu için link olarak):\n{link}")
            else:
                await status.edit(content="❌ Dosya çok büyük ve harici yükleme de başarısız oldu.")
    else:
        count = extract_found_count(tg_message.text)
        if count:
            await dest.send(f"✅ {count} adet bulundu")
        else:
            await dest.send(tg_message.text or "(boş mesaj)")


async def forward_result_to_dm(user, channel, tg_message):
    """Arama sonucunu (zip/txt vb.) komutu kullanan kişinin DM'ine gönderir;
    kullanılan kanala ise herkesin görebileceği "DM'ine iletildi" bildirimi yazar.
    DM kapalıysa (Forbidden) sonucu kanala düşürüp kullanıcıyı uyarır."""
    # DM kanalını aç
    dm = user.dm_channel
    if dm is None:
        try:
            dm = await user.create_dm()
        except discord.HTTPException:
            dm = None

    if dm is not None:
        try:
            await forward_result(dm, tg_message)
            await channel.send(f"📩 {user.mention} sonuç DM'ine iletildi.")
            return
        except discord.Forbidden:
            # Kullanıcının DM'i kapalı; kanala düşür
            pass

    await channel.send(
        f"⚠️ {user.mention} DM'in kapalı olduğu için sonuç buraya gönderiliyor."
    )
    await forward_result(channel, tg_message)


async def upload_to_gofile(raw: bytes, filename: str) -> str | None:
    """Dosyayı gofile.io'ya yükler, indirme linkini döner. Başarısızsa None döner."""
    try:
        async with aiohttp.ClientSession() as session:
            # Önce en uygun sunucuyu al
            async with session.get("https://api.gofile.io/servers") as resp:
                servers_data = await resp.json()
            servers = servers_data.get("data", {}).get("servers", [])
            if not servers:
                print("gofile: uygun sunucu bulunamadı")
                return None
            server = servers[0]["name"]

            upload_url = f"https://{server}.gofile.io/contents/uploadfile"
            form = aiohttp.FormData()
            form.add_field("file", raw, filename=filename)

            async with session.post(upload_url, data=form) as resp:
                result = await resp.json()

            if result.get("status") == "ok":
                return result["data"]["downloadPage"]
            return None
    except Exception as e:
        print(f"gofile yükleme hatası: {e}")
        return None


_commands_synced = False


@discord_bot.event
async def on_ready():
    global _commands_synced
    print(f"✅ Discord'a bağlanıldı: {discord_bot.user} ({discord_bot.user.id})")

    # Komutları yalnızca ilk on_ready'de senkronize et (reconnect'lerde tekrar etme)
    if _commands_synced:
        return
    _commands_synced = True
    try:
        # Sunuculara ANINDA yansıması için global komutları her sunucuya kopyalayıp
        # o sunucuya sync et. sync() o kapsamdaki tüm komut setini güncelle KİLE
        # değiştirir; yani eski/takılı komutlar da otomatik silinir.
        for guild in discord_bot.guilds:
            discord_bot.tree.copy_global_to(guild=guild)
            synced = await discord_bot.tree.sync(guild=guild)
            print(f"   {guild.name}: {len(synced)} komut senkronize edildi")
        # DM'lerde de çalışması için global sync (yayılması ~1 saati bulabilir)
        global_synced = await discord_bot.tree.sync()
        print(f"   Global: {len(global_synced)} komut senkronize edildi")
        print("   Komutlar hazır: /search /deneme /hakkim /help /admin")
    except Exception as e:
        _commands_synced = False
        print(f"⚠️ Komut senkronizasyonu başarısız: {e}")


@discord_bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
    """Slash komutlarında oluşan hataları kullanıcıya nazikçe bildir, botu düşürme."""
    print(f"Slash komut hatası: {error!r}")
    try:
        msg = "⚠️ Komut çalışırken bir hata oluştu."
        if interaction.response.is_done():
            await interaction.followup.send(msg, ephemeral=True)
        else:
            await interaction.response.send_message(msg, ephemeral=True)
    except Exception:
        pass


async def main():
    await tg_client.start()  # ilk seferde telefon + kod isteyecek
    async with discord_bot:
        # İki client'ı aynı event loop'ta paralel çalıştır.
        # Telethon'un da canlı kalması için run_until_disconnected'ı ekliyoruz;
        # aksi halde bağlantı düşerse köprü sessizce ölür.
        await asyncio.gather(
            discord_bot.start(DISCORD_BOT_TOKEN),
            tg_client.run_until_disconnected(),
        )


if __name__ == "__main__":
    asyncio.run(main())