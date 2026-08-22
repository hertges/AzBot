import math, time, threading, os, zipfile, uuid, html as _html

from pyrogram.errors import FloodWait, MessageNotModified
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from . import log

logger = log.get(__name__)

def esc(s):
    """HTML-escape dynamic content (& < >) before embedding it in any
    message/caption. The bot runs with parse_mode=HTML globally (the WZML
    approach): unlike markdown, HTML entities have deterministic bounds —
    arbitrary filenames/URLs/log lines can never corrupt the entity table
    (the source of Telegram's ENTITY_BOUNDS_INVALID crashes). Only & <>
    need escaping; markdown metacharacters are literal in HTML mode."""
    return _html.escape(str(s), quote=False)

def cancel_kb(task_id):
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛑 Stop", callback_data=f"cancel:{task_id}")]])

def safe_edit(client_or_query, *args, **kwargs):
    """Like edit_message_text, but treats MESSAGE_NOT_MODIFIED as success —
    it only means the content you wanted is already on screen, which isn't
    an error a user should ever see reported as one."""
    try:
        return client_or_query.edit_message_text(*args, **kwargs)
    except MessageNotModified:
        return None

def safe_delete(client, chat_id, *msg_ids):
    """Best-effort message deletion for the auto-clean setting — missing
    permissions, already-deleted messages, etc. should never break a job."""
    ids = [m for m in dict.fromkeys(msg_ids) if m]  # dedupe, drop None/0
    if not ids:
        return
    try:
        client.delete_messages(chat_id, ids)
    except Exception as e:
        logger.debug(f"auto-clean delete failed for {ids} in {chat_id}: {e}")

def autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest):
    """Called only from a job's success path. Deletes the user's triggering
    message, and the bot's own status message too — unless dest is
    drive-only, in which case that status message is the only visible
    record of the result (it holds the Drive link) and is kept."""
    from . import state
    if not state.get_autoclean(chat_id):
        return
    if dest == "drive":
        safe_delete(client, chat_id, reply_to)
    else:
        safe_delete(client, chat_id, msg_id, reply_to)

_seen_updates = {}
_seen_lock = threading.Lock()
_DEDUP_TTL = 8  # seconds

def _dedup_key(update):
    """Unique-enough key for an incoming Message or CallbackQuery, used to
    detect the same update being handed to us twice."""
    try:
        if hasattr(update, "data"):  # CallbackQuery
            return ("cb", update.id)
        return ("msg", update.chat.id, update.id)  # Message
    except Exception:
        return None

def _mark_if_new(key):
    """Returns True the first time `key` is seen within the TTL window,
    False on a repeat. Also does a cheap opportunistic cleanup so this
    dict doesn't grow forever."""
    now = time.time()
    with _seen_lock:
        if len(_seen_updates) > 2000:
            for k, t in list(_seen_updates.items()):
                if now - t > _DEDUP_TTL:
                    del _seen_updates[k]
        last = _seen_updates.get(key)
        if last is not None and now - last < _DEDUP_TTL:
            return False
        _seen_updates[key] = now
        return True

def guarded(fn):
    """Wraps a Pyrogram message/callback handler two ways:

    1. Any exception the handler doesn't catch itself gets reported into
       the chat instead of silently vanishing into the logs — Pyrogram's
       dispatcher just logs handler exceptions and moves on, which from
       the user's side looks like the bot ignored them.
    2. Defends against the same update being processed twice — e.g. two
       bot instances polling the same token after a redeploy that didn't
       cleanly stop the old process. If we've seen this exact
       message/callback within the last few seconds, skip it silently."""
    import functools

    @functools.wraps(fn)
    def wrapper(client, update, *a, **kw):
        key = _dedup_key(update)
        if key and not _mark_if_new(key):
            logger.warning(f"duplicate update suppressed in {fn.__name__}: {key}")
            return
        try:
            return fn(client, update, *a, **kw)
        except Exception as e:
            logger.exception(f"handler {fn.__name__} raised")
            try:
                chat_id = update.message.chat.id if hasattr(update, "message") and update.message else update.chat.id
            except Exception:
                chat_id = None
            if chat_id is not None:
                try:
                    client.send_message(chat_id, f"❌ Something went wrong (`{fn.__name__}`):\n`{str(e)[:300]}`")
                except Exception:
                    logger.warning(f"couldn't even report error for {fn.__name__} to chat {chat_id}")
            if hasattr(update, "answer"):  # CallbackQuery — clear the loading spinner
                try:
                    update.answer("❌ Error — see chat for details", show_alert=True)
                except Exception:
                    pass
    return wrapper

def fmtsz(b):
    if not b or b <= 0:
        return "0 B"
    n = ("B", "KB", "MB", "GB", "TB")
    i = min(int(math.floor(math.log(b, 1024))), 4)
    return f"{round(b / math.pow(1024, i), 2)} {n[i]}"

def fmt_time(s):
    s = int(max(0, s)); m, s = divmod(s, 60); h, m = divmod(m, 60)
    if h: return f"{h}h {m}m {s}s"
    if m: return f"{m}m {s}s"
    return f"{s}s"

def bar(done, total, L=14):
    if total <= 0:
        return "[" + "░" * L + "] 0.0%"
    pct = min(100.0, 100 * done / total)
    f = int(L * pct / 100)
    blocks = "█" * f + "░" * (L - f)
    return f"[{blocks}] {pct:.1f}%"

def smooth_speed(samples):
    if len(samples) < 2:
        return "…"
    dt = samples[-1][1] - samples[0][1]
    if dt <= 0:
        return "…"
    return fmtsz(max(0, (samples[-1][0] - samples[0][0]) / dt)) + "/s"

# ── Throttled status-message editing ─────────────────────────────────────
# Telegram allows ~1 edit/sec per chat; we coalesce rapid progress updates
# so download/upload progress hooks (which fire many times a second) don't
# get us flood-limited.

_lock = threading.Lock()
_last_edit = {}
_pending = {}
_timers = {}
MIN_GAP = 2.5

def throttled_edit(client, chat_id, msg_id, text, markup=None, force=False):
    if not msg_id:
        return
    key = (chat_id, msg_id)

    def _do():
        with _lock:
            pending = _pending.pop(key, None)
            _timers.pop(key, None)
            if pending is None:
                return
            _text, _markup = pending
            _last_edit[key] = time.time()
        try:
            client.edit_message_text(chat_id, msg_id, _text, reply_markup=_markup)
        except FloodWait as e:
            time.sleep(e.value + 1)
            try:
                client.edit_message_text(chat_id, msg_id, _text, reply_markup=_markup)
            except Exception:
                pass
        except MessageNotModified:
            pass
        except Exception as e:
            # Don't let a failed cosmetic edit vanish silently — ENTITY_BOUNDS /
            # entity-parse errors used to surface here as bare "[edit] ..." prints.
            logger.warning(f"edit_message_text failed for ({chat_id}, {msg_id}): {type(e).__name__}: {e}")

    with _lock:
        now = time.time()
        gap = now - _last_edit.get(key, 0)
        _pending[key] = (text, markup)
        existing = _timers.get(key)
        if existing:
            existing.cancel()
        delay = 0.05 if (force or gap >= MIN_GAP) else (MIN_GAP - gap + 0.05)
        t = threading.Timer(delay, _do)
        t.daemon = True
        _timers[key] = t
        t.start()

def safe_send(client, chat_id, text, **kwargs):
    try:
        return client.send_message(chat_id, text, **kwargs)
    except FloodWait as e:
        time.sleep(e.value + 1)
        return client.send_message(chat_id, text, **kwargs)

def new_task_id():
    return str(uuid.uuid4())[:8]

# Extensions yt-dlp/gallery-dl sometimes fall back to when they can't
# positively identify a container — worth re-checking via magic bytes so
# the file doesn't end up captioned/uploaded as "*.unknown_video".
_JUNK_EXTS = {"unknown_video", "unknown_audio", "unknown_video_or_audio", "unknown", "na", "bin", ""}

_MAGIC_SNIFFERS = (
    (lambda h: h[4:8] == b"ftyp", "mp4"),
    (lambda h: h[:4] == b"\x1a\x45\xdf\xa3", "mkv"),
    (lambda h: h[:3] == b"\xff\xd8\xff", "jpg"),
    (lambda h: h[:8] == b"\x89PNG\r\n\x1a\n", "png"),
    (lambda h: h[:6] in (b"GIF87a", b"GIF89a"), "gif"),
    (lambda h: h[:4] == b"RIFF" and h[8:12] == b"WEBP", "webp"),
    (lambda h: h[:4] == b"%PDF", "pdf"),
    (lambda h: h[:4] == b"PK\x03\x04", "zip"),
    (lambda h: h[:3] == b"ID3" or (len(h) > 1 and h[0] == 0xFF and h[1] & 0xE0 == 0xE0), "mp3"),
    (lambda h: h[:2] == b"\x1f\x8b", "gz"),
    # MPEG-TS: 0x47 sync byte, repeating every 188 bytes — common for raw
    # HLS-style CDN streams that have no ISO/EBML header at all.
    (lambda h: len(h) >= 189 and h[0] == 0x47 and h[188] == 0x47, "ts"),
)

def sniff_ext(fpath):
    """Best-effort container detection from magic bytes. Returns an
    extension (no dot) or None if nothing matched."""
    try:
        with open(fpath, "rb") as f:
            head = f.read(256)
    except OSError:
        return None
    for check, ext in _MAGIC_SNIFFERS:
        if check(head):
            return ext
    return None

# ffprobe's format_name -> a sane file extension. Its output can list
# several comma-separated aliases for one demuxer (e.g. the ISO-BMFF
# family); first plausible one wins.
_FFPROBE_EXT_MAP = {
    "mov": "mp4", "mp4": "mp4", "m4a": "m4a", "3gp": "mp4", "3g2": "mp4", "mj2": "mp4",
    "matroska": "mkv", "webm": "webm",
    "mpegts": "ts", "mpeg": "mpg",
    "avi": "avi", "flv": "flv", "asf": "wmv", "ogg": "ogg", "wav": "wav",
    "flac": "flac", "mp3": "mp3", "aac": "aac",
}

def sniff_ext_ffprobe(fpath):
    """Deeper fallback for when magic-byte sniffing comes up empty —
    covers raw MPEG-TS and anything else without a recognizable header at
    the start of the file, since ffprobe actually demuxes rather than
    pattern-matching a handful of bytes. Needs ffprobe on PATH (it ships
    alongside ffmpeg, which this project already requires)."""
    import subprocess
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=format_name",
             "-of", "default=noprint_wrappers=1:nokey=1", fpath],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip().lower()
    except Exception:
        return None
    for name in out.split(","):
        if name in _FFPROBE_EXT_MAP:
            return _FFPROBE_EXT_MAP[name]
    return None

def fix_unknown_ext(fpath):
    """If fpath's extension looks like a placeholder yt-dlp/gallery-dl
    couldn't resolve, work out the real container and rename to match.
    Tries cheap magic-byte sniffing first, then falls back to ffprobe
    (which correctly identifies raw MPEG-TS and other containers that
    don't have a distinctive header at byte 0). Returns the (possibly
    renamed) path."""
    ext = os.path.splitext(fpath)[1].lstrip(".").lower()
    if ext not in _JUNK_EXTS:
        return fpath
    real_ext = sniff_ext(fpath) or sniff_ext_ffprobe(fpath)
    if not real_ext:
        return fpath
    new_path = os.path.splitext(fpath)[0] + "." + real_ext
    try:
        os.rename(fpath, new_path)
        return new_path
    except OSError:
        return fpath

def zip_dir(src, out):
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
        for root, _, files in os.walk(src):
            for f in files:
                fp = os.path.join(root, f)
                if fp == out:
                    continue
                zf.write(fp, os.path.relpath(fp, src))
    return out

def split_file(fpath, chunk_bytes):
    parts = []
    n = 1
    with open(fpath, "rb") as src:
        while True:
            data = src.read(chunk_bytes)
            if not data:
                break
            pp = f"{fpath}.part{n:03d}"
            with open(pp, "wb") as dst:
                dst.write(data)
            parts.append(pp)
            n += 1
    return parts
