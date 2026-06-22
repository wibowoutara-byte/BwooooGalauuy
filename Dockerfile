# ── WA Checker Bot — Python (Telegram) + Node (Baileys/WhatsApp) ──────────────
# Satu container menjalankan bot.py yang men-spawn node_helper/wa_helper.js.

FROM python:3.13-slim

# Install Node.js 20 LTS + dependency sistem
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates gnupg \
    && mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install dependency Node (di-cache jika package.json tidak berubah)
COPY node_helper/package.json node_helper/package-lock.json* ./node_helper/
RUN cd node_helper && npm install --omit=dev

# Install dependency Python
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Salin sisa kode
COPY . .

# Path persisten (diarahkan ke volume oleh Railway/Render)
ENV DATA_DIR=/data \
    WA_SESSION_DIR=/data/wa_session \
    PYTHONUNBUFFERED=1

RUN mkdir -p /data/wa_session

CMD ["python", "bot.py"]
