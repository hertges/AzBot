import os

from . import config, state, log

logger = log.get(__name__)

CancelledError = state.CancelledError

_drive_ready = bool(config.GCP_CLIENT_ID and config.GCP_CLIENT_SECRET and config.GCP_REFRESH_TOKEN)
if _drive_ready:
    logger.info("Google Drive integration enabled")
else:
    logger.info("Google Drive integration disabled (GCP credentials not set)")

def enabled():
    return _drive_ready

def get_service():
    if not _drive_ready:
        return None
    from google.oauth2.credentials import Credentials
    from googleapiclient.discovery import build
    creds = Credentials(
        token=None, refresh_token=config.GCP_REFRESH_TOKEN,
        token_uri="https://oauth2.googleapis.com/token",
        client_id=config.GCP_CLIENT_ID, client_secret=config.GCP_CLIENT_SECRET,
        scopes=["https://www.googleapis.com/auth/drive"],
    )
    return build("drive", "v3", credentials=creds, static_discovery=False, cache_discovery=False)

def _find_file(srv, name):
    safe_name = name.replace("'", "\\'")
    q = f"name = '{safe_name}' and trashed = false"
    if config.DRIVE_FOLDER_ID:
        q += f" and '{config.DRIVE_FOLDER_ID}' in parents"
    res = srv.files().list(q=q, fields="files(id, name)", pageSize=1).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None

def download_named_file(name, dest_path):
    """Downloads a Drive file by name (in DRIVE_FOLDER_ID) to dest_path.
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
    """Uploads/overwrites a Drive file by name (in DRIVE_FOLDER_ID)."""
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
    if config.DRIVE_FOLDER_ID:
        meta["parents"] = [config.DRIVE_FOLDER_ID]
    resp = srv.files().create(body=meta, media_body=media, fields="id").execute()
    return resp["id"]

def list_files(query_extra=None, page_size=20):
    """Lists files in DRIVE_FOLDER_ID (or Drive root if unset)."""
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

def upload_file(fpath, progress_cb=None, task_id=None):
    """Uploads fpath to config.DRIVE_FOLDER_ID (or Drive root). Returns webViewLink or None."""
    info = upload_file_full(fpath, progress_cb=progress_cb, task_id=task_id)
    return info["link"] if info else None

def upload_file_full(fpath, progress_cb=None, task_id=None):
    """Same as upload_file but returns {"id": ..., "link": ...} — use when
    the caller needs the file id too (e.g. to attach Rename/Delete buttons)."""
    from googleapiclient.http import MediaFileUpload
    srv = get_service()
    if not srv:
        return None
    logger.info(f"Drive upload starting — {fpath}")
    meta = {"name": os.path.basename(fpath)}
    if config.DRIVE_FOLDER_ID:
        meta["parents"] = [config.DRIVE_FOLDER_ID]
    media = MediaFileUpload(fpath, chunksize=50 * 1024 * 1024, resumable=True)
    req = srv.files().create(body=meta, media_body=media, fields="id, webViewLink")
    response = None
    while response is None:
        if task_id and state.is_cancelled(task_id):
            logger.info(f"[{task_id}] Drive upload cancelled by user")
            raise CancelledError("drive upload cancelled by user")
        status, response = req.next_chunk()
        if status and progress_cb:
            progress_cb(int(status.resumable_progress), int(status.total_size or 0))
    try:
        srv.permissions().create(fileId=response["id"], body={"role": "reader", "type": "anyone"}).execute()
    except Exception as e:
        logger.warning(f"Drive permission set failed for {response.get('id')}: {e}")
    logger.info(f"Drive upload done — {fpath} -> {response.get('webViewLink')}")
    return {"id": response["id"], "link": response.get("webViewLink")}

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
            _, done = downloader.next_chunk()
    return dest_path
