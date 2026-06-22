#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────
# WA Checker Bot — Script start all-in-one
# Menyiapkan dependensi (Python + Node) lalu menjalankan bot.
# ─────────────────────────────────────────────────────────────
set -e

cd "$(dirname "$0")"

echo "==> [1/4] Cek file .env"
if [ ! -f .env ]; then
  echo "    .env belum ada. Menyalin dari .env.example..."
  cp .env.example .env
  echo "    >> Edit file .env dan isi TELEGRAM_BOT_TOKEN, lalu jalankan lagi."
  exit 1
fi

echo "==> [2/4] Siapkan virtualenv & dependensi Python"
if command -v uv >/dev/null 2>&1; then
  [ -d .venv ] || uv venv .venv
  uv pip install --python .venv/bin/python -r requirements.txt
else
  [ -d .venv ] || python3 -m venv .venv
  ./.venv/bin/pip install -r requirements.txt
fi

echo "==> [3/4] Install dependensi Node (Baileys)"
cd node_helper
[ -d node_modules ] || npm install
cd ..

echo "==> [4/4] Menjalankan bot"
exec ./.venv/bin/python bot.py
