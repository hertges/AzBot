import os, json, re, threading, shutil, time, queue, uuid

from . import config, log

logger = log.get(__name__)

class CancelledError(Exception):
    """Raised anywhere in a job's pipeline — download, subprocess, upload —
    once the user has hit Stop / run /cancel. Central definition so every
    stage can raise/catch the same type."""
    pass

def _load_json(path, default):
    try:
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)
    except Exception:
        pass
    return default

def _save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)

_settings = _load_json(config.SETTINGS_FILE, {})
_allowed_users = set(_load_json(config.USERS_FILE, [config.ADMIN_ID] if config.ADMIN_ID else []))
_lock = threading.Lock()

def save_settings():
    with _lock:
        _save_json(config.SETTINGS_FILE, _settings)
    # Mirror to Drive (AzBotData/settings.json) — best-effort, never blocks.
    try:
        from . import datastore
        import threading as _th
        _th.Thread(target=datastore.push_settings, daemon=True).start()
    except Exception:
        pass

def save_users():
    with _lock:
        _save_json(config.USERS_FILE, list(_allowed_users))
    # Mirror access control to Drive (AzBotData/users.json).
    try:
        from . import datastore
        import threading as _th
        _th.Thread(target=datastore.push_datafile,
                   args=(config.USERS_FILE, "users.json"), daemon=True).start()
    except Exception:
        pass

def is_authorized(user_id):
    if not config.ADMIN_ID:
        return True
    uid = str(user_id)
    return uid == config.ADMIN_ID or uid in _allowed_users

def allow_user(user_id):
    _allowed_users.add(str(user_id)); save_users()
    logger.info(f"user {user_id} authorized")

def ban_user(user_id):
    _allowed_users.discard(str(user_id)); save_users()
    logger.info(f"user {user_id} banned")

def get_prefs(chat_id):
    key = str(chat_id)
    with _lock:
        p = _settings.setdefault(key, {})
        p.setdefault("active_cookie", "global")
        return p

def set_pref(chat_id, k, v):
    p = get_prefs(chat_id)
    p[k] = v
    save_settings()

def get_autoclean(chat_id):
    return bool(get_prefs(chat_id).get("autoclean", False))

def set_autoclean(chat_id, value):
    set_pref(chat_id, "autoclean", bool(value))

def get_as_document(chat_id):
    return bool(get_prefs(chat_id).get("as_document", False))

def set_as_document(chat_id, value):
    set_pref(chat_id, "as_document", bool(value))

# ── Drive account selection (multi-account) ─────────────────────────────
# Accounts themselves come from env (config.DRIVE_ACCOUNTS). Per chat we
# store which one to use; default is the first configured account.
DEFAULT_ACCOUNT = next(iter(config.DRIVE_ACCOUNTS), None)

def get_drive_account(chat_id):
    name = get_prefs(chat_id).get("drive_account") or DEFAULT_ACCOUNT
    if name not in config.DRIVE_ACCOUNTS:
        name = DEFAULT_ACCOUNT
        if name:
            set_pref(chat_id, "drive_account", name)
    return name

def set_drive_account(chat_id, name):
    if name not in config.DRIVE_ACCOUNTS:
        return False
    set_pref(chat_id, "drive_account", name)
    invalidate_listing(chat_id)
    reset_drive_nav(chat_id)
    return True

# ── Instagram index ledger (flat, per user / type) ──────────────────────
# One file per (username, type) under data/indexes/ — gallery-dl's
# --download-archive format (one id per line). Shared across chats on
# purpose: one archive of @user serves everyone. /igindex manages them.
def index_path(chat_id, username, archive_type):
    os.makedirs(config.INDEX_DIR, exist_ok=True)
    safe_u = re.sub(r"[^A-Za-z0-9._-]", "_", username)
    return os.path.join(config.INDEX_DIR, f"{safe_u}.{archive_type}.txt")

def ig_index_exists(chat_id, username, archive_type):
    p = index_path(chat_id, username, archive_type)
    if os.path.exists(p):
        return True
    # Not local — try restoring from the Drive datastore (AzBotData/indexes/…)
    try:
        from . import datastore
        if datastore.pull_index(chat_id, username, archive_type, p):
            return True
    except Exception:
        pass
    return False

def list_ig_indexes():
    """[(username, type), ...] found on the bot."""
    out = []
    if os.path.isdir(config.INDEX_DIR):
        for f in sorted(os.listdir(config.INDEX_DIR)):
            m = re.match(r"^(.+)\.(posts|reels|stories|highlights|tagged)\.txt$", f)
            if m:
                out.append((m.group(1), m.group(2)))
    return out

def delete_ig_index(username=None, archive_type=None):
    """Deletes matching ledgers (local + Drive mirror). All args None → wipe
    every index. Returns count deleted."""
    removed = []
    if os.path.isdir(config.INDEX_DIR):
        for f in os.listdir(config.INDEX_DIR):
            m = re.match(r"^(.+)\.(posts|reels|stories|highlights|tagged)\.txt$", f)
            if not m:
                continue
            u, t = m.group(1), m.group(2)
            if username and re.sub(r"[^A-Za-z0-9._-]", "_", username) != u:
                continue
            if archive_type and t != archive_type:
                continue
            try:
                os.remove(os.path.join(config.INDEX_DIR, f))
                removed.append(f)
            except OSError:
                pass
    # Mirror the deletion into the Drive datastore too.
    try:
        from . import datastore
        for f in removed:
            datastore.delete_remote_index(None, f)
    except Exception:
        pass
    for f in removed:
        logger.info(f"ig index deleted: {f}")
    return len(removed)

def cookie_path(name="global"):
    safe = re.sub(r"[^\w\-]", "_", name)
    return os.path.join(config.COOKIES_DIR, f"{safe}.txt")

def mirror_cookie(name):
    """Push one cookie file to the Drive datastore (best-effort, async)."""
    p = cookie_path(name)
    if not os.path.exists(p):
        return
    try:
        from . import datastore
        import threading as _th
        _th.Thread(target=datastore.push_datafile,
                   args=(p, f"cookies/{os.path.basename(p)}"), daemon=True).start()
    except Exception:
        pass

def unmirror_cookie(name):
    """Remove a cookie file from the Drive datastore (best-effort, async)."""
    try:
        from . import datastore
        safe = re.sub(r"[^\w\-]", "_", name)
        import threading as _th
        _th.Thread(target=datastore.delete_remote_datafile,
                   args=(f"cookies/{safe}.txt",), daemon=True).start()
    except Exception:
        pass

def restore_cookies():
    """Boot-time: pull every cookie from AzBotData/cookies/ that's missing
    locally. Returns count restored."""
    n = 0
    try:
        from . import datastore
        for fname in datastore.list_remote_files("cookies"):
            local = os.path.join(config.COOKIES_DIR, fname)
            if not os.path.exists(local) and datastore.pull_datafile(f"cookies/{fname}", local):
                n += 1
    except Exception as e:
        logger.warning(f"cookie restore skipped: {e}")
    return n

# ── Custom thumbnails (WZML-X /setthumb parity) ──────────────────────────
def thumb_path(chat_id):
    os.makedirs(config.THUMBS_DIR, exist_ok=True)
    return os.path.join(config.THUMBS_DIR, f"{chat_id}.jpg")

def get_thumb(chat_id):
    p = thumb_path(chat_id)
    return p if os.path.exists(p) else None

def set_thumb(chat_id, src_image):
    """Normalizes any incoming image to a ≤320px JPEG thumbnail and mirrors
    it to the Drive datastore. Returns the stored path."""
    import subprocess
    dst = thumb_path(chat_id)
    try:
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", src_image,
                        "-vf", "scale='min(320,iw)':-2", "-frames:v", "1", dst],
                       capture_output=True, timeout=30, check=True)
    except Exception:
        # not an image ffmpeg can decode — copy as-is only if it's jpg-sized
        shutil.copyfile(src_image, dst)
    try:
        from . import datastore
        import threading as _th
        _th.Thread(target=datastore.push_datafile,
                   args=(dst, f"thumbs/{os.path.basename(dst)}"), daemon=True).start()
    except Exception:
        pass
    logger.info(f"custom thumb set for chat {chat_id}")
    return dst

def delete_thumb(chat_id):
    p = thumb_path(chat_id)
    removed = False
    if os.path.exists(p):
        os.remove(p)
        removed = True
    try:
        from . import datastore
        import threading as _th
        _th.Thread(target=datastore.delete_remote_datafile,
                   args=(f"thumbs/{os.path.basename(p)}",), daemon=True).start()
    except Exception:
        pass
    return removed

def restore_thumbs():
    """Boot-time: pull every custom thumb missing locally."""
    n = 0
    try:
        from . import datastore
        os.makedirs(config.THUMBS_DIR, exist_ok=True)
        for fname in datastore.list_remote_files("thumbs"):
            local = os.path.join(config.THUMBS_DIR, fname)
            if not os.path.exists(local) and datastore.pull_datafile(f"thumbs/{fname}", local):
                n += 1
    except Exception as e:
        logger.warning(f"thumb restore skipped: {e}")
    return n

# ── Duplicate-task guard (WZML-X parity: same link ≠ second download) ────
ACTIVE_URLS = {}          # normalized url -> task_id
_urls_lock = threading.Lock()

def register_url(url, task_id):
    key = url.strip()
    with _urls_lock:
        ACTIVE_URLS[key] = task_id

def find_active_url(url):
    with _urls_lock:
        tid = ACTIVE_URLS.get(url.strip())
    return tid

def drop_url_by_task(task_id):
    with _urls_lock:
        for k, v in list(ACTIVE_URLS.items()):
            if v == task_id:
                del ACTIVE_URLS[k]

def list_cookie_profiles():
    return [os.path.splitext(f)[0] for f in os.listdir(config.COOKIES_DIR) if f.endswith(".txt")]

def active_cookie_file(chat_id):
    name = get_prefs(chat_id).get("active_cookie", "global")
    p = cookie_path(name)
    return p if os.path.exists(p) else None

def rename_cookie_profile(chat_id, old_name, new_name):
    old_path, new_path = cookie_path(old_name), cookie_path(new_name)
    if not os.path.exists(old_path):
        return False, "That profile doesn't exist anymore."
    if os.path.exists(new_path):
        return False, "A profile with that name already exists."
    os.rename(old_path, new_path)
    # Only updates *this* chat's pointer — other chats using the same
    # profile name will just see it as missing until they switch again.
    if get_prefs(chat_id).get("active_cookie") == old_name:
        set_pref(chat_id, "active_cookie", new_name)
    logger.info(f"cookie profile '{old_name}' renamed to '{new_name}'")
    return True, None

def delete_cookie_profile(chat_id, name):
    path = cookie_path(name)
    if not os.path.exists(path):
        return False
    os.remove(path)
    unmirror_cookie(name)   # remove from Drive datastore too
    if get_prefs(chat_id).get("active_cookie") == name:
        set_pref(chat_id, "active_cookie", "global")
    logger.info(f"cookie profile '{name}' deleted")
    return True

# ── Short-lived "waiting for a text reply" state ────────────────────────
# Used for flows like "reply with the new name" after tapping Rename,
# where the next plain-text message should be consumed as input instead
# of going through the normal auto-leech link detection.
_awaiting_input = {}
_awaiting_lock = threading.Lock()
AWAITING_INPUT_TTL = 120

def set_awaiting_input(chat_id, kind, target):
    with _awaiting_lock:
        _awaiting_input[chat_id] = (kind, target, time.time())

def pop_awaiting_input(chat_id):
    with _awaiting_lock:
        entry = _awaiting_input.pop(chat_id, None)
    if not entry:
        return None
    kind, target, ts = entry
    if time.time() - ts > AWAITING_INPUT_TTL:
        return None
    return kind, target

# ── Drive browser: per-chat folder navigation + short-lived listing cache ──
# Full folder contents are fetched once and paginated client-side (see
# drive.list_folder_full) — this cache is just so paging back and forth
# within the same folder doesn't re-fetch from the API every tap.
drive_nav = {}            # chat_id -> [(folder_id, display_name), ...], root first
_drive_listing_cache = {}  # chat_id -> {"folder_id":.., "files":[...], "ts":..}
DRIVE_LISTING_TTL = 120

def _drive_root():
    return config.DRIVE_FOLDER_ID or "root"

def get_drive_nav(chat_id):
    if chat_id not in drive_nav:
        drive_nav[chat_id] = [(_drive_root(), "🏠 Root")]
    return drive_nav[chat_id]

def push_drive_nav(chat_id, folder_id, name):
    get_drive_nav(chat_id).append((folder_id, name))

def pop_drive_nav(chat_id):
    nav = get_drive_nav(chat_id)
    if len(nav) > 1:
        nav.pop()
    return nav[-1]

def reset_drive_nav(chat_id):
    drive_nav[chat_id] = [(_drive_root(), "🏠 Root")]

def get_cached_listing(chat_id, folder_id):
    c = _drive_listing_cache.get(chat_id)
    if c and c["folder_id"] == folder_id and time.time() - c["ts"] < DRIVE_LISTING_TTL:
        return c["files"]
    return None

def set_cached_listing(chat_id, folder_id, files):
    _drive_listing_cache[chat_id] = {"folder_id": folder_id, "files": files, "ts": time.time()}

def invalidate_listing(chat_id):
    _drive_listing_cache.pop(chat_id, None)

# ── Pending yt-dlp quality picker sessions ──────────────────────────────
# Keyed by a short token (callback_data has a 64-byte limit, too small for
# a full URL) rather than chat_id, since a chat could in principle have
# more than one picker message open at once.
_quality_sessions = {}
QUALITY_SESSION_TTL = 600

def new_quality_session(url, dest, zip_output, reply_to):
    token = uuid.uuid4().hex[:10]
    _quality_sessions[token] = {
        "url": url, "dest": dest, "zip_output": zip_output,
        "reply_to": reply_to, "ts": time.time(),
    }
    return token

def get_quality_session(token):
    s = _quality_sessions.get(token)
    if not s or time.time() - s["ts"] > QUALITY_SESSION_TTL:
        _quality_sessions.pop(token, None)
        return None
    return s

def pop_quality_session(token):
    return _quality_sessions.pop(token, None)

class QualityExtra:
    """Rename/folder tags attached to a picker session (kept separate so
    the session dict shape stays stable)."""
    _extra = {}

    @classmethod
    def save(cls, token, rename=None, folder=None):
        if rename or folder:
            cls._extra[token] = {"rename": rename, "folder": folder}

    @classmethod
    def take(cls, token):
        return cls._extra.pop(token, {"rename": None, "folder": None})

# Retry tokens for failed quality probes ("🔄 Retry" button)
_retry_tokens = {}

def new_retry_token(url, dest, zip_output, rename, folder):
    token = uuid.uuid4().hex[:10]
    _retry_tokens[token] = {
        "url": url, "dest": dest, "zip_output": zip_output,
        "rename": rename, "folder": folder, "ts": time.time(),
    }
    return token

def pop_retry_token(token):
    t = _retry_tokens.pop(token, None)
    if t and time.time() - t["ts"] > 900:
        return None
    return t

# ── WZML-X-style format menu storage ─────────────────────────────────────
# The real format table must outlive the probe (buttons reference it by
# index). Kept per token with a TTL sweeper on access.
_picker_formats = {}
_picker_pending = {}
PICKER_TTL = 900

def save_picker_formats(token, title, options):
    _picker_formats[token] = {"title": title, "options": options, "ts": time.time()}

class PickerFormats:
    @classmethod
    def save(cls, token, title, options):
        save_picker_formats(token, title, options)

    @classmethod
    def load(cls, token):
        e = _picker_formats.get(token)
        if not e or time.time() - e["ts"] > PICKER_TTL:
            _picker_formats.pop(token, None)
            return "", []
        # prune stale siblings occasionally
        if len(_picker_formats) > 64:
            now = time.time()
            for k in [k for k, v in _picker_formats.items() if now - v["ts"] > PICKER_TTL]:
                _picker_formats.pop(k, None)
        return e["title"], e["options"]

class PickerPending:
    """Video-only selector waiting for its audio-merge choice."""
    @classmethod
    def save(cls, token, selector):
        _picker_pending[token] = (selector, time.time())

    @classmethod
    def take(cls, token):
        e = _picker_pending.pop(token, None)
        if not e or time.time() - e[1] > PICKER_TTL:
            return None
        return e[0]

# ── Pending /ig multi-select archive-type sessions ──────────────────────
_ig_picker_sessions = {}
IG_PICKER_TTL = 600

def new_ig_picker_session(username, dest, default_selected=("stories",)):
    token = uuid.uuid4().hex[:10]
    _ig_picker_sessions[token] = {
        "username": username, "dest": dest,
        "selected": set(default_selected), "ts": time.time(),
    }
    return token

def get_ig_picker_session(token):
    s = _ig_picker_sessions.get(token)
    if not s or time.time() - s["ts"] > IG_PICKER_TTL:
        _ig_picker_sessions.pop(token, None)
        return None
    return s

def toggle_ig_picker_type(token, archive_type):
    s = get_ig_picker_session(token)
    if not s:
        return None
    if archive_type in s["selected"]:
        s["selected"].discard(archive_type)
    else:
        s["selected"].add(archive_type)
    return s

def pop_ig_picker_session(token):
    return _ig_picker_sessions.pop(token, None)

# ── Disk / job bookkeeping ──────────────────────────────────────────────
ACTIVE_JOBS = {}          # task_id -> {cancelled, dir, ...}
jobs_lock   = threading.Lock()
task_queue  = queue.Queue()

def disk_free_mb():
    return shutil.disk_usage(config.DOWNLOAD_DIR).free // (1024 * 1024)

def ensure_free(mb=config.MIN_FREE_MB):
    if disk_free_mb() >= mb:
        return True
    for e in sorted(os.scandir(config.DOWNLOAD_DIR), key=lambda x: x.stat().st_mtime):
        if e.is_dir():
            shutil.rmtree(e.path, ignore_errors=True)
        if disk_free_mb() >= mb:
            return True
    return disk_free_mb() >= mb

def purge_stale(max_age_s=3600):
    if not os.path.isdir(config.DOWNLOAD_DIR):
        return
    now = time.time()
    for e in os.scandir(config.DOWNLOAD_DIR):
        if e.is_dir() and now - e.stat().st_mtime > max_age_s:
            shutil.rmtree(e.path, ignore_errors=True)

def is_cancelled(task_id):
    with jobs_lock:
        job = ACTIVE_JOBS.get(task_id)
        return bool(job and job.get("cancelled"))

def register_job(task_id, chat_id, msg_id, kind, task_dir):
    with jobs_lock:
        ACTIVE_JOBS[task_id] = {
            "cancelled": False, "dir": task_dir, "chat_id": chat_id,
            "msg_id": msg_id, "kind": kind, "proc": None,
            "start": time.time(),
        }
    logger.info(f"[{task_id}] job registered — kind={kind} chat={chat_id}")

def attach_proc(task_id, proc):
    with jobs_lock:
        if task_id in ACTIVE_JOBS:
            ACTIVE_JOBS[task_id]["proc"] = proc

def cancel_task(task_id):
    with jobs_lock:
        job = ACTIVE_JOBS.get(task_id)
        if not job:
            return False
        job["cancelled"] = True
        proc = job.get("proc")
    logger.info(f"[{task_id}] cancel requested")
    if proc is not None:
        try:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()
        except Exception as e:
            logger.warning(f"[{task_id}] error terminating process: {e}")
    return True

def drop_job(task_id):
    with jobs_lock:
        job = ACTIVE_JOBS.pop(task_id, None)
    if job:
        logger.info(f"[{task_id}] job finished in {time.time() - job.get('start', time.time()):.1f}s")
