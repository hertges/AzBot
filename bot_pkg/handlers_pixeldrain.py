"""Public-link uploads: pixeldrain (multi-key rotation) + keyless x0.at fallback.

`/px` as a reply to a file → uploads it and returns a shareable pixeldrain link
plus a direct download URL. AzBot's own destinations are Telegram and Drive;
neither can hand an outside script a fetchable URL, which is the point here.

Env:
  PIXELDRAIN_API_KEYS=key1,key2,key3   # comma/newline separated, tried in order
  PIXELDRAIN_API_KEY=key1              # singular form, still accepted
  PIXELDRAIN_COOLDOWN_H=6              # hours a full account is skipped
  PIXELDRAIN_FALLBACK=1                # 0 disables the keyless x0.at fallback

Rotation lives in keyring.py. pixeldrain answers with machine-readable `value`
codes, which is what lets the bot tell "this ACCOUNT is out of space" (rotate)
from "this IP is rate limited" (rotating is useless — say so rather than retry
every key for nothing).

Errors you can hit, and what happens:
  user_out_of_space / too_many_files       account is full → next key
  email_address_not_verified               account can't upload yet → next key
  authentication_required / account_banned key revoked or banned → dropped for good
  ip_banned                                this server's IP is banned → hard error
  file_too_large                           over 10 GB, or over the plan cap → hard error
  ip_rate_limit_reached (429)              per-IP limit → hard error, rotation won't help
  writing / internal (5xx)                 pixeldrain-side problem → hard error
  every key unusable                       → x0.at fallback (1 GiB, keyless)
"""
import os
import shutil
import time
from urllib.parse import quote

import requests
from pyrogram import filters
from pyrogram.types import Message

from . import config, state, log
from .core import app
from .keyring import Keyring
from .utils import bar, esc, fmtsz, guarded, new_task_id, safe_edit, throttled_edit

logger = log.get(__name__)

BASE = "https://pixeldrain.com/api"

# pixeldrain documents stable `value` codes for every failure, so rotation can be
# decided on the code instead of guessing from the HTTP status.
KEYS = Keyring(
    "pixeldrain", "PIXELDRAIN_API_KEYS",
    extra_env=("PIXELDRAIN_API_KEY",),
    rotate={"user_out_of_space", "too_many_files", "email_address_not_verified"},
    dead={"authentication_required", "account_banned", "ip_banned"},
    cooldown_s=max(1, int(os.environ.get("PIXELDRAIN_COOLDOWN_H", "6") or 6)) * 3600,
)


class UploadError(Exception):
    pass


def _fallback_on():
    return (os.environ.get("PIXELDRAIN_FALLBACK", "1") or "1").strip() not in ("0", "false", "no")


def _put(path, key, progress):
    """PUT the raw file body. Returns parsed JSON on success, raises on failure."""
    name = os.path.basename(path)
    size = os.path.getsize(path)

    def body():
        sent = 0
        with open(path, "rb") as f:
            while True:
                chunk = f.read(1 << 20)
                if not chunk:
                    return
                sent += len(chunk)
                if progress:
                    progress(sent, size)
                yield chunk

    r = requests.put(f"{BASE}/file/{quote(name, safe='')}", data=body(), auth=("", key),
                     headers={"Content-Length": str(size)}, timeout=(30, 300))
    try:
        data = r.json()
    except Exception:
        data = {}
    if r.status_code in (200, 201) and data.get("success"):
        return data
    raise _Err(r.status_code, data)


class _Err(UploadError):
    def __init__(self, status, data):
        self.status = status
        self.value = data.get("value") or f"http_{status}"
        self.message = data.get("message") or self.value
        super().__init__(self.message)


def _x0(path):
    size = os.path.getsize(path)
    if size > 1024 * 1024 * 1024:
        raise UploadError("file is over x0.at's 1 GiB limit and no pixeldrain key accepted it")
    with open(path, "rb") as f:
        r = requests.post("https://x0.at", files={"file": (os.path.basename(path), f)},
                          timeout=(30, 600))
    url = (r.text or "").strip()
    if r.status_code != 200 or not url.startswith("http"):
        raise UploadError(f"x0.at refused it (HTTP {r.status_code}): {url[:120]}")
    return {"host": "x0.at", "url": url, "direct": url,
            "note": "fallback host — no pixeldrain key could take it; x0.at keeps small files longest"}


def upload(path, progress=None):
    """Upload to the first pixeldrain key that accepts it, else the fallback host.

    Returns {"host", "url", "direct", "note"}.
    """
    if os.path.getsize(path) > 10 * 1024 ** 3:
        raise UploadError("file is over pixeldrain's 10 GB single-file cap")

    order = KEYS.order()
    if not order:
        if _fallback_on():
            return _x0(path)
        raise UploadError("no PIXELDRAIN_API_KEYS configured, and the fallback is off")

    last = None
    for key in order:
        try:
            data = _put(path, key, progress)
        except _Err as e:
            last = e
            kind = KEYS.classify(e.value)
            if kind == "dead":
                KEYS.mark_dead(key, e.value)
                continue
            if kind == "rotate":
                KEYS.mark_full(key, e.value)
                continue
            # file_too_large / ip_rate_limit_reached / writing / internal: another
            # key cannot help, and the fallback cannot rescue an IP ban either.
            raise UploadError(f"pixeldrain: {e.value} — {e.message}")
        except Exception as e:
            raise UploadError(f"pixeldrain upload failed: {e}")

        fid = data.get("id")
        KEYS.mark_good(key)
        logger.info(f"pixeldrain: uploaded {os.path.basename(path)} as {fid} "
                    f"with key {KEYS.fp(key)}")
        return {"host": "pixeldrain", "url": f"https://pixeldrain.com/u/{fid}",
                "direct": f"{BASE}/file/{fid}?download", "note": ""}

    if _fallback_on():
        return _x0(path)
    why = f" ({last.value})" if last else ""
    raise UploadError(f"every pixeldrain key is unusable{why}")


# ── /px handler ────────────────────────────────────────────────────────────
# Runs in the handler thread pool (utils.guarded), so a long upload can't
# freeze the network loop. No queue job, no cancel button: one file, one reply.

def _auth(_, __, m: Message):
    return bool(m.from_user) and state.is_authorized(m.from_user.id)


Authorized = filters.create(_auth)


@app.on_message(filters.command("px") & Authorized)
@guarded
def cmd_px(client, message):
    reply = message.reply_to_message
    if not reply or not (reply.media or reply.document):
        return message.reply_text(
            "🔗 Reply to a file with <code>/px</code> and I'll upload it to "
            "pixeldrain and return a public link.")
    if not KEYS.configured() and not _fallback_on():
        return message.reply_text(
            "⚠️ Nothing configured: set <code>PIXELDRAIN_API_KEYS</code> in .env "
            "or re-enable the fallback host.")

    task_id = new_task_id()
    work = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(work, exist_ok=True)
    status = message.reply_text("⬇️ Fetching the file from Telegram…")
    try:
        path = client.download_media(reply, file_name=os.path.join(work, ""))
        if not path:
            return safe_edit(client, message.chat.id, status.id, "❌ Couldn't download that file.")
        name, size = os.path.basename(path), os.path.getsize(path)
        last = [0.0]

        def prog(done, total):
            now = time.time()
            if now - last[0] < 1.5 and done < total:
                return
            last[0] = now
            throttled_edit(client, message.chat.id, status.id,
                           f"⬆️ Uploading <code>{esc(name)}</code> ({fmtsz(size)})\n{bar(done, total)}")

        res = upload(path, prog)
        lines = [f"✅ <b>Uploaded to {res['host']}</b> — {fmtsz(size)}",
                 f"🔗 {res['url']}"]
        if res.get("direct") and res["direct"] != res["url"]:
            lines.append(f"⬇️ Direct: {res['direct']}")
        if res.get("note"):
            lines.append(f"ℹ️ {esc(res['note'])}")
        safe_edit(client, message.chat.id, status.id, "\n".join(lines))
    except UploadError as e:
        safe_edit(client, message.chat.id, status.id, f"❌ {esc(e)}")
    finally:
        shutil.rmtree(work, ignore_errors=True)
