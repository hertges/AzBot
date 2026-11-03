import re
from dotenv import load_dotenv
load_dotenv()

import os

# ── Telegram / Pyrogram credentials ─────────────────────────────────────
# API_ID / API_HASH come from https://my.telegram.org (needed by Pyrogram
# even when running as a bot — this is what lets us talk MTProto directly
# instead of the 50MB-limited HTTP Bot API).
API_ID   = int(os.environ.get("API_ID", "0") or 0)
API_HASH = os.environ.get("API_HASH", "").strip()
BOT_TOKEN = os.environ.get("BOT_TOKEN", os.environ.get("TELEGRAM_TOKEN", "")).strip()

ADMIN_ID  = os.environ.get("ADMIN_ID", "").strip()

# ── Google Drive (optional secondary destination) ──────────────────────
DRIVE_FOLDER_ID   = os.environ.get("DRIVE_FOLDER_ID", "").strip()

def _parse_drive_accounts():
    """Multi-account Drive support. Two env styles are accepted:

    1. Single account (backwards compatible):
         GCP_CLIENT_ID=... GCP_CLIENT_SECRET=... GCP_REFRESH_TOKEN=...
       → one account named "main".

    2. Multiple accounts, numbered:
         GDrive_1_CLIENT_ID / GDrive_1_CLIENT_SECRET / GDrive_1_REFRESH_TOKEN
         GDrive_2_CLIENT_ID / ...
       (any suffix works: GDrive_work_..., GDrive_2_...). Names come from the
       suffix — "work", "2" — so /settings can show them by name.
    """
    accounts = {}
    single = (os.environ.get("GCP_CLIENT_ID", "").strip(),
              os.environ.get("GCP_CLIENT_SECRET", "").strip(),
              os.environ.get("GCP_REFRESH_TOKEN", "").strip())
    if all(single):
        accounts["main"] = {"client_id": single[0], "client_secret": single[1],
                            "refresh_token": single[2]}
    for key, val in os.environ.items():
        m = re.match(r"^GDrive_(.+?)_(CLIENT_ID|CLIENT_SECRET|REFRESH_TOKEN)$", key,
                     re.IGNORECASE)
        if not m:
            continue
        name, field = m.group(1).strip().lower(), m.group(2).upper()
        accounts.setdefault(name, {"client_id": "", "client_secret": "", "refresh_token": ""})
        accounts[name][{"CLIENT_ID": "client_id", "CLIENT_SECRET": "client_secret",
                        "REFRESH_TOKEN": "refresh_token"}[field]] = val.strip()
    return {n: a for n, a in accounts.items() if all(a.values())}

DRIVE_ACCOUNTS = _parse_drive_accounts()

if not (API_ID and API_HASH and BOT_TOKEN):
    print("CRITICAL: API_ID / API_HASH / BOT_TOKEN missing! See .env.example")
    exit(1)

_BASE_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOWNLOAD_DIR = os.path.join(_BASE_DIR, "downloads")
DATA_DIR     = os.path.join(_BASE_DIR, "data")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)
os.makedirs(DATA_DIR, exist_ok=True)

SETTINGS_FILE = os.path.join(DATA_DIR, "settings.json")
USERS_FILE    = os.path.join(DATA_DIR, "users.json")
COOKIES_DIR   = os.path.join(DATA_DIR, "cookies")
os.makedirs(COOKIES_DIR, exist_ok=True)
# Per-user, per-type Instagram index ledgers (data/indexes/<user>.<type>.txt)
INDEX_DIR     = os.path.join(DATA_DIR, "indexes")
os.makedirs(INDEX_DIR, exist_ok=True)

MIN_FREE_MB = 300

# Telegram's hard per-file cap for bot uploads over MTProto (what Pyrogram
# uses). This is the real ceiling regardless of client library — there is
# no "native" way past it without switching to a user account.
TG_MAX_FILE_BYTES = 2 * 1000 * 1000 * 1000  # ~2GB

# Concurrent per-job upload workers (LiveDispatcher). Default 1 on sub-512MB
# containers; raise via env (UPLOAD_WORKERS) if you have headroom.
UPLOAD_WORKERS = int(os.environ.get("UPLOAD_WORKERS", "1") or 1)

# Concurrent DRIVE uploads, process-wide. Default 1 — per-thread service
# objects removed the shared-transport corruption, but 2 concurrent Drive
# uploads still double chunk-buffer RAM on a tight box.
DRIVE_UPLOAD_CONCURRENCY = int(os.environ.get("DRIVE_UPLOAD_CONCURRENCY", "1") or 1)
# Concurrent job workers (each = one download+upload pipeline). 1 keeps the
# bot responsive while halving worst-case concurrent subprocess/buffer load
# vs the old hardcoded 4 — raise via env if you serve many chats at once.
QUEUE_WORKERS = int(os.environ.get("QUEUE_WORKERS", "1") or 1)
# asyncio.to_thread pool for message/callback handlers (see utils.guarded).
# Must comfortably exceed the number of simultaneously-slow things a handler
# can legally do (yt-dlp probes, Drive listings, ffprobe) so taps and
# commands never queue behind them. Default 6 on sub-512MB boxes.
# BUG (crash on deploy): a blank env var (e.g. HANDLER_WORKERS= copied
# verbatim from .env.example) makes os.environ.get(name, default) return ""
# — the EMPTY STRING, not the default — since .get()'s default only applies
# when the key is absent, not when it's present-but-blank. int("") then
# raises ValueError and kills the bot before it can even start. Same fix as
# API_ID above: `or <default>` catches "" too, since empty string is falsy.
HANDLER_WORKERS = int(os.environ.get("HANDLER_WORKERS", "6") or 6)

# ── Link classification ─────────────────────────────────────────────────
# Anything on this list, pasted with no command, gets auto-leeched.
# yt-dlp itself supports ~1800 sites; these are just the hosts we treat as
# "obviously a media link" for auto-detection so we don't try to leech
# every bare URL someone pastes in chat.
YTDLP_AUTO_HOSTS = (
    "youtube.com", "youtu.be", "music.youtube.com",
    "instagram.com",
    "tiktok.com",
    "twitter.com", "x.com",
    "facebook.com", "fb.watch",
    "reddit.com", "redd.it",
    "vimeo.com", "dailymotion.com", "twitch.tv", "clips.twitch.tv",
    "soundcloud.com", "bilibili.com", "nicovideo.jp",
    "streamable.com", "rumble.com", "odysee.com",
    "pinterest.com", "pin.it",
    "threads.net",
    "snapchat.com",
)

DIRECT_FILE_EXT = (
    ".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v", ".ts",
    ".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus",
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
    ".bin", ".iso", ".pdf", ".apk", ".exe", ".deb", ".rpm", ".epub",
)

URL_RE = re.compile(r"https?://\S+")
MAGNET_RE = re.compile(r"magnet:\?xt=urn:btih:[A-Za-z0-9]+\S*")

DEST_LABEL = {"telegram": "📱 Telegram", "drive": "☁️ Drive"}

IG_HOSTS = ("instagram.com",)

SHELL_TIMEOUT_S = 60

# If a subprocess job (torrent/gallery/clone) produces no output at all for
# this long, treat it as stalled and stop it rather than hang forever —
# e.g. a torrent with zero peers, or a site blocking gallery-dl silently.
# 10 minutes: Instagram rate-limit waits routinely sit silent for several
# minutes and ARE still making progress (v15-v17 killed healthy archives
# at 4 minutes, which looked like "stuck in a certain step").
STALL_TIMEOUT_S = 600

# Optional raw yt-dlp option overrides, as a JSON object, e.g. in .env:
#   YT_DLP_OPTIONS={"writesubtitles": true, "subtitleslangs": ["en"]}
# Same idea as WZML-X's YT_DLP_OPTIONS — lets an admin tweak/extend yt-dlp
# behavior without touching code. Merged on top of the bot's own defaults,
# so it can override anything (format selection included) if needed.
import json as _json
_YT_DLP_OPTIONS_RAW = os.environ.get("YT_DLP_OPTIONS", "").strip()
try:
    YT_DLP_OPTIONS = _json.loads(_YT_DLP_OPTIONS_RAW) if _YT_DLP_OPTIONS_RAW else {}
    if not isinstance(YT_DLP_OPTIONS, dict):
        raise ValueError("YT_DLP_OPTIONS must be a JSON object")
except (ValueError, _json.JSONDecodeError) as e:
    print(f"WARNING: couldn't parse YT_DLP_OPTIONS ({e}) — ignoring it")
    YT_DLP_OPTIONS = {}

# ── Logging ──────────────────────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
LOG_DIR = os.path.join(DATA_DIR, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "bot.log")

