# WA Checker Bot

Bot Telegram untuk mengecek apakah sebuah nomor HP terdaftar di WhatsApp. Bot
ditulis dengan Python (python-telegram-bot) dan menggunakan helper Node.js
(Baileys) untuk berkomunikasi dengan WhatsApp tanpa perlu API berbayar.

## Fitur

- `/start`, `/help` — info & panduan
- `/cek` — cek satu atau banyak nomor sekaligus (maks 50)
- `/file` — upload `.txt`/`.csv`, cek semua nomor di dalamnya
- `/riwayat` — 10 hasil pengecekan terakhir per user
- `/pair`, `/status`, `/unpair` — kelola koneksi WhatsApp (pairing pakai kode, tanpa QR)
- `/admin`, `/broadcast`, `/adduser`, `/removeuser`, `/listuser` — panel admin
- Rate limiting, logging, dan ekspor hasil ke file `.txt`

## Struktur Proyek

```
.
├── bot.py                  # Bot Telegram utama (Python)
├── requirements.txt        # Dependensi Python
├── .env.example            # Template konfigurasi
├── start.sh                # Script setup + run all-in-one
├── data/                   # history.json & allowed_ids.json (otomatis)
└── node_helper/
    ├── wa_helper.js        # Helper WhatsApp (Baileys)
    ├── package.json
    └── wa_session/         # Sesi WA (otomatis dibuat setelah pairing)
```

## Persyaratan

- Python 3.9+
- Node.js 18+
- Token bot Telegram dari [@BotFather](https://t.me/BotFather)

## Cara Menjalankan

### Cara cepat (otomatis)

```bash
cp .env.example .env      # lalu isi TELEGRAM_BOT_TOKEN di .env
bash start.sh
```

`start.sh` akan menginstal dependensi Python & Node, lalu menjalankan bot.

### Cara manual

```bash
# 1. Konfigurasi
cp .env.example .env
#    edit .env -> isi TELEGRAM_BOT_TOKEN (wajib)

# 2. Dependensi Python
pip3 install -r requirements.txt

# 3. Dependensi Node (Baileys)
cd node_helper && npm install && cd ..

# 4. Jalankan bot
python3 bot.py
```

## Menautkan WhatsApp

1. Jalankan bot, buka chat ke bot di Telegram.
2. Kirim `/pair +6281234567890` (atau `/pair` jika `OWNER_PHONE_NUMBER` sudah diisi di `.env`).
3. Bot membalas dengan kode 8 digit.
4. Di HP: **WhatsApp → Setelan → Perangkat Tertaut → Tautkan Perangkat → Tautkan dengan nomor telepon**, lalu masukkan kode.
5. Cek dengan `/status`. Setelah terhubung, gunakan `/cek` untuk mengecek nomor.

## Konfigurasi (.env)

| Variabel | Wajib | Keterangan |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ | Token dari @BotFather |
| `OWNER_PHONE_NUMBER` | — | Nomor WA default untuk `/pair` (format `628xxx`) |
| `ADMIN_USER_ID` | — | User ID Telegram admin |
| `ALLOWED_USER_IDS` | — | Daftar user diizinkan (pisah koma). Kosong = terbuka |
| `MAX_NUMBERS_PER_REQUEST` | — | Default `50` |
| `RATE_LIMIT_WINDOW_SEC` | — | Default `60` |
| `RATE_LIMIT_MAX_REQ` | — | Default `5` |
| `NODE_CHECK_TIMEOUT_SEC` | — | Default `180` |

## Deploy 24/7 (Railway / Render)

> Bot ini **tidak bisa** di-deploy ke Vercel karena Vercel bersifat *serverless*
> (fungsi mati setelah beberapa detik & tanpa disk permanen), sedangkan bot ini
> butuh proses yang **nyala terus** + penyimpanan sesi WhatsApp yang **persisten**.
> Gunakan Railway atau Render. Sudah disediakan `Dockerfile`, `railway.json`, dan `render.yaml`.

### Railway

1. Push proyek ini ke GitHub.
2. Buka [railway.app](https://railway.app) → **New Project → Deploy from GitHub repo**.
3. Railway otomatis memakai `Dockerfile`.
4. Buka tab **Variables**, isi:
   - `TELEGRAM_BOT_TOKEN` (wajib)
   - `ADMIN_USER_ID`, `OWNER_PHONE_NUMBER` (opsional)
5. Buka tab **Settings → Volumes**, buat volume baru dan mount ke **`/data`**
   (di sinilah sesi WhatsApp & history disimpan agar tidak hilang saat restart).
6. Deploy. Lihat **Logs**, lalu lanjut ke langkah "Menautkan WhatsApp" via Telegram.

### Render

1. Push proyek ini ke GitHub.
2. Buka [render.com](https://render.com) → **New → Blueprint**, pilih repo ini.
   Render membaca `render.yaml` (tipe **worker** + disk persisten `/data` otomatis).
3. Isi env var rahasia saat diminta: `TELEGRAM_BOT_TOKEN` (wajib), `ADMIN_USER_ID`,
   `OWNER_PHONE_NUMBER`.
4. Deploy, lalu lanjut ke langkah "Menautkan WhatsApp" via Telegram.

> Variabel `DATA_DIR=/data` dan `WA_SESSION_DIR=/data/wa_session` sudah diset di
> Dockerfile/render.yaml — keduanya mengarah ke volume persisten.

## Catatan

- Hentikan bot dengan `CTRL+C` (shutdown bersih).
- Log aktivitas tersimpan di `bot.log`.
- Bot melakukan polling — jalankan di server/VM yang menyala terus agar selalu aktif.
- Gunakan bot secara bertanggung jawab dan patuhi Ketentuan Layanan WhatsApp.
