

Bu bot, Discord üzerinden slash komutlarıyla Telegram'daki hedef bir bot ile iletişim kurmanızı sağlar. Discord üzerinden arama yaptığınızda Telegram botundan sonuçları çeker ve dosyaları size iletir.

TELEGRAMDAKİ BOTUN 3.4 MİLYAR VERİSİ VARDIR

## Kurulum ve Kurulum Adımları

Botu çalıştırmadan önce aşağıdaki adımları sırasıyla takip edin:

### 1. Gereksinimlerin Kurulması
Öncelikle gerekli Python kütüphanelerini yükleyin. Komut satırını (Terminal/PowerShell) açıp proje dizininde şu komutu çalıştırın:
```bash
pip install -r requirements.txt
```

### 2. API ve Token Bilgilerinin Alınması
Botun çalışabilmesi için Telegram ve Discord hesap/bot bilgilerine ihtiyacınız var:

* **Telegram API Anahtarları (Hesabınız için):**
  1. [my.telegram.org](https://my.telegram.org) adresine gidin ve Telegram numaranızla giriş yapın.
  2. **API development tools** kısmına girerek yeni bir uygulama oluşturun.
  3. Size verilen `api_id` ve `api_hash` değerlerini kopyalayın.

* **Discord Bot Token:**
  1. [Discord Developer Portal](https://discord.com/developers/applications) adresine gidin.
  2. Yeni bir Application oluşturup **Bot** sekmesinden bir bot oluşturun ve **Token**ını kopyalayın.
  3. Aynı sayfada aşağı kaydırarak **Privileged Gateway Intents** altındaki **Message Content Intent** seçeneğini aktif edin (Aksi halde bot mesajları okuyamaz).

### 3. Yapılandırma (`main.py` Düzenleme)
[main.py] dosyasını bir metin editörüyle açın ve **`# ------------------- CONFIG -------------------`** bölümünü kendi bilgilerinizle doldurun:

```python
TELEGRAM_API_ID = 12345678                            # Telegram'dan aldığınız api_id
TELEGRAM_API_HASH = "abcdef0123456789abcdef0123456789"  # Telegram'dan aldığınız api_hash
TELEGRAM_BOT_USERNAME = "@dead_handbot"               # Köprü kurulacak Telegram botunun kullanıcı adı
DISCORD_BOT_TOKEN = "DISCORD_BOT_TOKENUNUZ"            # Discord botunuzun tokeni

ADMIN_ID = 1122334455667788                            # Admin yetkisine sahip olacak Discord Kullanıcı ID'niz
ALLOWED_CHANNEL_ID = 1122334455667788                  # Komutların çalışacağı Discord Kanal ID'si
SEARCH_LOG_WEBHOOK = "DISCORD_WEBHOOK_URL"             # Arama geçmişinin loglanacağı Webhook URL'si
REFERRAL_GUILD_ID = 1122334455667788                   # Referans doğrulama sunucu ID'niz
```

### 4. Botu Çalıştırma
Tüm yapılandırmayı yaptıktan sonra terminal üzerinden botu başlatın:
```bash
python main.py
```

> **Önemli Not (İlk Çalıştırma):**
> Botu ilk kez çalıştırdığınızda, terminal sizden **Telegram telefon numaranızı** (ülke koduyla birlikte örn: `+905...`) ve ardından Telegram uygulamasına gelen **doğrulama kodunu** girmenizi isteyecektir. Bu işlem tek seferliktir; oturum açıldıktan sonra dizinde otomatik olarak `bridge_session.session` dosyası oluşacak ve sonraki çalıştırmalarda tekrar kod istemeyecektir.
