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

def save_users():
    with _lock:
        _save_json(config.USERS_FILE, list(_allowed_users))

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

def cookie_path(name="global"):
    safe = re.sub(r"[^\w\-]", "_", name)
    return os.path.join(config.COOKIES_DIR, f"{safe}.txt")

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
