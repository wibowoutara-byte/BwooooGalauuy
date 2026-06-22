"""
bot.py  —  WA Checker Bot (Versi Lengkap & Fixed)
==================================================
Fitur:
  ✅ /start         — selamat datang
  ✅ /help          — panduan lengkap
  ✅ /cek           — cek 1 atau banyak nomor sekaligus (max 50)
  ✅ /file          — upload file .txt/.csv, cek semua nomor
  ✅ /riwayat       — lihat 10 hasil cek terakhir (per user)
  ✅ /pair +62xxx   — pairing WA dengan kode (tanpa QR)
  ✅ /status        — cek apakah session WA aktif
  ✅ /unpair        — logout/hapus session WA dari Telegram
  ✅ /admin         — panel admin (statistik + aksi)
  ✅ /broadcast     — kirim pesan ke semua user (admin only)
  ✅ /adduser       — tambah user yang diizinkan (admin only)
  ✅ /removeuser    — hapus user dari daftar izin (admin only)
  ✅ /listuser      — lihat daftar user (admin only)
  ✅ Rate limiting  — anti-spam per user (configurable)
  ✅ Bulk export    — hasil dikirim sebagai file .txt rapi
  ✅ Logging        — semua aktivitas tercatat di bot.log
  ✅ Notif admin    — admin diberitahu saat ada user baru
  ✅ Graceful shutdown — CTRL+C bersih
  ✅ /pair bisa terima nomor langsung: /pair +6281234567890

Struktur folder:
  wa_checker_bot/
  ├── bot.py               ← file ini
  ├── .env                 ← konfigurasi
  ├── bot.log              ← log otomatis
  ├── data/
  │   ├── history.json     ← riwayat cek per user
  │   └── allowed_ids.json ← daftar user
  └── node_helper/
      ├── wa_helper.js
      ├── package.json
      └── wa_session/

Setup:
  1. pip install python-telegram-bot python-dotenv aiofiles
  2. cd node_helper && npm install
  3. Isi .env (lihat .env.example)
  4. python bot.py
  5. Di Telegram: /pair +6281234567890 → masukkan kode → siap!

.env.example:
  TELEGRAM_BOT_TOKEN=123456789:AAxxxxxx
  OWNER_PHONE_NUMBER=628123456789
  ADMIN_USER_ID=123456789
  ALLOWED_USER_IDS=123456789,987654321    # kosongkan = semua bisa pakai
  MAX_NUMBERS_PER_REQUEST=50
  RATE_LIMIT_WINDOW_SEC=60
  RATE_LIMIT_MAX_REQ=5
  NODE_CHECK_TIMEOUT_SEC=180
"""

import os
import re
import json
import asyncio
import subprocess
import sys
import logging
import time
import io
from datetime import datetime
from collections import defaultdict
from pathlib import Path
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.constants import ParseMode
from telegram.error import TelegramError

# ─── Setup ────────────────────────────────────────────────────────────────────

load_dotenv()

BASE_DIR     = Path(__file__).parent
# DATA_DIR dapat di-override via env (untuk volume persisten di Railway/Render)
DATA_DIR     = Path(os.getenv("DATA_DIR", str(BASE_DIR / "data")))
HISTORY_FILE = DATA_DIR / "history.json"
ALLOWED_FILE = DATA_DIR / "allowed_ids.json"
LOG_FILE     = DATA_DIR / "bot.log"
NODE_HELPER  = BASE_DIR / "node_helper" / "wa_helper.js"
NODE_CWD     = BASE_DIR / "node_helper"

DATA_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logger = logging.getLogger("WABot")

# ─── Konfigurasi dari .env ────────────────────────────────────────────────────

BOT_TOKEN          = os.getenv("TELEGRAM_BOT_TOKEN", "")
OWNER_PHONE        = os.getenv("OWNER_PHONE_NUMBER", "").strip()
ADMIN_USER_ID      = os.getenv("ADMIN_USER_ID", "").strip()

_raw_allowed         = os.getenv("ALLOWED_USER_IDS", "")
_STATIC_IDS: set[str] = {x.strip() for x in _raw_allowed.split(",") if x.strip()}

MAX_PER_REQUEST    = int(os.getenv("MAX_NUMBERS_PER_REQUEST", "50"))
RATE_LIMIT_WINDOW  = int(os.getenv("RATE_LIMIT_WINDOW_SEC", "60"))
RATE_LIMIT_MAX_REQ = int(os.getenv("RATE_LIMIT_MAX_REQ", "5"))
NODE_CHECK_TIMEOUT = int(os.getenv("NODE_CHECK_TIMEOUT_SEC", "180"))

# ─── State In-Memory ──────────────────────────────────────────────────────────

_rate_tracker: dict[int, list[float]] = defaultdict(list)

# ─── Helper: Allowed IDs (dinamis) ───────────────────────────────────────────

def load_allowed_ids() -> set[str]:
    ids = set(_STATIC_IDS)
    if ALLOWED_FILE.exists():
        try:
            saved = json.loads(ALLOWED_FILE.read_text(encoding="utf-8"))
            ids.update(str(x) for x in saved)
        except Exception:
            pass
    if ADMIN_USER_ID:
        ids.add(ADMIN_USER_ID)
    return ids


def save_dynamic_allowed(ids: set[str]):
    dynamic = ids - _STATIC_IDS
    if ADMIN_USER_ID:
        dynamic.discard(ADMIN_USER_ID)
    try:
        ALLOWED_FILE.write_text(
            json.dumps(sorted(dynamic), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Gagal simpan allowed_ids: %s", e)


def is_authorized(user_id: int) -> bool:
    allowed = load_allowed_ids()
    if not allowed and not _STATIC_IDS:
        return True  # mode terbuka
    return str(user_id) in allowed


def is_admin(user_id: int) -> bool:
    return str(user_id) == ADMIN_USER_ID if ADMIN_USER_ID else False

# ─── Helper: Rate Limit ───────────────────────────────────────────────────────

def check_rate_limit(user_id: int) -> bool:
    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW
    history = _rate_tracker[user_id]
    history[:] = [t for t in history if t > window_start]
    if len(history) >= RATE_LIMIT_MAX_REQ:
        return False
    history.append(now)
    return True


def rate_reset_seconds(user_id: int) -> int:
    if not _rate_tracker[user_id]:
        return 0
    oldest = min(_rate_tracker[user_id])
    remaining = int(RATE_LIMIT_WINDOW - (time.time() - oldest))
    return max(0, remaining)

# ─── Helper: Normalisasi Nomor ────────────────────────────────────────────────

def normalize_number(raw: str) -> str | None:
    """
    Bersihkan & normalisasi nomor ke format internasional tanpa '+'.
    Mendukung: 08xxx, 628xxx, +628xxx, +62xxx, 8xxx
    """
    cleaned = re.sub(r"[\s\-\(\)\+\.\,]", "", raw)
    if not cleaned.isdigit():
        return None
    if cleaned.startswith("0"):
        cleaned = "62" + cleaned[1:]
    elif cleaned.startswith("8") and len(cleaned) <= 12:
        cleaned = "62" + cleaned
    if len(cleaned) < 8 or len(cleaned) > 15:
        return None
    return cleaned


def normalize_phone_for_pair(raw: str) -> str | None:
    """
    Normalisasi nomor khusus untuk /pair.
    Menerima: +62xxx, 62xxx, 08xxx, 628xxx → return string digit (628xxx)
    """
    cleaned = re.sub(r"[\s\-\(\)\+\.]", "", raw)
    if not cleaned.isdigit():
        return None
    if cleaned.startswith("0"):
        cleaned = "62" + cleaned[1:]
    elif cleaned.startswith("8") and len(cleaned) <= 12:
        cleaned = "62" + cleaned
    if not cleaned.startswith("62"):
        return None
    if len(cleaned) < 10 or len(cleaned) > 15:
        return None
    return cleaned


def parse_numbers_from_text(text: str) -> tuple[list[str], list[str]]:
    raws = re.split(r"[\s,;\n\t|]+", text.strip())
    valid, invalid = [], []
    seen: set[str] = set()
    for raw in raws:
        raw = raw.strip()
        if not raw:
            continue
        norm = normalize_number(raw)
        if norm and norm not in seen:
            valid.append(norm)
            seen.add(norm)
        elif raw:
            invalid.append(raw)
    return valid, invalid

# ─── Helper: Riwayat ──────────────────────────────────────────────────────────

def load_history() -> dict:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def save_history(data: dict):
    try:
        HISTORY_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except Exception as e:
        logger.warning("Gagal simpan history: %s", e)


def add_to_history(user_id: int, numbers: list[str], results: dict):
    data = load_history()
    uid = str(user_id)
    if uid not in data:
        data[uid] = []
    entry = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "count": len(numbers),
        "registered": sum(1 for v in results.values() if v.get("registered")),
        "not_registered": sum(1 for v in results.values() if not v.get("registered")),
        "numbers_reg": [n for n in numbers if results.get(n, {}).get("registered")],
        "numbers_not_reg": [n for n in numbers if not results.get(n, {}).get("registered")],
    }
    data[uid].insert(0, entry)
    data[uid] = data[uid][:50]
    save_history(data)

# ─── Helper: Node subprocess ──────────────────────────────────────────────────

def run_node(args: list[str], timeout: int = 30) -> dict | list:
    """Panggil wa_helper.js dan parse JSON dari stdout terakhir."""
    if not NODE_HELPER.exists():
        return {"_error": f"wa_helper.js tidak ditemukan di {NODE_HELPER}"}
    try:
        result = subprocess.run(
            ["node", str(NODE_HELPER)] + args,
            cwd=str(NODE_CWD),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {"_error": f"Timeout ({timeout}s). Coba lagi atau restart bot."}
    except FileNotFoundError:
        return {"_error": "Node.js tidak ditemukan. Install Node.js terlebih dahulu."}

    stdout = result.stdout.strip()
    if stdout:
        try:
            lines = [ln for ln in stdout.splitlines() if ln.strip()]
            # Cari baris JSON terakhir yang valid
            for line in reversed(lines):
                try:
                    return json.loads(line)
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

    if result.returncode != 0:
        logger.error("node stderr: %s", result.stderr[:500])
        return {"_error": "Gagal menghubungi WA. Cek /status atau lakukan /pair ulang."}

    return {"_error": "Tidak ada response dari WA helper."}


def run_node_check(numbers: list[str]) -> dict:
    return run_node(["check", ",".join(numbers)], timeout=NODE_CHECK_TIMEOUT)


def run_node_status() -> dict:
    return run_node(["status"], timeout=20)


def run_node_unpair() -> dict:
    return run_node(["unpair"], timeout=20)


def start_pairing_process(phone: str) -> subprocess.Popen:
    """Mulai proses pairing sebagai subprocess non-blocking."""
    return subprocess.Popen(
        ["node", str(NODE_HELPER), "pair", phone],
        cwd=str(NODE_CWD),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )

# ─── Format Hasil Cek ─────────────────────────────────────────────────────────

def format_results(
    numbers: list[str],
    results: dict,
    invalid: list[str],
) -> tuple[str, str]:
    reg     = [n for n in numbers if results.get(n, {}).get("registered")]
    not_reg = [n for n in numbers if not results.get(n, {}).get("registered")]

    summary = (
        f"📊 *Hasil Cek WhatsApp*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ Terdaftar      : `{len(reg)}`\n"
        f"❌ Tidak terdaftar: `{len(not_reg)}`\n"
        f"⚠️ Format salah   : `{len(invalid)}`\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )

    pesan = summary
    if reg:
        block = "\n".join(f"✅ `{n}`" for n in reg)
        pesan += f"\n*Terdaftar di WA:*\n{block}\n"
    if not_reg:
        block = "\n".join(f"❌ `{n}`" for n in not_reg)
        pesan += f"\n*Tidak terdaftar:*\n{block}\n"
    if invalid:
        pesan += f"\n*Format tidak valid:*\n`{'`, `'.join(invalid)}`"

    ts  = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    sep = "=" * 45
    file_lines = [
        "WA Checker Bot — Laporan Pengecekan",
        f"Waktu          : {ts}",
        f"Total dicek    : {len(numbers)}",
        f"Terdaftar      : {len(reg)}",
        f"Tidak terdaftar: {len(not_reg)}",
        f"Format salah   : {len(invalid)}",
        sep,
        "TERDAFTAR DI WHATSAPP:",
    ]
    file_lines += reg if reg else ["(tidak ada)"]
    file_lines += ["", sep, "TIDAK TERDAFTAR:"]
    file_lines += not_reg if not_reg else ["(tidak ada)"]
    if invalid:
        file_lines += ["", sep, "FORMAT TIDAK VALID:"] + invalid

    return pesan, "\n".join(file_lines)

# ─── Util: Kirim hasil (pesan / file) ────────────────────────────────────────

async def send_results(
    update: Update,
    msg,
    numbers: list[str],
    results: dict,
    invalid: list[str],
):
    pesan, file_text = format_results(numbers, results, invalid)
    add_to_history(update.effective_user.id, numbers, results)

    ts    = datetime.now().strftime("%Y%m%d_%H%M%S")
    fname = f"hasil_cek_{ts}.txt"
    kirim_file = len(pesan) > 3800 or len(numbers) > 15

    if kirim_file:
        try:
            await msg.delete()
        except Exception:
            pass
        reg_count = sum(1 for v in results.values() if v.get("registered"))
        await update.message.reply_text(
            f"✅ Selesai\\! *{reg_count}/{len(numbers)}* nomor terdaftar di WA\\.\n"
            f"Hasil lengkap dikirim sebagai file 👇",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        buf = io.BytesIO(file_text.encode("utf-8"))
        buf.name = fname
        await update.message.reply_document(document=buf, filename=fname)
    else:
        await msg.edit_text(pesan, parse_mode=ParseMode.MARKDOWN)
        if len(numbers) > 5:
            buf = io.BytesIO(file_text.encode("utf-8"))
            buf.name = fname
            await update.message.reply_document(document=buf, filename=fname)

# ─── Notif Admin ──────────────────────────────────────────────────────────────

async def notify_admin(app, text: str):
    if not ADMIN_USER_ID:
        return
    try:
        await app.bot.send_message(
            chat_id=int(ADMIN_USER_ID),
            text=text,
            parse_mode=ParseMode.MARKDOWN,
        )
    except TelegramError as e:
        logger.warning("Gagal kirim notif ke admin: %s", e)

# ─── Command Handlers ─────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user  = update.effective_user
    nama  = user.first_name or "Kamu"
    authorized = is_authorized(user.id)

    if not authorized:
        await notify_admin(
            ctx.application,
            f"🔔 *User baru mencoba bot:*\n"
            f"👤 Nama : {user.full_name}\n"
            f"🆔 ID   : `{user.id}`\n"
            f"🔗 Username: @{user.username or '-'}\n\n"
            f"Gunakan /adduser {user.id} untuk memberi akses.",
        )
        await update.message.reply_text(
            f"👋 Halo, *{nama}*!\n\n"
            f"Maaf, kamu belum memiliki akses ke bot ini.\n"
            f"Hubungi admin untuk mendapatkan akses.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    teks = (
        f"👋 Halo, *{nama}*!\n\n"
        f"Bot ini untuk mengecek apakah nomor HP terdaftar di WhatsApp.\n\n"
        f"📌 *Perintah tersedia:*\n"
        f"• /cek — Cek nomor (satu atau banyak)\n"
        f"• /file — Upload file .txt/.csv berisi nomor\n"
        f"• /riwayat — Lihat 10 hasil cek terakhirmu\n"
        f"• /status — Cek koneksi WA\n"
        f"• /pair — Hubungkan akun WA ke bot\n"
        f"• /unpair — Putuskan koneksi WA\n"
        f"• /help — Panduan lengkap\n"
    )
    if is_admin(user.id):
        teks += f"\n🛠 *Admin:* /admin | /adduser | /removeuser | /listuser | /broadcast\n"

    await update.message.reply_text(teks, parse_mode=ParseMode.MARKDOWN)


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    teks = (
        "📖 *Panduan WA Checker Bot*\n\n"
        "*═══ CEK NOMOR ═══*\n"
        "`/cek 081234567890`\n"
        "`/cek 081234567890 08987654321`\n"
        "`/cek 081234, 082345, 083456`\n"
        f"_(pisah spasi/koma/enter — max {MAX_PER_REQUEST} nomor)_\n\n"
        "*═══ CEK VIA FILE ═══*\n"
        "1. Ketik `/file`\n"
        "2. Upload file `.txt` atau `.csv`\n"
        "_(satu nomor per baris, atau dipisah koma/titik koma)_\n\n"
        "*═══ FORMAT NOMOR ═══*\n"
        "• `081234567890` → `6281234567890` ✅\n"
        "• `+6281234567890` → `6281234567890` ✅\n"
        "• `6281234567890` → tetap ✅\n"
        "• `8123456789` → `628123456789` ✅\n\n"
        "*═══ PAIRING WHATSAPP ═══*\n"
        "Cara 1 (nomor dari .env):\n"
        "  `/pair`\n\n"
        "Cara 2 (nomor langsung):\n"
        "  `/pair +6281234567890`\n"
        "  `/pair 081234567890`\n\n"
        "Langkah setelah kode muncul:\n"
        "1. Buka WhatsApp di HP\n"
        "2. ⚙️ Setelan → Perangkat Tertaut\n"
        "3. Tautkan Perangkat\n"
        "4. Pilih *Tautkan dengan nomor telepon*\n"
        "5. Masukkan kode 8 digit yang diberikan\n\n"
        "*═══ LIMIT ═══*\n"
        f"• Maks *{MAX_PER_REQUEST}* nomor per /cek\n"
        f"• Rate limit: *{RATE_LIMIT_MAX_REQ}* request per {RATE_LIMIT_WINDOW} detik\n"
        f"• File maks: *500KB*\n"
    )
    await update.message.reply_text(teks, parse_mode=ParseMode.MARKDOWN)


async def cmd_cek(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not is_authorized(user.id):
        await update.message.reply_text("🚫 Kamu tidak punya akses ke bot ini.")
        return

    if not check_rate_limit(user.id):
        sisa = rate_reset_seconds(user.id)
        await update.message.reply_text(
            f"⏳ Terlalu banyak request.\n"
            f"Tunggu *{sisa} detik* lagi, lalu coba lagi.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    raw_text = update.message.text.replace("/cek", "", 1).strip()
    # Hapus mention bot jika ada
    raw_text = re.sub(r"^@\S+\s*", "", raw_text).strip()

    if not raw_text:
        await update.message.reply_text(
            "ℹ️ Sertakan nomor setelah /cek.\n\n"
            "Contoh:\n"
            "`/cek 081234567890`\n"
            "`/cek 081234567890, 08987654321`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    numbers, invalid = parse_numbers_from_text(raw_text)

    if not numbers:
        await update.message.reply_text(
            "⚠️ Tidak ada nomor valid yang ditemukan.\n"
            "Pastikan format nomor benar (contoh: 081234567890 atau 6281234567890)."
        )
        return

    if len(numbers) > MAX_PER_REQUEST:
        await update.message.reply_text(
            f"⚠️ Maksimal *{MAX_PER_REQUEST}* nomor per request.\n"
            f"Kamu memasukkan *{len(numbers)}* nomor.\n\n"
            f"Gunakan `/file` untuk cek nomor dalam jumlah banyak.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    msg = await update.message.reply_text(
        f"⏳ Mengecek *{len(numbers)}* nomor ke WhatsApp...",
        parse_mode=ParseMode.MARKDOWN,
    )

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, run_node_check, numbers)

    if "_error" in data:
        await msg.edit_text(
            f"❌ *Gagal mengecek nomor*\n\n"
            f"Error: {data['_error']}\n\n"
            f"Pastikan WA sudah terhubung (/status) dan lakukan /pair jika belum.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    logger.info("User %s cek %d nomor", user.id, len(numbers))
    await send_results(update, msg, numbers, data, invalid)


async def cmd_file(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("🚫 Kamu tidak punya akses ke bot ini.")
        return

    ctx.user_data["waiting_file"] = True
    await update.message.reply_text(
        "📂 *Mode Upload File*\n\n"
        "Kirim file `.txt` atau `.csv` berisi nomor HP.\n"
        "Format yang diterima:\n"
        "• Satu nomor per baris\n"
        "• Atau dipisah koma/titik koma\n\n"
        "_Kirim file sekarang, atau /cek untuk mode manual._",
        parse_mode=ParseMode.MARKDOWN,
    )


async def handle_document(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        return

    if not ctx.user_data.get("waiting_file"):
        await update.message.reply_text(
            "ℹ️ Gunakan /file terlebih dahulu, lalu kirim file.\n"
            "Atau gunakan /cek untuk memasukkan nomor langsung."
        )
        return

    ctx.user_data["waiting_file"] = False
    doc = update.message.document

    if not doc.file_name.lower().endswith((".txt", ".csv")):
        await update.message.reply_text(
            "⚠️ Hanya file `.txt` atau `.csv` yang diterima.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if doc.file_size > 500_000:
        await update.message.reply_text("⚠️ File terlalu besar. Maksimal 500KB.")
        return

    if not check_rate_limit(user.id):
        sisa = rate_reset_seconds(user.id)
        await update.message.reply_text(
            f"⏳ Rate limit aktif. Tunggu *{sisa} detik* lalu kirim file lagi.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    msg = await update.message.reply_text("⏳ Membaca file...")

    tg_file    = await doc.get_file()
    raw_bytes  = await tg_file.download_as_bytearray()
    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        content = raw_bytes.decode("latin-1", errors="replace")

    numbers, invalid = parse_numbers_from_text(content)

    if not numbers:
        await msg.edit_text(
            "⚠️ Tidak ada nomor valid ditemukan di file.\n"
            "Pastikan satu nomor per baris atau dipisah koma."
        )
        return

    if len(numbers) > MAX_PER_REQUEST:
        await msg.edit_text(
            f"⚠️ File berisi *{len(numbers)}* nomor.\n"
            f"Maksimal *{MAX_PER_REQUEST}* per sekali cek.\n\n"
            f"Pecah file menjadi beberapa bagian dan kirim satu per satu.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await msg.edit_text(
        f"⏳ Mengecek *{len(numbers)}* nomor dari file `{doc.file_name}`...",
        parse_mode=ParseMode.MARKDOWN,
    )

    loop = asyncio.get_event_loop()
    data = await loop.run_in_executor(None, run_node_check, numbers)

    if "_error" in data:
        await msg.edit_text(
            f"❌ *Gagal mengecek:*\n{data['_error']}\n\n"
            f"Pastikan WA sudah terhubung (/status).",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    logger.info("User %s cek file %s (%d nomor)", user.id, doc.file_name, len(numbers))
    await send_results(update, msg, numbers, data, invalid)


async def cmd_riwayat(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("🚫 Tidak punya akses.")
        return

    data    = load_history()
    entries = data.get(str(user.id), [])

    if not entries:
        await update.message.reply_text(
            "📭 Belum ada riwayat pengecekan.\n"
            "Gunakan /cek atau /file untuk mulai mengecek nomor."
        )
        return

    lines = ["📋 *10 Pengecekan Terakhirmu:*\n"]
    for i, e in enumerate(entries[:10], 1):
        lines.append(
            f"{i}. `{e['time']}`\n"
            f"   📱 {e['count']} nomor — "
            f"✅ {e['registered']} terdaftar / "
            f"❌ {e['not_registered']} tidak"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("🚫 Tidak punya akses.")
        return

    msg    = await update.message.reply_text("🔍 Memeriksa status koneksi WA...")
    loop   = asyncio.get_event_loop()
    status = await loop.run_in_executor(None, run_node_status)

    if status.get("linked"):
        phone = status.get("phone", "tidak diketahui")
        name  = status.get("name", "")
        teks  = (
            f"✅ *WhatsApp Terhubung*\n"
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"📱 Nomor : `{phone}`\n"
            f"👤 Nama  : {name or '—'}\n\n"
            f"Bot siap digunakan untuk cek nomor."
        )
    elif "_error" in status:
        teks = (
            f"⚠️ *Error saat memeriksa:*\n{status['_error']}\n\n"
            f"Coba /pair untuk menghubungkan ulang."
        )
    else:
        teks = (
            "❌ *WhatsApp belum terhubung*\n\n"
            "Bot tidak bisa cek nomor.\n"
            "Gunakan /pair untuk menautkan akun WA."
        )

    await msg.edit_text(teks, parse_mode=ParseMode.MARKDOWN)


# ─── /pair  ───────────────────────────────────────────────────────────────────

async def cmd_pair(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """
    Mendukung dua mode:
      /pair                  → pakai OWNER_PHONE dari .env
      /pair +6281234567890   → pakai nomor yang diberikan langsung
      /pair 081234567890     → juga bisa
    """
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("🚫 Tidak punya akses.")
        return

    # ── Tentukan nomor target ──────────────────────────────────────────────
    args_text = update.message.text.replace("/pair", "", 1).strip()
    # Hapus mention bot jika ada
    args_text = re.sub(r"^@\S+\s*", "", args_text).strip()

    if args_text:
        # Nomor diberikan langsung oleh user
        phone = normalize_phone_for_pair(args_text)
        if not phone:
            await update.message.reply_text(
                "⚠️ Format nomor tidak valid.\n\n"
                "Contoh penggunaan:\n"
                "`/pair +6281234567890`\n"
                "`/pair 081234567890`\n"
                "`/pair 6281234567890`",
                parse_mode=ParseMode.MARKDOWN,
            )
            return
    else:
        # Pakai nomor dari .env
        if not OWNER_PHONE:
            await update.message.reply_text(
                "⚠️ *OWNER\\_PHONE\\_NUMBER* belum diisi di `.env`\\.\n\n"
                "Isi nomor WA di `.env`, atau berikan nomor langsung:\n"
                "`/pair \\+6281234567890`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return
        phone = normalize_phone_for_pair(OWNER_PHONE)
        if not phone:
            await update.message.reply_text(
                f"⚠️ `OWNER_PHONE_NUMBER` di .env tidak valid: `{OWNER_PHONE}`\n\n"
                "Format yang benar: `628xxxxxxxxx` (tanpa + atau 0 di depan).",
                parse_mode=ParseMode.MARKDOWN,
            )
            return

    # ── Cek apakah sudah linked ─────────────────────────────────────────────
    loop   = asyncio.get_event_loop()
    status = await loop.run_in_executor(None, run_node_status)

    if status.get("linked"):
        linked_phone = status.get("phone", phone)
        await update.message.reply_text(
            f"✅ Session WA sudah aktif untuk nomor `{linked_phone}`.\n\n"
            f"Jika ingin pairing ulang, gunakan /unpair terlebih dahulu.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── Tampilkan nomor yang akan di-pair ───────────────────────────────────
    display_phone = f"+{phone}"
    msg = await update.message.reply_text(
        f"🔗 Meminta kode pairing untuk nomor `{display_phone}`\\.\\.\\.\n"
        f"_\\(tunggu 5–20 detik\\)_",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

    # ── Jalankan proses pairing ─────────────────────────────────────────────
    process      = start_pairing_process(phone)
    pairing_code = None
    error_msg    = None
    already_linked = False

    try:
        # Baca stdout line-by-line selama max 30 detik
        deadline = time.time() + 30
        while time.time() < deadline:
            line = await loop.run_in_executor(None, process.stdout.readline)
            if not line:
                break
            line = line.strip()
            if not line:
                continue
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue

            if "pairing_code" in d:
                pairing_code = d["pairing_code"]
                break
            if "already_linked" in d:
                already_linked = True
                linked_phone   = d.get("phone", phone)
                break
            if "error" in d:
                error_msg = d["error"]
                break

    except Exception as e:
        error_msg = str(e)
    finally:
        try:
            process.stdout.close()
        except Exception:
            pass

    # ── Tangani hasil ───────────────────────────────────────────────────────

    if already_linked:
        await msg.edit_text(
            f"✅ *WhatsApp sudah terhubung\\!*\n"
            f"📱 Nomor: `{linked_phone}`\n\n"
            f"Jika ingin pairing ulang, gunakan /unpair dulu\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return

    if error_msg:
        # Bersihkan karakter markdown berbahaya untuk pesan error
        safe_err = re.sub(r"[_*\[\]()~`>#+=|{}.!\\-]", r"\\\g<0>", str(error_msg))
        await msg.edit_text(
            f"❌ *Gagal mendapatkan kode pairing:*\n`{error_msg}`\n\n"
            f"Kemungkinan penyebab:\n"
            f"• Nomor tidak valid atau belum punya WA\n"
            f"• Koneksi internet bermasalah\n"
            f"• Node.js belum install: `cd node_helper && npm install`\n\n"
            f"Coba lagi beberapa detik kemudian.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    if not pairing_code:
        await msg.edit_text(
            "❌ Kode pairing tidak diterima dalam batas waktu.\n\n"
            "Kemungkinan penyebab:\n"
            "• Nomor sudah terhubung di perangkat lain\n"
            "• Koneksi internet lambat\n"
            "• `node_helper/node_modules` belum ada\n\n"
            "Coba `/pair` lagi dalam beberapa detik.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    # ── Tampilkan kode pairing ─────────────────────────────────────────────
    # Pastikan format XXXX-XXXX
    code_clean = re.sub(r"\D", "", pairing_code)
    if len(code_clean) == 8:
        display_code = f"{code_clean[:4]}-{code_clean[4:]}"
    else:
        display_code = pairing_code  # tampilkan apa adanya

    await msg.edit_text(
        f"🔑 *Kode Pairing WhatsApp*\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"📱 Nomor: `{display_phone}`\n\n"
        f"```\n{display_code}\n```\n\n"
        f"*Langkah di HP kamu:*\n"
        f"1\\. Buka WhatsApp\n"
        f"2\\. ⚙️ *Setelan* → *Perangkat Tertaut*\n"
        f"3\\. Ketuk *Tautkan Perangkat*\n"
        f"4\\. Pilih *Tautkan dengan nomor telepon*\n"
        f"5\\. Masukkan kode di atas\n\n"
        f"⚠️ _Kode berlaku ±60 detik\\. Segera masukkan\\!_",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    logger.info("Pairing code dikirim untuk nomor %s oleh user %s", display_phone, user.id)


# ─── /unpair ──────────────────────────────────────────────────────────────────

async def cmd_unpair(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_authorized(user.id):
        await update.message.reply_text("🚫 Tidak punya akses.")
        return

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Ya, putuskan", callback_data="confirm_unpair"),
            InlineKeyboardButton("❌ Batal", callback_data="cancel_unpair"),
        ]
    ])
    await update.message.reply_text(
        "⚠️ *Yakin ingin memutuskan koneksi WhatsApp?*\n\n"
        "Session akan dihapus dan bot tidak bisa cek nomor sampai di-/pair ulang.",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN,
    )


async def callback_unpair(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user  = query.from_user

    if not is_authorized(user.id):
        await query.edit_message_text("🚫 Tidak punya akses.")
        return

    if query.data == "cancel_unpair":
        await query.edit_message_text("✅ Dibatalkan. Koneksi WA tetap aktif.")
        return

    if query.data == "confirm_unpair":
        await query.edit_message_text("🔄 Memutuskan koneksi WA...")
        loop   = asyncio.get_event_loop()
        result = await loop.run_in_executor(None, run_node_unpair)
        if "_error" in result:
            await query.edit_message_text(
                f"❌ Gagal memutuskan: {result['_error']}"
            )
        else:
            await query.edit_message_text(
                "✅ *Koneksi WA berhasil diputuskan.*\n\n"
                "Gunakan /pair untuk menghubungkan kembali.",
                parse_mode=ParseMode.MARKDOWN,
            )
            logger.info("User %s melakukan unpair", user.id)

# ─── Admin Commands ───────────────────────────────────────────────────────────

async def cmd_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("🚫 Hanya admin yang bisa akses panel ini.")
        return

    history       = load_history()
    allowed       = load_allowed_ids()
    total_users   = len(history)
    total_checks  = sum(len(v) for v in history.values())
    total_numbers = sum(e["count"] for entries in history.values() for e in entries)

    teks = (
        "🛠 *Panel Admin*\n"
        "━━━━━━━━━━━━━━━━━━━━\n\n"
        f"👥 Total user (pernah cek): `{total_users}`\n"
        f"🔢 Total sesi cek          : `{total_checks}`\n"
        f"📱 Total nomor dicek       : `{total_numbers}`\n"
        f"🔑 User diizinkan          : `{len(allowed) if allowed else 'semua (terbuka)'}`\n\n"
        f"⚙️ *Konfigurasi Aktif:*\n"
        f"• Max nomor/request : `{MAX_PER_REQUEST}`\n"
        f"• Rate limit window : `{RATE_LIMIT_WINDOW}s`\n"
        f"• Rate limit max    : `{RATE_LIMIT_MAX_REQ}` req\n"
        f"• Node timeout      : `{NODE_CHECK_TIMEOUT}s`\n\n"
        f"*Perintah Admin:*\n"
        f"• /adduser `<id>` — tambah user\n"
        f"• /removeuser `<id>` — hapus user\n"
        f"• /listuser — lihat semua user\n"
        f"• /broadcast `<pesan>` — kirim ke semua\n"
    )
    await update.message.reply_text(teks, parse_mode=ParseMode.MARKDOWN)


async def cmd_adduser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("🚫 Hanya admin.")
        return

    args = update.message.text.split()[1:]
    if not args:
        await update.message.reply_text(
            "Penggunaan: `/adduser <user_id>`\nContoh: `/adduser 123456789`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    added = []
    for uid in args:
        uid = uid.strip()
        if not uid.isdigit():
            continue
        ids = load_allowed_ids()
        ids.add(uid)
        save_dynamic_allowed(ids)
        added.append(uid)

    if added:
        await update.message.reply_text(
            f"✅ User berhasil ditambahkan: `{'`, `'.join(added)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("⚠️ Tidak ada ID valid yang diberikan.")


async def cmd_removeuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("🚫 Hanya admin.")
        return

    args = update.message.text.split()[1:]
    if not args:
        await update.message.reply_text(
            "Penggunaan: `/removeuser <user_id>`\nContoh: `/removeuser 123456789`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    removed = []
    for uid in args:
        uid = uid.strip()
        if not uid.isdigit():
            continue
        if uid == ADMIN_USER_ID:
            await update.message.reply_text("⚠️ Tidak bisa hapus akun admin.")
            continue
        ids = load_allowed_ids()
        ids.discard(uid)
        save_dynamic_allowed(ids)
        removed.append(uid)

    if removed:
        await update.message.reply_text(
            f"✅ User dihapus: `{'`, `'.join(removed)}`",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text("⚠️ Tidak ada ID valid yang diberikan.")


async def cmd_listuser(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("🚫 Hanya admin.")
        return

    ids     = load_allowed_ids()
    history = load_history()

    if not ids:
        await update.message.reply_text("ℹ️ Bot dalam mode terbuka — semua user bisa pakai.")
        return

    lines = [f"👥 *Daftar User ({len(ids)} orang):*\n"]
    for uid in sorted(ids):
        entry_count = len(history.get(uid, []))
        tag = " 👑 Admin" if uid == ADMIN_USER_ID else ""
        lines.append(f"• `{uid}`{tag} — {entry_count} sesi cek")

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN)


async def cmd_broadcast(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not is_admin(user.id):
        await update.message.reply_text("🚫 Hanya admin.")
        return

    pesan = update.message.text.replace("/broadcast", "", 1).strip()
    if not pesan:
        await update.message.reply_text(
            "Penggunaan: `/broadcast <pesan>`\n\n"
            "Pesan akan dikirim ke semua user yang pernah menggunakan bot.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    history  = load_history()
    user_ids = list(history.keys())

    if not user_ids:
        await update.message.reply_text("⚠️ Belum ada user yang menggunakan bot.")
        return

    msg = await update.message.reply_text(
        f"📡 Mengirim pesan ke *{len(user_ids)}* user...",
        parse_mode=ParseMode.MARKDOWN,
    )

    sukses, gagal   = 0, 0
    broadcast_text  = f"📢 *Pesan dari Admin:*\n\n{pesan}"

    for uid in user_ids:
        try:
            await ctx.application.bot.send_message(
                chat_id=int(uid),
                text=broadcast_text,
                parse_mode=ParseMode.MARKDOWN,
            )
            sukses += 1
        except TelegramError:
            gagal += 1
        await asyncio.sleep(0.05)

    await msg.edit_text(
        f"✅ Broadcast selesai!\n"
        f"• Terkirim : `{sukses}`\n"
        f"• Gagal    : `{gagal}`",
        parse_mode=ParseMode.MARKDOWN,
    )

# ─── Handler Teks Tidak Dikenal ──────────────────────────────────────────────

async def handle_unknown_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("waiting_file"):
        await update.message.reply_text(
            "📂 Saya menunggu file dari kamu.\n"
            "Kirim file `.txt` atau `.csv` sekarang, atau ketik /cek untuk mode manual.",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    text    = update.message.text or ""
    numbers, _ = parse_numbers_from_text(text)
    if numbers and len(numbers) <= 3:
        await update.message.reply_text(
            f"💡 Sepertinya kamu ingin cek nomor?\n"
            f"Gunakan perintah `/cek {text.strip()}`",
            parse_mode=ParseMode.MARKDOWN,
        )
        return

    await update.message.reply_text(
        "❓ Perintah tidak dikenal.\n"
        "Ketik /help untuk daftar perintah lengkap."
    )

# ─── Entrypoint ───────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN belum diisi di .env")
        sys.exit(1)

    if not OWNER_PHONE:
        logger.warning(
            "OWNER_PHONE_NUMBER belum diisi di .env. "
            "Fitur /pair hanya bisa pakai nomor argumen: /pair +62xxx"
        )

    if not _STATIC_IDS and not ALLOWED_FILE.exists():
        logger.warning(
            "ALLOWED_USER_IDS kosong — semua orang bisa pakai bot ini. "
            "Sangat disarankan diisi untuk keamanan."
        )

    if not NODE_HELPER.exists():
        logger.warning(
            "wa_helper.js tidak ditemukan di %s. "
            "Jalankan: cd node_helper && npm install",
            NODE_HELPER,
        )

    app = Application.builder().token(BOT_TOKEN).build()

    # ── Command handlers ──
    app.add_handler(CommandHandler("start",       cmd_start))
    app.add_handler(CommandHandler("help",        cmd_help))
    app.add_handler(CommandHandler("cek",         cmd_cek))
    app.add_handler(CommandHandler("file",        cmd_file))
    app.add_handler(CommandHandler("riwayat",     cmd_riwayat))
    app.add_handler(CommandHandler("status",      cmd_status))
    app.add_handler(CommandHandler("pair",        cmd_pair))
    app.add_handler(CommandHandler("unpair",      cmd_unpair))
    app.add_handler(CommandHandler("admin",       cmd_admin))
    app.add_handler(CommandHandler("adduser",     cmd_adduser))
    app.add_handler(CommandHandler("removeuser",  cmd_removeuser))
    app.add_handler(CommandHandler("listuser",    cmd_listuser))
    app.add_handler(CommandHandler("broadcast",   cmd_broadcast))

    # ── Callback & document ──
    app.add_handler(CallbackQueryHandler(
        callback_unpair, pattern="^(confirm|cancel)_unpair$"
    ))
    app.add_handler(MessageHandler(filters.Document.ALL,            handle_document))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_unknown_text))

    logger.info("🤖 WA Checker Bot berjalan. Tekan CTRL+C untuk berhenti.")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
