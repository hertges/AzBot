import os
import threading

from . import config, state, log

logger = log.get(__name__)

CancelledError = state.CancelledError

# ── Multi-account support ────────────────────────────────────────────────
# Accounts are defined in env (see config._parse_drive_accounts). Each chat
# picks one via /settings; every API call here takes the account name so
# concurrent chats can use different accounts safely.
_srv_cache = {}
_srv_lock = threading.Lock()

def enabled():
    return bool(config.DRIVE_ACCOUNTS)

def get_service(account=None):
    name = account or state.DEFAULT_ACCOUNT
    if not name or name not in config.DRIVE_ACCOUNTS:
        return None
    with _srv_lock:
        cached = _srv_cache.get(name)
        if cached is not None:
            return cached
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    a = config.DRIVE_ACCOUNTS[name]
    creds = Credentials(
        token=None, refresh_token=a["refresh_token"],
        token_uri="https://oauth2.googleapis.com/token",
        client_id=a["client_id"], client_secret=a["client_secret"],
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    srv = build("drive", "v3", credentials=creds, static_discovery=False, cache_discovery=False)
    with _srv_lock:
        _srv_cache[name] = srv   # googleapiclient http objects are thread-safe per call
    return srv

def _root_id(chat_id):
    """Uploads go to the chat's chosen account root (or DRIVE_FOLDER_ID if set)."""
    return config.DRIVE_FOLDER_ID or "root"

def _resolve_folder(srv, name, parent_id, create=True):
    """Finds (or creates) folder `name` inside parent_id. One level — #folder
    tags are simple names. Returns folder id."""
    safe = name.replace("'", "\\'")
    q = (f"name = '{safe}' and mimeType = 'application/vnd.google-apps.folder' "
         f"and '{parent_id}' in parents and trashed = false")
    res = srv.files().list(q=q, fields="files(id)", pageSize=1).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"]
    if not create:
        return None
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder",
            "parents": [parent_id]}
    return srv.files().create(body=meta, fields="id").execute()["id"]

def ensure_folder(name, chat_id=None, account=None):
    """Returns the id of Drive folder `name` for this chat's account,
    creating it inside the root if it doesn't exist. None on failure."""
    srv = get_service(account or state.get_drive_account(chat_id))
    if not srv:
        return None
    try:
        return _resolve_folder(srv, name, _root_id(chat_id), create=True)
    except Exception as e:
        logger.warning(f"ensure_folder('{name}') failed: {e}")
        return None

def _find_file(srv, name):
    safe_name = name.replace("'", "\\'")
    q = f"name = '{safe_name}' and trashed = false"
    res = srv.files().list(q=q, fields="files(id, name)", pageSize=1).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None

def download_named_file(name, dest_path):
    """Downloads a Drive file by name to dest_path.
    Returns True if found and downloaded, False if it doesn't exist yet."""
    srv = get_service()
    if not srv:
        return False
    from googleapiclient.http import MediaIoBaseDownload
    fid = _find_file(srv, name)
    if not fid:
        return False
    request = srv.files().get_media(fileId=fid)
    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request)
        done = False
        while not done:
            _, done = downloader.next_chunk()
    return True

def upload_named_file(name, local_path):
    """Uploads/overwrites a Drive file by name."""
    srv = get_service()
    if not srv:
        return None
    from googleapiclient.http import MediaFileUpload
    fid = _find_file(srv, name)
    media = MediaFileUpload(local_path, resumable=False)
    if fid:
        srv.files().update(fileId=fid, media_body=media).execute()
        return fid
    meta = {"name": name}
    resp = srv.files().create(body=meta, media_body=media, fields="id").execute()
    return resp["id"]

def list_files(query_extra=None, page_size=20):
    """Lists files at the configured root level."""
    srv = get_service()
    if not srv:
        return None
    q_parts = ["trashed = false"]
    if config.DRIVE_FOLDER_ID:
        q_parts.append(f"'{config.DRIVE_FOLDER_ID}' in parents")
    if query_extra:
        q_parts.append(query_extra)
    q = " and ".join(q_parts)
    res = srv.files().list(
        q=q, fields="files(id, name, mimeType, size, webViewLink)",
        pageSize=page_size, orderBy="modifiedTime desc",
    ).execute()
    return res.get("files", [])

def search_files(name_contains, page_size=20):
    safe = name_contains.replace("'", "\\'")
    return list_files(query_extra=f"name contains '{safe}'", page_size=page_size)

def upload_file(fpath, progress_cb=None, task_id=None, rename=None, folder=None,
                chat_id=None, account=None):
    """Uploads fpath; returns webViewLink or None (see upload_file_full)."""
    info = upload_file_full(fpath, progress_cb=progress_cb, task_id=task_id,
                            rename=rename, folder=folder, chat_id=chat_id, account=account)
    return info["link"] if info else None

def upload_file_full(fpath, progress_cb=None, task_id=None, rename=None,
                     folder=None, chat_id=None, account=None, make_public=True):
    """Uploads fpath to the chat's chosen Drive account.

    rename  — final file NAME; extension is preserved from fpath unless the
              caller already included one ("extension won't be touched" rule:
              we only REPLACE the basename).
    folder  — "#folder" tag target: created inside the root on demand, file
              goes there instead of the root.
    Returns {"id", "link"} or None."""
    from googleapiclient.http import MediaFileUpload
    acct = account or state.get_drive_account(chat_id)
    srv = get_service(acct)
    if not srv:
        return None
    logger.info(f"Drive[{acct}] upload starting — {fpath}")

    base = os.path.basename(fpath)
    ext = os.path.splitext(base)[1]
    if rename:
        # Extension is NEVER replaced by the requested name: if |name lacks
        # the file's real extension we append it; if the user already typed
        # the same extension we don't double it. A dotted name like "my.v2"
        # still keeps the true ".mp4".
        rename = rename.strip()
        if ext and not rename.lower().endswith(ext.lower()):
            rename = rename + ext
        name = rename
    else:
        name = base

    meta = {"name": name}
    if folder:
        pid = ensure_folder(folder, chat_id=chat_id, account=acct)
        if pid:
            meta["parents"] = [pid]
    elif config.DRIVE_FOLDER_ID:
        meta["parents"] = [config.DRIVE_FOLDER_ID]

    media = MediaFileUpload(fpath, chunksize=50 * 1024 * 1024, resumable=True)
    req = srv.files().create(body=meta, media_body=media, fields="id, webViewLink")
    response = None
    while response is None:
        if task_id and state.is_cancelled(task_id):
            logger.info(f"[{task_id}] Drive upload cancelled by user")
            raise CancelledError("drive upload cancelled by user")
        status, response = req.next_chunk(num_retries=3)
        if status and progress_cb:
            progress_cb(int(status.resumable_progress), int(status.total_size or 0))
    if make_public:
        try:
            srv.permissions().create(fileId=response["id"],
                                     body={"role": "reader", "type": "anyone"}).execute()
        except Exception as e:
            logger.warning(f"Drive permission set failed for {response.get('id')}: {e}")
    link = response.get("webViewLink") or f"https://drive.google.com/file/d/{response['id']}/view"
    logger.info(f"Drive[{acct}] upload done — {fpath} -> {link}")
    return {"id": response["id"], "link": link}

def delete_file(file_id):
    srv = get_service()
    if not srv:
        return False
    srv.files().delete(fileId=file_id).execute()
    logger.info(f"Drive file deleted: {file_id}")
    return True

def rename_file(file_id, new_name):
    srv = get_service()
    if not srv:
        return False
    srv.files().update(fileId=file_id, body={"name": new_name}).execute()
    logger.info(f"Drive file {file_id} renamed to '{new_name}'")
    return True

def get_file(file_id):
    srv = get_service()
    if not srv:
        return None
    try:
        return srv.files().get(fileId=file_id, fields="id, name, size, webViewLink, mimeType").execute()
    except Exception as e:
        logger.warning(f"Drive get_file failed for {file_id}: {e}")
        return None

def list_folder_full(folder_id, max_total=2000):
    """Fetches every item directly inside folder_id (files AND subfolders),
    paginating internally. The UI slices this client-side rather than
    chaining Drive's own pageToken, since that only supports stepping
    forward — client-side slicing lets the browser jump to any page
    directly and supports real folder navigation with a back button."""
    srv = get_service()
    if not srv:
        return []
    q = f"'{folder_id}' in parents and trashed = false"
    files, page_token = [], None
    while True:
        res = srv.files().list(
            q=q, fields="nextPageToken, files(id, name, mimeType, size, webViewLink)",
            pageSize=200, orderBy="folder,name_natural", pageToken=page_token,
        ).execute()
        files.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token or len(files) >= max_total:
            break
    return files[:max_total]

def create_folder(name, parent_id):
    srv = get_service()
    if not srv:
        return None
    meta = {"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]}
    resp = srv.files().create(body=meta, fields="id").execute()
    return resp.get("id")

def get_file_any(file_id):
    """Like get_file, but also works for a file that isn't ours — just
    shared with our account, or shared publicly ('anyone with the link').
    Needed for leeching Drive links a user pastes in that weren't
    uploaded through this bot."""
    srv = get_service()
    if not srv:
        return None
    try:
        return srv.files().get(
            fileId=file_id, fields="id, name, size, webViewLink, mimeType",
            supportsAllDrives=True,
        ).execute()
    except Exception as e:
        logger.warning(f"Drive get_file_any failed for {file_id}: {e}")
        return None

def list_folder_any(folder_id, max_total=2000):
    """Like list_folder_full, but for a shared folder that isn't ours."""
    srv = get_service()
    if not srv:
        return []
    q = f"'{folder_id}' in parents and trashed = false"
    files, page_token = [], None
    while True:
        res = srv.files().list(
            q=q, fields="nextPageToken, files(id, name, mimeType, size, webViewLink)",
            pageSize=200, orderBy="folder,name_natural", pageToken=page_token,
            supportsAllDrives=True, includeItemsFromAllDrives=True,
        ).execute()
        files.extend(res.get("files", []))
        page_token = res.get("nextPageToken")
        if not page_token or len(files) >= max_total:
            break
    return files[:max_total]

def download_file_content(file_id, dest_path, task_id=None, mime_type=None):
    """Downloads a Drive file's actual bytes to dest_path via the API
    (using our own OAuth credentials — much more reliable than yt-dlp's
    anonymous GoogleDrive extractor, which frequently 400s on ordinary
    shared links). Google-native docs (Sheets/Docs/Slides) get exported
    to a normal file format instead, since they have no raw bytes."""
    srv = get_service()
    if not srv:
        raise RuntimeError("Drive isn't configured")
    from googleapiclient.http import MediaIoBaseDownload

    export_map = {
        "application/vnd.google-apps.document": ("application/pdf", ".pdf"),
        "application/vnd.google-apps.spreadsheet": (
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", ".xlsx"),
        "application/vnd.google-apps.presentation": (
            "application/vnd.openxmlformats-officedocument.presentationml.presentation", ".pptx"),
    }
    if mime_type in export_map:
        export_mime, ext = export_map[mime_type]
        if not dest_path.endswith(ext):
            dest_path += ext
        request = srv.files().export_media(fileId=file_id, mimeType=export_mime)
    else:
        request = srv.files().get_media(fileId=file_id, supportsAllDrives=True)

    with open(dest_path, "wb") as fh:
        downloader = MediaIoBaseDownload(fh, request, chunksize=50 * 1024 * 1024)
        done = False
        while not done:
            if task_id and state.is_cancelled(task_id):
                raise state.CancelledError("cancelled by user")
            _, done = downloader.next_chunk(num_retries=3)
    return dest_path
