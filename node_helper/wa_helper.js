/**
 * wa_helper.js — WhatsApp Helper (Baileys)
 * ==========================================
 * Commands:
 *   node wa_helper.js pair <phone>   → get pairing code
 *   node wa_helper.js check <nums>   → check if numbers are on WA
 *   node wa_helper.js status         → check if session is linked
 *   node wa_helper.js unpair         → logout and delete session
 *
 * Output: JSON lines to stdout
 * Install: npm install @whiskeysockets/baileys@latest qrcode-terminal pino
 */

const {
  default: makeWASocket,
  useMultiFileAuthState,
  DisconnectReason,
  makeCacheableSignalKeyStore,
  fetchLatestBaileysVersion,
} = require("@whiskeysockets/baileys");
const { Boom } = require("@hapi/boom");
const pino = require("pino");
const path = require("path");
const fs = require("fs");

// WA_SESSION_DIR dapat di-override via env (untuk volume persisten di Railway/Render)
const SESSION_DIR = process.env.WA_SESSION_DIR
  ? path.resolve(process.env.WA_SESSION_DIR)
  : path.join(__dirname, "wa_session");
const SILENT_LOGGER = pino({ level: "silent" });

// ── JSON output helpers ────────────────────────────────────────────────────

function out(obj) {
  process.stdout.write(JSON.stringify(obj) + "\n");
}

function outError(msg) {
  out({ error: msg });
  process.exit(1);
}

// ── Ensure session directory exists ────────────────────────────────────────

if (!fs.existsSync(SESSION_DIR)) {
  fs.mkdirSync(SESSION_DIR, { recursive: true });
}

// ── Main ───────────────────────────────────────────────────────────────────

const [, , command, arg] = process.argv;

if (!command) {
  outError("No command provided. Use: pair | check | status | unpair");
}

(async () => {
  switch (command.toLowerCase()) {
    case "pair":
      await doPair(arg);
      break;
    case "check":
      await doCheck(arg);
      break;
    case "status":
      await doStatus();
      break;
    case "unpair":
      await doUnpair();
      break;
    default:
      outError(`Unknown command: ${command}`);
  }
})();

// ─────────────────────────────────────────────────────────────────────────────
// PAIR
// ─────────────────────────────────────────────────────────────────────────────

async function doPair(rawPhone) {
  if (!rawPhone) outError("phone number required for pair");

  // Normalize: strip all non-digits, ensure starts with country code
  let phone = rawPhone.replace(/\D/g, "");
  if (phone.startsWith("0")) phone = "62" + phone.slice(1);
  if (!phone.startsWith("62")) phone = "62" + phone;

  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);
  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    logger: SILENT_LOGGER,
    printQRInTerminal: false,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, SILENT_LOGGER),
    },
    browser: ["WA Checker Bot", "Chrome", "120.0.0"],
    connectTimeoutMs: 30_000,
    defaultQueryTimeoutMs: 30_000,
  });

  sock.ev.on("creds.update", saveCreds);

  // Wait until registration state is ready
  await new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Timeout waiting for socket ready")), 20000);

    sock.ev.on("connection.update", async (update) => {
      const { connection, lastDisconnect, qr } = update;

      if (connection === "open") {
        clearTimeout(timer);
        // Already linked — just report status
        const info = sock.user;
        out({
          already_linked: true,
          phone: info?.id?.split(":")[0] || phone,
          name: info?.name || "",
        });
        await sock.end();
        resolve();
        return;
      }

      if (connection === "close") {
        clearTimeout(timer);
        const code = lastDisconnect?.error?.output?.statusCode;
        if (code !== DisconnectReason.loggedOut) {
          // Not an error — just closed after pairing code sent
          resolve();
        } else {
          reject(new Error("Logged out"));
        }
        return;
      }

      // Request pairing code once socket is ready (no QR received)
      if (!qr && !sock.authState.creds.registered) {
        try {
          clearTimeout(timer);
          const code = await sock.requestPairingCode(phone);
          // Format code as XXXX-XXXX
          const formatted = code.match(/.{1,4}/g)?.join("-") || code;
          out({ pairing_code: formatted, phone });
          // Keep process alive briefly so caller can read code
          setTimeout(async () => {
            try { await sock.end(); } catch (_) {}
            process.exit(0);
          }, 3000);
          resolve();
        } catch (err) {
          reject(err);
        }
      }
    });
  }).catch((err) => {
    outError(err.message || "Failed to get pairing code");
  });
}

// ─────────────────────────────────────────────────────────────────────────────
// CHECK
// ─────────────────────────────────────────────────────────────────────────────

async function doCheck(numsArg) {
  if (!numsArg) outError("numbers required for check");

  const numbers = numsArg
    .split(",")
    .map((n) => n.trim())
    .filter(Boolean);

  if (numbers.length === 0) outError("no numbers provided");

  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);

  if (!state.creds.registered) {
    outError("WhatsApp not paired. Use /pair first.");
  }

  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    logger: SILENT_LOGGER,
    printQRInTerminal: false,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, SILENT_LOGGER),
    },
    browser: ["WA Checker Bot", "Chrome", "120.0.0"],
    connectTimeoutMs: 30_000,
    defaultQueryTimeoutMs: 60_000,
  });

  sock.ev.on("creds.update", saveCreds);

  await waitForConnection(sock);

  const results = {};

  try {
    // onWhatsApp accepts array of JIDs
    const jids = numbers.map((n) => n + "@s.whatsapp.net");
    const response = await sock.onWhatsApp(...jids);

    // response is array of { exists, jid }
    for (const item of response) {
      const num = item.jid.replace("@s.whatsapp.net", "");
      results[num] = { registered: item.exists };
    }

    // Mark any number not returned as not registered
    for (const n of numbers) {
      if (!(n in results)) {
        results[n] = { registered: false };
      }
    }
  } catch (err) {
    await sock.end();
    outError("Check failed: " + err.message);
    return;
  }

  await sock.end();
  out(results);
}

// ─────────────────────────────────────────────────────────────────────────────
// STATUS
// ─────────────────────────────────────────────────────────────────────────────

async function doStatus() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);

  if (!state.creds.registered) {
    out({ linked: false });
    return;
  }

  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    logger: SILENT_LOGGER,
    printQRInTerminal: false,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, SILENT_LOGGER),
    },
    connectTimeoutMs: 15_000,
    defaultQueryTimeoutMs: 15_000,
  });

  sock.ev.on("creds.update", saveCreds);

  try {
    await waitForConnection(sock, 12000);
    const info = sock.user;
    out({
      linked: true,
      phone: info?.id?.split(":")[0] || "",
      name: info?.name || "",
    });
    await sock.end();
  } catch (_) {
    try { await sock.end(); } catch (__) {}
    out({ linked: false });
  }
}

// ─────────────────────────────────────────────────────────────────────────────
// UNPAIR
// ─────────────────────────────────────────────────────────────────────────────

async function doUnpair() {
  const { state, saveCreds } = await useMultiFileAuthState(SESSION_DIR);

  if (!state.creds.registered) {
    // Just wipe session folder
    clearSession();
    out({ success: true, message: "Session cleared (was not linked)." });
    return;
  }

  const { version } = await fetchLatestBaileysVersion();

  const sock = makeWASocket({
    version,
    logger: SILENT_LOGGER,
    printQRInTerminal: false,
    auth: {
      creds: state.creds,
      keys: makeCacheableSignalKeyStore(state.keys, SILENT_LOGGER),
    },
    connectTimeoutMs: 15_000,
  });

  sock.ev.on("creds.update", saveCreds);

  try {
    await waitForConnection(sock, 12000);
    await sock.logout();
  } catch (_) {}

  clearSession();
  out({ success: true, message: "Logged out and session deleted." });
}

// ─────────────────────────────────────────────────────────────────────────────
// Helpers
// ─────────────────────────────────────────────────────────────────────────────

function clearSession() {
  try {
    const files = fs.readdirSync(SESSION_DIR);
    for (const f of files) {
      fs.rmSync(path.join(SESSION_DIR, f), { recursive: true, force: true });
    }
  } catch (_) {}
}

function waitForConnection(sock, timeout = 20000) {
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => reject(new Error("Connection timeout")), timeout);

    sock.ev.on("connection.update", (update) => {
      const { connection, lastDisconnect } = update;
      if (connection === "open") {
        clearTimeout(timer);
        resolve();
      } else if (connection === "close") {
        clearTimeout(timer);
        const reason = lastDisconnect?.error?.output?.statusCode;
        if (reason === DisconnectReason.loggedOut) {
          clearSession();
          reject(new Error("Session logged out. Please /pair again."));
        } else {
          reject(new Error("Connection closed: " + reason));
        }
      }
    });
  });
}
