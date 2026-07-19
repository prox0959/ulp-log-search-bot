"""
Telegram -> Discord Bridge Bot
--------------------------------
Discord'da /search <kelime> yazınca, Telethon (senin Telegram hesabın) üzerinden
hedef Telegram botuna /search <kelime> gönderir, gelen inline butonlu seçenekleri
Discord'a taşır, kullanıcı seçim yapınca Telegram tarafında butona tıklar,
gelen .txt/.zip dosyasını indirip Discord'a yükler.

KOMUTLAR
--------
/search <site>  -> arama yapar

KURULUM
-------
pip install -r requirements.txt

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
import os
import sys
import contextlib
import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from telethon import TelegramClient, events

# Windows konsolu (cp1254 vb.) emoji'li print'lerde çökebilir; UTF-8'e sabitle
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

DISCORD_FILE_LIMIT = 10 * 1024 * 1024  # 10 MB, Discord ücretsiz limiti

# Basit domain/URL kontrolü
DOMAIN_PATTERN = re.compile(
    r"^(https?://)?([a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(/.*)?$"
)

# ------------------- CONFIG -------------------
TELEGRAM_API_ID = 0                                     # my.telegram.org'dan al (örn: 123456)
TELEGRAM_API_HASH = ""                                  # my.telegram.org'dan al (örn: "abcdef123456...")
TELEGRAM_BOT_USERNAME = "@dead_handbot"                 # köprü kurulacak Telegram botunun kullanıcı adı
DISCORD_BOT_TOKEN = ""                                  # Discord bot tokenini girin
TELETHON_SESSION_NAME = "bridge_session"                # session dosyası adı, sabit kalsın

# Kullanıcı komutlarının çalışabileceği kanal ID'si (0 ise her kanalda ve DM'de çalışır)
ALLOWED_CHANNEL_ID = 0
# Kullanıcıların aradığı sitelerin loglanacağı Discord webhook'u (boş ise loglama yapılmaz)
SEARCH_LOG_WEBHOOK = ""
# ----------------------------------------------

tg_client = TelegramClient(TELETHON_SESSION_NAME, TELEGRAM_API_ID, TELEGRAM_API_HASH)

intents = discord.Intents.default()
intents.message_content = True

discord_bot = commands.Bot(
    command_prefix=commands.when_mentioned_or("/", "!"),
    intents=intents,
    help_command=None,
)

# Aynı anda tek arama işlemi varsayımıyla basit bir kilit/kuyruk
search_lock = asyncio.Lock()


# ------------------- KANAL KISITI + LOG -------------------
async def log_search(user, query: str, where: str) -> None:
    """Yapılan aramayı webhook'a gönderir. Hata olsa da botu düşürmez."""
    if not SEARCH_LOG_WEBHOOK:
        return
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


# ------------------- TELEGRAM WAITER -------------------
@contextlib.asynccontextmanager
async def telegram_waiter(with_buttons=False, with_file=False, timeout=20):
    """Hedef bottan yeni mesaj gelmesini bekleyen yardımcı."""
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
    """Hedef botun cevabındaki adedi çeker."""
    if not text:
        return None
    m = re.search(r"free version shows:\s*([\d\s.,]+)", text, re.IGNORECASE)
    if not m:
        return None
    digits = re.sub(r"\D", "", m.group(1))
    return digits or None


async def upload_to_gofile(raw: bytes, filename: str) -> str | None:
    """Dosyayı gofile.io'ya yükler, indirme linkini döner. Başarısızsa None döner."""
    try:
        async with aiohttp.ClientSession() as session:
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


async def forward_result(dest, tg_message):
    """Telegram'dan gelen sonucu Discord kanalına (dest) yollar."""
    if tg_message.file:
        raw = await tg_client.download_media(tg_message, file=bytes)
        filename = tg_message.file.name or "sonuc.txt"

        if len(raw) <= DISCORD_FILE_LIMIT:
            await dest.send(
                content="✅ İşte sonuç:",
                file=discord.File(io.BytesIO(raw), filename=filename),
            )
        else:
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
    """Arama sonucunu komutu kullanan kişinin DM'ine gönderir."""
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
            pass

    await channel.send(
        f"⚠️ {user.mention} DM'in kapalı olduğu için sonuç buraya gönderiliyor."
    )
    await forward_result(channel, tg_message)


# ------------------- Discord Commands -------------------
@discord_bot.tree.command(name="search", description="Bir site için arama yapar.")
@app_commands.describe(site="Aranacak site adresi, örn: vk.com")
async def search(interaction: discord.Interaction, site: str):
    query = site.strip()

    # Kanal kısıtlaması kontrolü
    if ALLOWED_CHANNEL_ID != 0 and interaction.guild is not None and interaction.channel.id != ALLOWED_CHANNEL_ID:
        await interaction.response.send_message(
            f"❌ Bu komut sadece izin verilen kanalda çalışabilir.",
            ephemeral=True
        )
        return

    if not DOMAIN_PATTERN.match(query):
        await interaction.response.send_message(
            "❌ Geçersiz format. Bir site adresi gir, örnek:\n"
            "`/search vk.com` veya `/search https://vk.com`",
            ephemeral=True
        )
        return

    await interaction.response.defer(thinking=True)

    # Webhook loglama
    where = "DM" if interaction.guild is None else f"#{interaction.channel} ({interaction.guild})"
    await log_search(interaction.user, query, where)

    channel = interaction.channel

    async with search_lock:
        await interaction.edit_original_response(content=f"🔎 Aranıyor: **{query}** ...")

        try:
            async with telegram_waiter(with_buttons=True, timeout=20) as waiter:
                await tg_client.send_message(TELEGRAM_BOT_USERNAME, f"/search {query}")
                response = await waiter
        except asyncio.TimeoutError:
            await interaction.edit_original_response(
                content="❌ Telegram botundan cevap gelmedi (zaman aşımı)."
            )
            return

        buttons = response.buttons
        if not buttons:
            await forward_result_to_dm(interaction.user, channel, response)
            return

        flat_buttons = [b for row in buttons for b in row]
        options_text = "\n".join(f"{i+1}️⃣ {b.text}" for i, b in enumerate(flat_buttons))

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

        await interaction.followup.send("⏳ Lütfen bekleyin, bu işlem biraz zaman alabilir...")

        try:
            async with telegram_waiter(with_file=True, timeout=30) as waiter:
                await response.click(idx)
                file_msg = await waiter
        except asyncio.TimeoutError:
            await interaction.followup.send("❌ Dosya gelmedi (zaman aşımı).")
            return

        await forward_result_to_dm(interaction.user, channel, file_msg)


_commands_synced = False


@discord_bot.event
async def on_ready():
    global _commands_synced
    print(f"✅ Discord'a bağlanıldı: {discord_bot.user} ({discord_bot.user.id})")

    if _commands_synced:
        return
    _commands_synced = True
    try:
        for guild in discord_bot.guilds:
            discord_bot.tree.copy_global_to(guild=guild)
            synced = await discord_bot.tree.sync(guild=guild)
            print(f"   {guild.name}: {len(synced)} komut senkronize edildi")
        global_synced = await discord_bot.tree.sync()
        print(f"   Global: {len(global_synced)} komut senkronize edildi")
        print("   Komutlar hazır: /search")
    except Exception as e:
        _commands_synced = False
        print(f"⚠️ Komut senkronizasyonu başarısız: {e}")


@discord_bot.tree.error
async def on_app_command_error(interaction: discord.Interaction, error: app_commands.AppCommandError):
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
    await tg_client.start()
    async with discord_bot:
        await asyncio.gather(
            discord_bot.start(DISCORD_BOT_TOKEN),
            tg_client.run_until_disconnected(),
        )


if __name__ == "__main__":
    asyncio.run(main())
