"""Drive-backed persistence for bot data — settings.json and Instagram
index ledgers live inside ONE Drive folder (default "AzBotData"):

    AzBotData/
      settings.json
      indexes/<chat>/<user>.<type>.txt

The local copies under data/ are the working set (fast, offline-safe);
every change is mirrored up to Drive, and missing local pieces are pulled
down on demand. All transfers are tiny text files — negligible RAM.

Everything is stored on the FIRST configured Drive account (bot-level
storage, not the per-chat upload choice). If Drive isn't configured or is
unreachable, local operation continues untouched.
"""
import os, threading

from . import config, log

logger = log.get(__name__)

DATA_FOLDER_NAME = os.environ.get("DATA_DRIVE_FOLDER", "AzBotData").strip() or "AzBotData"

_folder_ids = {}          # rel-path -> folder id cache (process lifetime)
_lock = threading.Lock()


def _account():
    return next(iter(config.DRIVE_ACCOUNTS), None)


def _folder_by_name(srv, name, parent_id):
    safe = name.replace("'", "\\'")
    q = (f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' "
         f"and '{parent_id}' in parents and trashed = false")
    res = srv.files().list(q=q, fields="files(id)", pageSize=1).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]}
    return srv.files().create(body=meta, fields="id").execute()["id"]


def _ensure_path(rel_dir):
    """resolves-or-creates 'indexes/42'-style nested folders. Returns folder
    id or None. Cached so repeat pushes don't hit the API."""
    root = config.DRIVE_FOLDER_ID or "root"
    key = f"{root}|{rel_dir}"
    with _lock:
        if key in _folder_ids:
            return _folder_ids[key]
    try:
        from . import drive
        srv = drive.get_service(_account())
        if not srv:
            return None
        cur = root
        for part in [DATA_FOLDER_NAME] + [p for p in rel_dir.split("/") if p]:
            cur = _folder_by_name(srv, part, cur)
        with _lock:
            _folder_ids[key] = cur
        return cur
    except Exception as e:
        logger.warning(f"datastore: ensure_path('{rel_dir}') failed: {e}")
        return None


def _upload(local_path, rel_path):
    """PUT local file at rel_path ('settings.json', 'indexes/42/u.t.txt')."""
    try:
        from . import drive
        srv = drive.get_service(_account())
        if not srv:
            return False
        from googleapiclient.http import MediaFileUpload
        d = os.path.dirname(rel_path)
        pid = _ensure_path(d) if d else _ensure_path("")
        if not pid:
            return False
        # overwrite if a file with this name already exists in that folder
        safe = os.path.basename(rel_path).replace("'", "\\'")
        q = (f"name = '{safe}' and '{pid}' in parents and trashed = false")
        res = srv.files().list(q=q, fields="files(id)", pageSize=1).execute()
        existing = res.get("files", [])
        media = MediaFileUpload(local_path, resumable=False)
        if existing:
            srv.files().update(fileId=existing[0]["id"], media_body=media).execute()
        else:
            srv.files().create(body={"name": os.path.basename(rel_path), "parents": [pid]},
                               media_body=media, fields="id").execute()
        logger.info(f"datastore: pushed {rel_path}")
        return True
    except Exception as e:
        logger.warning(f"datastore: push {rel_path} failed: {e}")
        return False


def _download(remote_name, folder_id, local_path):
    """GET a file by name from folder_id into local_path. Returns True if
    found+written."""
    try:
        from . import drive
        srv = drive.get_service(_account())
        if not srv:
            return False
        safe = remote_name.replace("'", "\\'")
        q = f"name = '{safe}' and '{folder_id}' in parents and trashed = false"
        res = srv.files().list(q=q, fields="files(id)", pageSize=1).execute()
        files = res.get("files", [])
        if not files:
            return False
        from googleapiclient.http import MediaIoBaseDownload
        request = srv.files().get_media(fileId=files[0]["id"])
        os.makedirs(os.path.dirname(local_path) or ".", exist_ok=True)
        with open(local_path, "wb") as fh:
            downloader = MediaIoBaseDownload(fh, request)
            done = False
            while not done:
                _, done = downloader.next_chunk(num_retries=2)
        return True
    except Exception as e:
        logger.warning(f"datastore: pull {remote_name} failed: {e}")
        return False


# ── public API ────────────────────────────────────────────────────────────

def push_index(chat_id, username, archive_type):
    """Mirror the local index ledger up to Drive (fire-and-forget friendly;
    cheap enough to call inline too). chat_id is unused (flat index space)
    but kept in the signature for compatibility."""
    p = None
    try:
        from . import state as _state
        p = _state.index_path(chat_id, username, archive_type)
    except Exception:
        return False
    if not p or not os.path.exists(p):
        return False
    rel = f"indexes/{os.path.basename(p)}"
    return _upload(p, rel)


def pull_index(chat_id, username, archive_type, local_path):
    """Ensure local_path exists, downloading the Drive copy if missing.
    Returns True if a remote copy was pulled."""
    if os.path.exists(local_path):
        return False
    folder = _ensure_path("indexes")
    if not folder:
        return False
    ok = _download(os.path.basename(local_path), folder, local_path)
    if ok:
        logger.info(f"datastore: pulled index {username}.{archive_type}")
    return ok


def delete_remote_index(chat_id, basename):
    """Best-effort removal of indexes/<basename> from Drive."""
    try:
        from . import drive
        srv = drive.get_service(_account())
        folder = _ensure_path("indexes")
        if not srv or not folder:
            return False
        safe = basename.replace("'", "\\'")
        q = f"name = '{safe}' and '{folder}' in parents and trashed = false"
        res = srv.files().list(q=q, fields="files(id)", pageSize=10).execute()
        n = 0
        for f in res.get("files", []):
            try:
                srv.files().delete(fileId=f["id"]).execute()
                n += 1
            except Exception:
                pass
        return n > 0
    except Exception as e:
        logger.warning(f"datastore: delete_remote_index failed: {e}")
        return False


def push_settings():
    """Upload settings.json into AzBotData/. Called (async) after every save."""
    return _upload(config.SETTINGS_FILE, "settings.json")


def pull_settings_if_missing():
    """Boot-time restore: only when there's no local settings.json yet."""
    if os.path.exists(config.SETTINGS_FILE):
        return False
    folder = _ensure_path("")
    if not folder:
        return False
    ok = _download("settings.json", folder, config.SETTINGS_FILE)
    if ok:
        logger.info("datastore: restored settings.json from Drive")
    return ok
