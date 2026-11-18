"""Instagram DM downloads — the transport-agnostic core.

Users DM a link to one of this bot's Instagram Professional accounts; the
webhook (ig_webhook.py) hands the event here, this module classifies it,
downloads with gallery-dl, and delivers to the Telegram user who linked that
Instagram account with /directdownload.

Only Meta's official Instagram Messaging API can read DMs. That means a
Professional account, a linked Facebook Page, and a public HTTPS webhook —
there is no password-based path, and this module never asks for one.

Linking: /directdownload issues a short code; the user sends that code as a DM
to the bot's Instagram account. The webhook matches the code and stores
IGSID → Telegram chat. Unlinked senders are told how to link and ignored.

Supported: posts (single image), carousels, reels, stories, highlights.
Not supported: live streams (no official download API, and re-streaming
someone else's live is a legal problem, not just a technical one), profile
archives over DM (use /ig), disappearing/view-once media, and DMs containing
only a gif or sticker (Meta sends no webhook at all for those).
"""
import json
import os
import re
import secrets
import shutil
import subprocess
import threading
import time

import requests

from . import config, state, log
from .core import app
from .utils import esc, fmtsz, new_task_id, throttled_edit, cancel_kb

logger = log.get(__name__)

STORE = os.path.join(config.DATA_DIR, "ig_dm.json")
CODE_TTL_S = 30 * 60

_lock = threading.Lock()

# ── content classification ────────────────────────────────────────────────
# Order matters: /stories/highlights/<id> must be tested before the plain
# story pattern, which would otherwise read "highlights" as a username.
KINDS = [
    ("highlight", re.compile(r"instagram\.com/stories/highlights/(\d+)")),
    ("story",     re.compile(r"instagram\.com/stories/([A-Za-z0-9._]+)/(\d+)")),
    ("live",      re.compile(r"instagram\.com/live/(\d+)")),
    ("reel",      re.compile(r"instagram\.com/(?:reel|reels)/([A-Za-z0-9_-]+)")),
    ("post",      re.compile(r"instagram\.com/(?:[A-Za-z0-9._]+/)?(?:p|tv)/([A-Za-z0-9_-]+)")),
]

LABEL = {
    "post": "🖼 Post", "carousel": "🎠 Carousel", "reel": "🎬 Reel",
    "story": "⏳ Story", "highlight": "⭐ Highlight", "live": "🔴 Live",
}

# Features the user asked for, and where each one actually stands. Rendered by
# /directdownload so supported and unsupported are never confused.
FEATURES = [
    ("post", "✅", "Posts (single image or video)"),
    ("carousel", "✅", "Carousel posts (every slide)"),
    ("reel", "✅", "Reels"),
    ("story", "✅", "Stories — needs an Instagram cookie profile (/cookie)"),
    ("highlight", "✅", "Highlights — needs the same cookie profile"),
    ("live", "❌", "Live streams — Instagram has no download API for these, "
                   "and re-streaming someone else's live is a legal problem too"),
]

NEEDS_COOKIE = {"story", "highlight"}


def classify(text):
    """('kind', url) for a DM body, or (None, None) when there's no IG link."""
    for url in config.URL_RE.findall(text or ""):
        for kind, rx in KINDS:
            if rx.search(url):
                return kind, url
    return None, None


# ── link store: IGSID ↔ Telegram chat ─────────────────────────────────────
def _load():
    try:
        with open(STORE) as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def _save(d):
    try:
        tmp = STORE + ".tmp"
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, "w") as f:
            json.dump(d, f, indent=1)
        os.replace(tmp, STORE)
        os.chmod(STORE, 0o600)
    except Exception as e:
        logger.warning(f"ig_dm: couldn't save links: {e}")


def issue_code(chat_id):
    """A short code the user sends to the bot's Instagram account to link."""
    code = secrets.token_hex(3).upper()
    with _lock:
        d = _load()
        now = time.time()
        d["codes"] = {c: v for c, v in (d.get("codes") or {}).items() if v.get("exp", 0) > now}
        d.setdefault("codes", {})[code] = {"chat_id": chat_id, "exp": now + CODE_TTL_S}
        _save(d)
    return code


def link(igsid, code):
    """Consume a code. Returns the Telegram chat_id, or None if it doesn't fit."""
    with _lock:
        d = _load()
        entry = (d.get("codes") or {}).pop(code, None)
        if not entry or entry.get("exp", 0) < time.time():
            _save(d)
            return None
        d.setdefault("links", {})[str(igsid)] = {"chat_id": entry["chat_id"], "at": time.time()}
        _save(d)
    logger.info(f"ig_dm: linked IGSID {str(igsid)[-6:]} → chat {entry['chat_id']}")
    return entry["chat_id"]


def chat_for(igsid):
    return ((_load().get("links") or {}).get(str(igsid)) or {}).get("chat_id")


def unlink(chat_id):
    with _lock:
        d = _load()
        links = d.get("links") or {}
        gone = [k for k, v in links.items() if v.get("chat_id") == chat_id]
        for k in gone:
            del links[k]
        d["links"] = links
        _save(d)
    return len(gone)


# ── Meta API helpers ──────────────────────────────────────────────────────
def enabled():
    return bool(config.IG_PAGE_TOKEN and config.IG_APP_SECRET)


def send_ig_message(igsid, text):
    """Best-effort reply in the DM itself. Failure is logged, never fatal —
    the Telegram delivery is the real product."""
    if not enabled():
        return False
    try:
        r = requests.post(
            f"{config.IG_API_HOST}/{config.IG_API_VERSION}/{config.IG_ACCOUNT_ID}/messages",
            headers={"Authorization": f"Bearer {config.IG_PAGE_TOKEN}"},
            json={"recipient": {"id": igsid}, "message": {"text": text[:900]}},
            timeout=(10, 20))
        if r.status_code >= 400:
            logger.warning(f"ig_dm: IG reply failed {r.status_code}: {r.text[:200]}")
        return r.status_code < 400
    except Exception as e:
        logger.warning(f"ig_dm: IG reply error: {e}")
        return False


# ── event handling ────────────────────────────────────────────────────────
LINK_HINT = ("Send /directdownload in Telegram first, then send me the code it "
             "gives you here. Without that I don't know which Telegram account "
             "to deliver to.")


def handle_event(entry):
    """One webhook `messaging` event. Returns a short status string for the log."""
    msg = entry.get("message") or {}
    if msg.get("is_echo") or msg.get("is_deleted") or msg.get("is_unsupported"):
        return "ignored: echo/deleted/unsupported"
    igsid = (entry.get("sender") or {}).get("id")
    if not igsid:
        return "ignored: no sender"

    text = (msg.get("text") or "").strip()
    chat_id = chat_for(igsid)

    # 1) a link code from /directdownload
    if text and re.fullmatch(r"[0-9A-F]{6}", text):
        linked = link(igsid, text)
        if linked:
            send_ig_message(igsid, "✅ Linked. Send me any Instagram post, reel, "
                                   "story or highlight link and it lands in Telegram.")
            try:
                app.send_message(linked, "✅ Instagram connected. Send links to the "
                                         "bot's Instagram account from now on — "
                                         "they'll arrive here as files.")
            except Exception as e:
                logger.warning(f"ig_dm: link confirmation send failed: {e}")
            return f"linked chat {linked}"
        send_ig_message(igsid, "❌ That code is wrong or expired. "
                               "Run /directdownload again for a fresh one.")
        return "bad code"

    # 2) media the user sent straight to the bot (their own content)
    urls = []
    for att in (msg.get("attachments") or []):
        u = (att.get("payload") or {}).get("url")
        if u and (att.get("type") in ("image", "video", "share", "ig_reel", "reel")):
            urls.append((att.get("type"), u))

    kind, url = classify(text)
    if not kind and urls:
        kind, url = "attachment", urls[0][1]

    if not kind:
        if not chat_id:
            send_ig_message(igsid, "Send me an Instagram link and I'll fetch it. " + LINK_HINT)
            return "no link, unlinked sender"
        send_ig_message(igsid, "I couldn't find an Instagram link in that. "
                               "Supported: post, carousel, reel, story, highlight links.")
        return "no link"

    if kind == "live":
        send_ig_message(igsid, "🔴 Live streams aren't supported: Instagram exposes no "
                               "download API for them, and recording someone else's live "
                               "is a legal issue as much as a technical one. Once the "
                               "broadcast ends and is saved as a reel, send that link.")
        return "live refused"

    if not chat_id:
        send_ig_message(igsid, "I need to know who to deliver to. " + LINK_HINT)
        return "unlinked sender"

    if not state.is_authorized(chat_id):
        send_ig_message(igsid, "That Telegram account isn't allowed to use this bot.")
        return "chat not authorized"

    if kind in NEEDS_COOKIE and not state.active_cookie_file(chat_id):
        send_ig_message(igsid, "⏳ Stories and highlights need a logged-in Instagram "
                               "session. In Telegram run /cookie and add one, then resend.")
        return "needs cookie"

    task_id = new_task_id()
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    status = None
    try:
        status = app.send_message(
            chat_id, f"{LABEL.get(kind, '📷')} — fetching <code>{esc(url[:120])}</code>…",
            reply_markup=cancel_kb(task_id))
    except Exception as e:
        logger.warning(f"ig_dm: couldn't post status to chat {chat_id}: {e}")
        shutil.rmtree(task_dir, ignore_errors=True)
        send_ig_message(igsid, "📥 Couldn't reach Telegram just now — try again in a minute.")
        return "telegram send failed"

    state.register_job(task_id, chat_id, status.id, f"igdm:{kind}", task_dir)
    state.task_queue.put((run_dm_job, (app, chat_id, status.id, kind, url, task_id)))
    send_ig_message(igsid, "📥 Got it — fetching now. The files will appear in Telegram.")
    return f"queued {kind} for chat {chat_id}"


# ── the job ───────────────────────────────────────────────────────────────
def run_dm_job(client, chat_id, msg_id, kind, url, task_id):
    """Download one link with gallery-dl, upload whatever lands, summarise."""
    from .dispatcher import LiveDispatcher
    from .handlers_instagram import GDL_TUNING_ARGS  # lazy: avoids import cycle

    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    disp = None
    try:
        if not shutil.which("gallery-dl"):
            return throttled_edit(client, chat_id, msg_id,
                                  "❌ <code>gallery-dl</code> isn't installed on this host.",
                                  force=True)

        args = ["gallery-dl", "--dest", task_dir] + GDL_TUNING_ARGS
        cookie = state.active_cookie_file(chat_id)
        if cookie:
            args += ["--cookies", cookie]
        args.append(url)

        disp = LiveDispatcher(client, chat_id, msg_id, task_dir, "telegram",
                              task_id=task_id, quiet=True,
                              caption_prefix=LABEL.get(kind, "📷"))
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                universal_newlines=True, bufsize=1, start_new_session=True)
        state.attach_proc(task_id, proc)
        tail = []
        for line in iter(proc.stdout.readline, ""):
            if state.is_cancelled(task_id):
                break
            tail.append(line.rstrip())
            tail = tail[-15:]
        proc.wait()

        if state.is_cancelled(task_id):
            disp.stop()
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)

        sent, errors = disp.finalize(timeout=900)
        if sent == 0:
            why = "nothing came back"
            log_tail = " ".join(tail).lower()
            if "login" in log_tail or "401" in log_tail or "cookie" in log_tail:
                why = "Instagram wants a login — add or refresh a cookie profile with /cookie"
            elif "404" in log_tail or "not found" in log_tail:
                why = "that link no longer exists, or the account is private"
            elif "429" in log_tail or "rate" in log_tail:
                why = "Instagram rate-limited this server — try again later"
            return throttled_edit(client, chat_id, msg_id,
                                  f"❌ {LABEL.get(kind, '📷')} failed: {esc(why)}", force=True)

        total = sum(os.path.getsize(os.path.join(r, f))
                    for r, _, fs in os.walk(task_dir) for f in fs) if os.path.isdir(task_dir) else 0
        lines = [f"✅ <b>{LABEL.get(kind, '📷')} delivered</b>",
                 f"📥 <code>{sent}</code> file(s)"
                 + (f" • 📦 {fmtsz(total)}" if total else "")]
        if errors:
            lines.append(f"⚠️ <code>{len(errors)}</code> failed to send")
        throttled_edit(client, chat_id, msg_id, "\n".join(lines), force=True)
    except Exception as e:
        logger.exception(f"[{task_id}] ig_dm job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        if disp is not None:
            disp.stop()
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)
