"""Public-link uploads: pixeldrain (multi-key rotation) + keyless x0.at fallback.

`/px` as a reply to a file → uploads it and returns a shareable pixeldrain link
plus a direct download URL. AzBot's own destinations are Telegram and Drive;
neither can hand an outside script a fetchable URL, which is the point here.

Env:
  PIXELDRAIN_API_KEYS=key1,key2,key3   # comma/newline separated, tried in order
  PIXELDRAIN_COOLDOWN_H=6              # hours a "full" key is skipped before retry
  PIXELDRAIN_FALLBACK=0                # set to disable the keyless x0.at fallback

Rotation: pixeldrain answers with machine-readable `value` codes, so the bot can
tell "this ACCOUNT is out of space" (rotate) from "this IP is rate limited"
(rotating is useless — say so instead of retrying every key for nothing).
"""
import hashlib
import json
import os
import shutil
import threading
import time
from urllib.parse import quote

import requests
from pyrogram import filters
from pyrogram.types import Message

from . import config, state, log
from .core import app
from .utils import bar, esc, fmtsz, guarded, new_task_id, safe_edit, throttled_edit

logger = log.get(__name__)

BASE = "https://pixeldrain.com/api"
STATE_FILE = os.path.join(config.DATA_DIR, "pd_keys.json")
COOLDOWN_S = max(1, int(os.environ.get("PIXELDRAIN_COOLDOWN_H", "6") or 6)) * 3600

# per-account problems: another key may well succeed
ROTATE_COOLDOWN = {"user_out_of_space", "too_many_files", "email_address_not_verified"}
# per-account problems that will not fix themselves
ROTATE_DEAD = {"authentication_required", "account_banned"}

_lock = threading.Lock()


class UploadError(Exception):
    pass


def _keys():
    raw = os.environ.get("PIXELDRAIN_API_KEYS") or os.environ.get("PIXELDRAIN_API_KEY") or ""
    return [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]


def _fallback_on():
    return (os.environ.get("PIXELDRAIN_FALLBACK", "1") or "1").strip() not in ("0", "false", "no")


def _fp(key):
    return hashlib.sha256(key.encode()).hexdigest()[:12]


def _load():
    try:
        with open(STATE_FILE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d):
    try:
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(d, f)
        os.replace(tmp, STATE_FILE)
    except Exception as e:
        logger.warning(f"pixeldrain: couldn't save key state: {e}")


def _order(keys, st, now):
    """Keys usable right now: dead and cooled-down (full) ones are skipped.

    Skipping is what makes the cooldown real — a key marked full is not retried
    on every upload, only after PIXELDRAIN_COOLDOWN_H. Last known good key first
    so the common case is a single attempt.
    """
    usable = []
    for k in keys:
        e = st.get(_fp(k), {})
        if e.get("state") == "dead":
            continue
        if e.get("state") == "full" and e.get("until", 0) > now:
            continue
        usable.append(k)
    last = st.get("_last_good")
    if last in usable:
        usable.remove(last)
        usable.insert(0, last)
    return usable


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
    if r.status_code == 201 and data.get("success"):
        return data
    raise _Err(r.status_code, data)


class _Err(UploadError):
    def __init__(self, status, data):
        self.status = status
        self.value = data.get("value") or f"http_{status}"
        super().__init__(data.get("message") or self.value)


def _x0(path):
    size = os.path.getsize(path)
    if size > 1024 * 1024 * 1024:
        raise UploadError("file is over x0.at's 1 GiB limit and every pixeldrain key is exhausted")
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
    keys = _keys()
    if not keys:
        if _fallback_on():
            return _x0(path)
        raise UploadError("no PIXELDRAIN_API_KEYS configured")

    if os.path.getsize(path) > 10 * 1024 ** 3:
        raise UploadError("file is over pixeldrain's 10 GB single-file cap")

    now = time.time()
    with _lock:
        st = _load()
        order = _order(keys, st, now)
        if not order:
            # everything is cooling down: probe the non-dead keys once anyway,
            # otherwise a recovered account would sit unused for the full window
            order = [k for k in keys if st.get(_fp(k), {}).get("state") != "dead"]

    last_err = None
    for key in order:
        try:
            data = _put(path, key, progress)
        except _Err as e:
            last_err = e
            if e.value in ROTATE_DEAD or e.value in ROTATE_COOLDOWN:
                with _lock:
                    st = _load()
                    st[_fp(key)] = {"state": "dead" if e.value in ROTATE_DEAD else "full",
                                    "until": now + COOLDOWN_S, "value": e.value}
                    _save(st)
                logger.warning(f"pixeldrain: key {_fp(key)} -> {e.value}, rotating")
                continue
            # file_too_large / ip_rate_limit_reached / ip_banned / writing — retrying
            # another key cannot help, and neither can the fallback for ip_banned.
            raise UploadError(f"pixeldrain: {e.value} — {e}")
        except Exception as e:
            raise UploadError(f"pixeldrain upload failed: {e}")

        fid = data.get("id")
        with _lock:
            st = _load()
            st.pop(_fp(key), None)
            st["_last_good"] = key
            _save(st)
        logger.info(f"pixeldrain: uploaded {os.path.basename(path)} as {fid} with key {_fp(key)}")
        return {"host": "pixeldrain", "url": f"https://pixeldrain.com/u/{fid}",
                "direct": f"{BASE}/file/{fid}?download", "note": ""}

    if _fallback_on():
        return _x0(path)
    why = f" ({last_err.value})" if last_err else ""
    raise UploadError(f"every pixeldrain key is out of space{why}")


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
    if not _keys() and not _fallback_on():
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
