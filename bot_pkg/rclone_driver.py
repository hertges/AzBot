"""Multi-Cloud storage driver via rclone (Mega, OneDrive, Dropbox, S3, WebDAV, Box, pCloud…).

Users or admins upload a standard `rclone.conf` via `/rclone` (mirrored to
`AzBotData/rclone.conf` so it survives container restarts). Any chat can set
its cloud destination in `/settings` to either:
  • Google Drive API (`gdrive:<account>`) — existing native implementation
  • Any rclone remote (`rclone:<remote_name>`) — uploads via `rclone copyto`
    and generates a shareable public link via `rclone link`.
"""
import os
import re
import shutil
import subprocess
import time

from . import config, state, log

logger = log.get(__name__)

RCLONE_CONF = os.path.join(config.DATA_DIR, "rclone.conf")
RCLONE_PROGRESS_RE = re.compile(
    r"Transferred:\s+([\d.]+\s*\w+)\s*/\s*([\d.]+\s*\w+),\s*(\d+)%(?:,\s*([\d.]+\s*\w+/s))?"
)

_UNITS = {
    "B": 1, "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4,
    "KB": 1000, "MB": 1000 ** 2, "GB": 1000 ** 3, "TB": 1000 ** 4,
}


def _to_bytes(s):
    m = re.match(r"([\d.]+)\s*([A-Za-z]+)", (s or "").strip())
    if not m:
        return 0
    return int(float(m.group(1)) * _UNITS.get(m.group(2).upper(), 1))


def available():
    """True when the rclone binary AND a non-empty rclone.conf exist."""
    return bool(shutil.which("rclone")) and os.path.exists(RCLONE_CONF) and os.path.getsize(RCLONE_CONF) > 0


def list_remotes():
    """Returns list of remote names (without trailing ':') from rclone.conf."""
    if not os.path.exists(RCLONE_CONF):
        return []
    remotes = []
    try:
        with open(RCLONE_CONF, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                m = re.match(r"^\s*\[([^\]]+)\]\s*$", line)
                if m:
                    remotes.append(m.group(1).strip())
    except OSError:
        pass
    return remotes


def upload_file_full(fpath, remote, progress_cb=None, task_id=None, rename=None, folder=None):
    """Uploads `fpath` to `remote:<folder>/<name>` via rclone and returns
    `{"id": "<remote>:<remote_path>", "link": "<public_url_or_path>"}`."""
    if not available():
        raise RuntimeError("rclone or rclone.conf is not configured")

    base = os.path.basename(fpath)
    ext = os.path.splitext(base)[1]
    if rename:
        rename = rename.strip()
        if ext and not rename.lower().endswith(ext.lower()):
            rename += ext
        name = rename
    else:
        name = re.sub(r"\s*\[[0-9A-Za-z_-]{16,64}\]$", "", os.path.splitext(base)[0]).strip() or base
        if not name.endswith(ext):
            name += ext

    rel_folder = (folder or "").strip().strip("/")
    remote_path = f"{rel_folder}/{name}" if rel_folder else name
    dest_spec = f"{remote}:{remote_path}"

    args = [
        "rclone", "--config", RCLONE_CONF,
        "copyto", fpath, dest_spec,
        "--stats", "1s", "--stats-one-line", "-P",
    ]
    proc = subprocess.Popen(
        args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        universal_newlines=True, bufsize=1, start_new_session=True,
    )
    if task_id:
        state.attach_proc(task_id, proc)

    total_size = os.path.getsize(fpath)
    last_cb = 0.0
    for line in iter(proc.stdout.readline, ""):
        if task_id and state.is_cancelled(task_id):
            proc.terminate()
            raise state.CancelledError("rclone upload cancelled by user")
        now = time.time()
        if progress_cb and now - last_cb >= 1.5:
            m = RCLONE_PROGRESS_RE.search(line)
            if m:
                last_cb = now
                done_b = _to_bytes(m.group(1))
                tot_b = _to_bytes(m.group(2)) or total_size
                progress_cb(done_b, tot_b)

    proc.wait()
    if task_id and state.is_cancelled(task_id):
        raise state.CancelledError("rclone upload cancelled by user")
    if proc.returncode != 0:
        raise RuntimeError(f"rclone upload failed (exit {proc.returncode}) for {dest_spec}")

    # Best-effort public share link (works on Mega, OneDrive, Dropbox, Drive, etc.)
    link = f"<code>{dest_spec}</code>"
    try:
        r = subprocess.run(
            ["rclone", "--config", RCLONE_CONF, "link", dest_spec],
            capture_output=True, text=True, timeout=25,
        )
        if r.returncode == 0 and r.stdout.strip().startswith("http"):
            link = r.stdout.strip().splitlines()[-1]
    except Exception as e:
        logger.debug(f"rclone link skipped for {dest_spec}: {e}")

    logger.info(f"Rclone[{remote}] uploaded {fpath} -> {dest_spec}")
    return {"id": dest_spec, "link": link}
