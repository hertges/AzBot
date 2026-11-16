"""upload.am — let THEIR servers fetch the URL, so big files never touch this disk.

`/ua <url>` asks upload.am to fetch the file into your account over its own
connection. Nothing is downloaded here, which is the whole point on a box with
a small disk: a 4 GB file costs the bot zero bytes of local storage.

The fetch is async on their side and there is no progress endpoint, so the
handler snapshots the root file list, queues the fetch, then polls until a file
that wasn't there before shows up.

Env:
  UA_TOKEN=ua_live_xxx    # upload.am → Account → API (required)
  UA_FOLDER_UUID=         # optional destination folder, default account root
  UA_POLL_MIN=20          # minutes to wait for the file to appear (default 20)
"""
import os
import threading
import time
from urllib.parse import urlparse

import requests
from pyrogram import filters
from pyrogram.types import Message

from . import config, state, log
from .core import app
from .utils import esc, fmtsz, guarded, safe_edit, throttled_edit

logger = log.get(__name__)

BASE = "https://upload.am/api/v1"
POLL_EVERY = 10


class UaError(Exception):
    def __init__(self, code, msg):
        self.code = code
        super().__init__(msg or code)


def token():
    return (os.environ.get("UA_TOKEN") or "").strip()


def _headers():
    return {"Authorization": f"Bearer {token()}", "Content-Type": "application/json"}


def _call(method, path, **kw):
    """One API call, with upload.am's error envelope turned into UaError."""
    try:
        r = requests.request(method, BASE + path, headers=_headers(), timeout=(20, 90), **kw)
    except Exception as e:
        raise UaError("network", str(e))
    if r.status_code == 429:
        raise UaError("rate_limited", f"rate limit — retry in {r.headers.get('Retry-After', '60')}s")
    try:
        body = r.json()
    except Exception:
        body = {}
    if r.status_code >= 400:
        raise UaError(body.get("error") or f"http_{r.status_code}",
                      body.get("message") or body.get("error") or r.text[:200])
    return body


def remote_fetch(url, folder_uuid=None):
    payload = {"url": url}
    if folder_uuid:
        payload["folder_uuid"] = folder_uuid
    return _call("POST", "/remote-upload", json=payload)


def _root_files():
    return list(_call("GET", "/files?per_page=100").get("data") or [])


def _pick_new(before, files):
    """Newest file that was not in the snapshot, or None.

    upload.am has no "this file came from my remote fetch" field, so the
    snapshot difference is the only signal available.
    """
    fresh = [f for f in files if f.get("uuid") and f.get("uuid") not in before]
    if not fresh:
        return None
    fresh.sort(key=lambda f: f.get("created_at") or "", reverse=True)
    return fresh[0]


def _describe(f):
    return (f.get("name") or "file",
            f.get("size_bytes") or 0,
            f.get("download_page_url") or f"https://upload.am/d/{f.get('uuid')}")


def _poll(client, chat_id, msg_id, before, name_hint, task_id):
    deadline = time.time() + max(1, int(os.environ.get("UA_POLL_MIN", "20") or 20)) * 60
    last = 0.0
    while time.time() < deadline:
        time.sleep(POLL_EVERY)
        if state.is_cancelled(task_id):
            return safe_edit(client, chat_id, msg_id, "🛑 Stopped checking.", force=True)
        try:
            found = _pick_new(before, _root_files())
        except UaError as e:
            logger.warning(f"upload.am poll: {e.code} — {e}")
            if e.code in ("invalid_token", "account_disabled"):
                return safe_edit(client, chat_id, msg_id,
                                 f"❌ upload.am rejected the token: {esc(str(e))}", force=True)
            continue
        if found:
            name, size, link = _describe(found)
            state.drop_job(task_id)
            return safe_edit(client, chat_id, msg_id,
                             f"✅ <b>Fetched by upload.am</b>\n"
                             f"📄 <code>{esc(name)}</code> — {fmtsz(size)}\n"
                             f"🔗 {link}", force=True)
        now = time.time()
        if now - last > 60:
            last = now
            throttled_edit(client, chat_id, msg_id,
                           f"⏳ upload.am is still fetching <code>{esc(name_hint[:60])}</code>…\n"
                           f"<i>{int((deadline - now) // 60)} min before I stop checking.</i>")
    state.drop_job(task_id)
    safe_edit(client, chat_id, msg_id,
              f"⌛ Stopped waiting for <code>{esc(name_hint[:60])}</code>.\n"
              "The fetch may still finish — check <code>/ualist</code> in a few minutes, "
              "or the failure email if it died.", force=True)


def _auth(_, __, m: Message):
    return bool(m.from_user) and state.is_authorized(m.from_user.id)


Authorized = filters.create(_auth)


@app.on_message(filters.command("ua") & Authorized)
@guarded
def cmd_ua(client, message):
    if not token():
        return message.reply_text(
            "⚠️ <code>UA_TOKEN</code> is not set. Get one at upload.am → Account → API, "
            "then add <code>UA_TOKEN=ua_live_…</code> to .env.")

    parts = message.text.split(maxsplit=1)
    url = parts[1].strip() if len(parts) > 1 else ""
    if not url:
        reply = message.reply_to_message
        found = config.URL_RE.search((reply.text or reply.caption or "") if reply else "")
        url = found.group(0).rstrip(").,]>\"'") if found else ""
    if not url.startswith(("http://", "https://")):
        return message.reply_text(
            "🔗 <code>/ua &lt;direct file URL&gt;</code> — upload.am fetches it into your "
            "account, so nothing is downloaded to this server.\n"
            "Direct links only: the URL must start the download itself, not open a page.")

    name_hint = os.path.basename(urlparse(url).path) or url
    status = message.reply_text(f"📤 Asking upload.am to fetch <code>{esc(name_hint[:60])}</code>…")
    try:
        before = {f.get("uuid") for f in _root_files() if f.get("uuid")}
        remote_fetch(url, (os.environ.get("UA_FOLDER_UUID") or "").strip() or None)
    except UaError as e:
        hint = {
            "invalid_token": "token rejected — check UA_TOKEN",
            "email_not_verified": "confirm your upload.am email first",
            "unsafe_url": "that URL points at a private/internal address — refused",
            "rate_limited": "too many requests — wait a minute",
        }.get(e.code, str(e))
        return safe_edit(client, message.chat.id, status.id, f"❌ {esc(hint)}")

    task_id = f"ua{status.id}"
    state.register_job(task_id, message.chat.id, status.id, "uploadam", None)
    safe_edit(client, message.chat.id, status.id,
              f"📥 upload.am is fetching <code>{esc(name_hint[:60])}</code>…\n"
              "<i>No local disk used. I'll post the link when it lands.</i>")
    threading.Thread(target=_poll,
                     args=(client, message.chat.id, status.id, before, name_hint, task_id),
                     daemon=True, name="ua-poll").start()


@app.on_message(filters.command("ualist") & Authorized)
@guarded
def cmd_ualist(client, message):
    if not token():
        return message.reply_text("⚠️ <code>UA_TOKEN</code> is not set.")
    try:
        files = _root_files()
    except UaError as e:
        return message.reply_text(f"❌ upload.am: {esc(str(e))}")
    if not files:
        return message.reply_text("📂 Nothing in the upload.am root yet.")
    files.sort(key=lambda f: f.get("created_at") or "", reverse=True)
    lines = ["📂 <b>upload.am — newest in root</b>"]
    for f in files[:10]:
        name, size, link = _describe(f)
        lines.append(f"• <a href=\"{link}\">{esc(name)}</a> — {fmtsz(size)}")
    lines.append("\n<i>Free plan: files are deleted 30 days after the last download.</i>")
    message.reply_text("\n".join(lines))
