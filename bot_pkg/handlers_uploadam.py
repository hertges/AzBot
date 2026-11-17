"""upload.am — let THEIR servers fetch the URL, so big files never touch this disk.

`/ua <url>` asks upload.am to fetch the file into your account over its own
connection. Nothing is downloaded here, which is the whole point on a box with
a small disk: a 4 GB file costs the bot zero bytes of local storage.

The fetch is async on their side and there is no progress endpoint, so the
handler snapshots the root file list, queues the fetch, then polls until a file
that wasn't there before shows up.

Env:
  UA_TOKENS=ua_live_a,ua_live_b       # comma/newline separated, tried in order
  UA_TOKEN=ua_live_a                  # singular form, still accepted
  UA_FOLDER_UUID=                     # optional destination folder, default account root
  UA_POLL_MIN=20                      # minutes to wait for the file to appear
  UA_COOLDOWN_H=6                     # hours a full/429-hit token is skipped

Multi-token rotation (keyring.py): when one account can't fetch, the bot uses the
next token. upload.am counts quotas and rate limits per token, so rotation DOES
help for quota/rate-limit errors (unlike pixeldrain's per-IP rate limit).

Errors and what the bot does:
  invalid_token, expired_token, account_disabled   dead credential → dropped, try next
  email_not_verified                               account can't upload → next token
  quota_exceeded, file_too_large, plan_limit_...   plan quota used up → next token
  429 rate_limited                                 per-TOKEN limit → cool down, next token
  unsafe_url                                       refused URL → immediate stop, no rotation
  network, timeout                                 transient → do not rotate, report error
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
from .keyring import Keyring
from .utils import esc, fmtsz, guarded, safe_edit, throttled_edit

logger = log.get(__name__)

BASE = "https://upload.am/api/v1"
POLL_EVERY = 10

KEYS = Keyring(
    "uploadam", "UA_TOKENS",
    extra_env=("UA_TOKEN",),
    dead={"invalid_token", "expired_token", "account_disabled"},
    rotate={"quota_exceeded", "file_too_large", "plan_limit_reached",
            "plan_feature_locked", "email_not_verified", "rate_limited"},
    cooldown_s=max(1, int(os.environ.get("UA_COOLDOWN_H", "6") or 6)) * 3600,
)


class UaError(Exception):
    def __init__(self, code, msg):
        self.code = code
        super().__init__(msg or code)


def _headers(tok):
    return {"Authorization": f"Bearer {tok}", "Content-Type": "application/json"}


def _call(method, path, tok, **kw):
    """One call with one token. Raises UaError carrying the server's code."""
    try:
        r = requests.request(method, BASE + path, headers=_headers(tok),
                             timeout=(20, 90), **kw)
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


def _call_rotating(method, path, **kw):
    """Try each token in turn. Returns (body, token) so the caller knows the owner.

    Which token matters: /files is scoped per account, so the poll must list the
    same account the fetch was queued on, or it watches the wrong list forever.
    """
    order = KEYS.order()
    if not order:
        raise UaError("no_tokens", "no UA_TOKENS configured")
    last = None
    for tok in order:
        try:
            body = _call(method, path, tok, **kw)
            KEYS.mark_good(tok)
            return body, tok
        except UaError as e:
            last = e
            kind = KEYS.classify(e.code)
            if kind == "dead":
                KEYS.mark_dead(tok, e.code)
                continue
            if kind == "rotate":
                KEYS.mark_full(tok, e.code)
                continue
            # unsafe_url / network / other 4xx: another account cannot fix it
            raise
    raise UaError("all_tokens_exhausted", f"every token failed (last: {last.code if last else '?'})")


def remote_fetch(url, folder_uuid=None):
    payload = {"url": url}
    if folder_uuid:
        payload["folder_uuid"] = folder_uuid
    return _call_rotating("POST", "/remote-upload", json=payload)


def _root_files(tok=None):
    """File list; with tok, only that account's (the poll needs the same one)."""
    if tok:
        return list(_call("GET", "/files?per_page=100", tok).get("data") or [])
    body, _ = _call_rotating("GET", "/files?per_page=100")
    return list(body.get("data") or [])


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


def _poll(client, chat_id, msg_id, before, name_hint, task_id, tok):
    deadline = time.time() + max(1, int(os.environ.get("UA_POLL_MIN", "20") or 20)) * 60
    last = 0.0
    while time.time() < deadline:
        time.sleep(POLL_EVERY)
        if state.is_cancelled(task_id):
            return safe_edit(client, chat_id, msg_id, "🛑 Stopped checking.", force=True)
        try:
            found = _pick_new(before, _root_files(tok))
        except UaError as e:
            logger.warning(f"upload.am poll: {e.code} — {e}")
            if KEYS.classify(e.code) == "dead":
                KEYS.mark_dead(tok, e.code)
                return safe_edit(client, chat_id, msg_id,
                                 f"❌ upload.am rejected the token used for this fetch "
                                 f"({esc(e.code)}) — the file may still arrive; "
                                 f"check <code>/ualist</code>.", force=True)
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
    if not KEYS.configured():
        return message.reply_text(
            "⚠️ No upload.am token configured. Get one at upload.am → Account → API, "
            "then add <code>UA_TOKENS=ua_live_…</code> to .env "
            "(comma-separated for several accounts).")

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
        # Snapshot EVERY account before queuing: rotation may land on any of them,
        # and a snapshot taken afterwards would already contain the new file.
        snaps = {}
        for t in KEYS.order():
            try:
                snaps[t] = {f.get("uuid") for f in _root_files(t) if f.get("uuid")}
            except UaError as e:
                logger.warning(f"upload.am: snapshot skipped for {KEYS.fp(t)}: {e.code}")
        _, tok = remote_fetch(url, (os.environ.get("UA_FOLDER_UUID") or "").strip() or None)
        before = snaps.get(tok, set())
    except UaError as e:
        hint = {
            "no_tokens": "no UA_TOKENS configured",
            "invalid_token": "token rejected — check UA_TOKENS",
            "expired_token": "token expired — generate a new one on upload.am",
            "account_disabled": "that upload.am account is disabled",
            "email_not_verified": "confirm your upload.am email first",
            "unsafe_url": "that URL points at a private/internal address — refused",
            "rate_limited": "too many requests — wait a minute",
            "all_tokens_exhausted": "every upload.am account is full or rejected the file",
            "network": f"couldn't reach upload.am ({e})",
        }.get(e.code, str(e))
        return safe_edit(client, message.chat.id, status.id, f"❌ {esc(hint)}")

    task_id = f"ua{status.id}"
    state.register_job(task_id, message.chat.id, status.id, "uploadam", None)
    safe_edit(client, message.chat.id, status.id,
              f"📥 upload.am is fetching <code>{esc(name_hint[:60])}</code>…\n"
              "<i>No local disk used. I'll post the link when it lands.</i>")
    threading.Thread(target=_poll,
                     args=(client, message.chat.id, status.id, before, name_hint, task_id, tok),
                     daemon=True, name="ua-poll").start()


@app.on_message(filters.command("ualist") & Authorized)
@guarded
def cmd_ualist(client, message):
    if not KEYS.configured():
        return message.reply_text("⚠️ No upload.am token configured (<code>UA_TOKENS</code>).")
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
