"""Add & switch Drive accounts from Telegram via Google's OAuth 2.0
**device flow** — no console, no redirect URLs:

    /drivelogin <name>   → bot shows a google.com/device code + link
    (user opens it, signs in, types the code)
    → bot polls in the background, saves the refresh token as
      GDrive_<name>_REFRESH_TOKEN into data/overrides.json and registers
      the account live.

Requires GDrive_<name>_CLIENT_ID/_CLIENT_SECRET to already exist in .env
(client credentials can't be created by a bot), OR the shared
GCP_CLIENT_ID/GCP_CLIENT_SECRET pair — that is reused for new accounts.
"""
import os, re, time, threading, json
from urllib.parse import urlencode

from pyrogram import filters

from . import config, state, drive, datastore, log
from .core import app
from .utils import guarded, esc, safe_edit
from .handlers_core import Authorized, AdminOnly

logger = log.get(__name__)

DEVICE_URL = "https://accounts.google.com/device"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SCOPES = "https://www.googleapis.com/auth/drive"

OVERRIDES_FILE = os.path.join(config.DATA_DIR, "overrides.json")
_pending = {}   # chat_id -> {name, device_code, user_code, interval, expires}


def _client_pair(name):
    """Client ID/secret to use for account `name`: its own env vars if set,
    otherwise the shared GCP_* pair."""
    a = config.DRIVE_ACCOUNTS.get(name)
    if a:
        return a["client_id"], a["client_secret"]
    cid = os.environ.get("GCP_CLIENT_ID", "").strip()
    csec = os.environ.get("GCP_CLIENT_SECRET", "").strip()
    return (cid, csec) if cid and csec else (None, None)


def _save_override(name, refresh_token):
    data = state._load_json(OVERRIDES_FILE, {})
    data[f"GDrive_{name}_REFRESH_TOKEN"] = refresh_token
    state._save_json(OVERRIDES_FILE, data)
    # Mirror to the Drive datastore so tokens survive restarts/redeploys.
    try:
        from . import datastore
        datastore.push_datafile(OVERRIDES_FILE, "overrides.json")
    except Exception as e:
        logger.warning(f"override mirror failed: {e}")


def _register_account(name, refresh_token):
    """Registers/replaces an account at runtime + persists the token."""
    cid, csec = _client_pair(name)
    if not cid:
        raise RuntimeError(f"no client credentials for '{name}'")
    config.DRIVE_ACCOUNTS[name] = {
        "client_id": cid, "client_secret": csec, "refresh_token": refresh_token,
    }
    with drive._srv_lock:
        drive._srv_cache.pop(name, None)
    if not state.DEFAULT_ACCOUNT:
        state.DEFAULT_ACCOUNT = name
    _save_override(name, refresh_token)
    logger.info(f"drive account '{name}' registered via Telegram OAuth")


def _load_overrides_into_config():
    """Boot-time: apply saved tokens over env accounts."""
    data = state._load_json(OVERRIDES_FILE, {})
    n = 0
    for key, tok in data.items():
        m = key.match if False else None
        if not key.startswith("GDrive_") or not key.endswith("_REFRESH_TOKEN"):
            continue
        name = key[len("GDrive_"):-len("_REFRESH_TOKEN")]
        cid, csec = _client_pair(name)
        if cid and csec:
            config.DRIVE_ACCOUNTS[name] = {
                "client_id": cid, "client_secret": csec, "refresh_token": tok,
            }
            n += 1
    if n:
        logger.info(f"datastore: {n} Telegram-registered drive account(s) loaded")


def remove_account(name):
    """Removes a Telegram-registered account: token file entry, runtime
    registration, and any chat pointing at it falls back to default."""
    data = state._load_json(OVERRIDES_FILE, {})
    key = f"GDrive_{name}_REFRESH_TOKEN"
    if key not in data:
        raise RuntimeError("account wasn't added from Telegram")
    del data[key]
    state._save_json(OVERRIDES_FILE, data)
    try:
        from . import datastore
        datastore.push_datafile(OVERRIDES_FILE, "overrides.json")
    except Exception:
        pass
    config.DRIVE_ACCOUNTS.pop(name, None)
    with drive._srv_lock:
        drive._srv_cache.pop(name, None)
    logger.info(f"drive account '{name}' removed")


def start_device_login(client, message, name):
    """Old-bot flow: bot shows a Google authorization URL → user signs in,
    copies the code/link → sends it back. Works with ANY desktop-type OAuth
    client. Falls back to device sign-in codes only for TV-type clients."""
    name = re.sub(r"[^a-z0-9_-]", "", (name or "").lower())
    if not name:
        return message.reply_text("⚠️ Account name: letters/digits/-/_ only.")
    if name in config.DRIVE_ACCOUNTS:
        return message.reply_text(f"⚠️ Account <code>{esc(name)}</code> already exists.")
    cid, csec = _client_pair(name)
    if not cid:
        return message.reply_text(
            "❌ No OAuth client credentials found. Add "
            "<code>GDrive_" + esc(name) + "_CLIENT_ID/_CLIENT_SECRET</code> to `.env` "
            "(or a shared <code>GCP_CLIENT_ID/GCP_CLIENT_SECRET</code>), then retry.")

    state.set_awaiting_input(message.chat.id, ("drive_auth_code", name), None)
    # NOTE: Google shut down the legacy urn:ietf:wg:oauth:2.0:oob redirect —
    # using it yields "Error 400: redirect_uri_mismatch". The standard
    # copy/paste workaround: redirect to a throwaway localhost port; the
    # browser lands on a dead page whose ADDRESS BAR holds ?code=..., which
    # the user copies back to us (exactly the old-bot UX).
    redirect_uri = os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:1/")
    auth_url = ("https://accounts.google.com/o/oauth2/auth?"
                + urlencode({
                    "client_id": cid,
                    "redirect_uri": redirect_uri,
                    "response_type": "code",
                    "scope": SCOPES,
                    "access_type": "offline",
                    "prompt": "consent",
                  }))
    message.reply_text(
        f"☁️ <b>Please follow these steps to login:</b>\n\n"
        f"1️⃣ Open the <a href=\"{auth_url}\">Authorization URL</a>\n"
        f"2️⃣ Sign in with your Google Drive account\n"
        f"3️⃣ Give permission to access Google Drive\n"
        f"4️⃣ After approving you'll land on a page that <b>won't load</b> — "
        f"that's normal. Copy the <b>full URL</b> from the address bar "
        f"(it contains <code>?code=…</code>) and send it here.\n\n"
        f"💡 The account will be saved as <b>{esc(name)}</b>. "
        f"Send /cancel to abort.",
        disable_web_page_preview=True)


@app.on_message(filters.command(["drivelogin", "addaccount"]) & AdminOnly)
@guarded
def cmd_drivelogin(client, message):
    parts = message.text.split(maxsplit=1)
    existing = ", ".join(sorted(config.DRIVE_ACCOUNTS)) or "none"
    if len(parts) < 2 or not parts[1].strip():
        return message.reply_text(
            "☁️ <b>Add a Drive account</b>\n\n"
            f"1. Send: <code>/drivelogin &lt;name&gt;</code> (e.g. <code>/drivelogin work</code>)\n"
            f"…or use /settings → ☁️ Drive → ➕ Add account.\n\n"
            f"Active accounts: <code>{esc(existing)}</code>")
    start_device_login(client, message, parts[1].strip())


def _poll_approval(chat_id=None):
    """Polls the token endpoint until approved/expired."""
    chat_id = next(iter(_pending), None) if chat_id is None else chat_id
    p = _pending.get(chat_id)
    if not p:
        return
    cid, csec = _client_pair(p["name"])
    while time.time() < p["expires"]:
        time.sleep(p["interval"])
        try:
            import requests as rq
            r = rq.post(TOKEN_URL, data={
                "client_id": cid, "client_secret": csec,
                "device_code": p["device_code"],
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
            }, timeout=15)
            d = r.json()
        except Exception as e:
            logger.warning(f"drivelogin poll error: {e}")
            continue
        if "refresh_token" in d:
            try:
                _register_account(p["name"], d["refresh_token"])
            except Exception as e:
                logger.exception("drivelogin register failed")
                app.send_message(chat_id, f"❌ Registered but failed to activate: <code>{esc(e)}</code>")
                _pending.pop(chat_id, None)
                return
            app.send_message(
                chat_id,
                f"✅ Drive account <b>{esc(p['name'])}</b> added!\n"
                f"Switch chats to it in /settings → ☁️ Drive account.")
            _pending.pop(chat_id, None)
            return
        err = d.get("error")
        if err == "authorization_pending":
            continue
        if err == "slow_down":
            p["interval"] += 5
            continue
        app.send_message(chat_id, f"❌ Login failed: <code>{esc(err or str(d)[:200])}</code>")
        _pending.pop(chat_id, None)
        return
    app.send_message(chat_id, "⏱️ Device code expired — run /drivelogin again.")


@app.on_message(filters.command(["tokenup", "tokenpaste"]) & AdminOnly)
@guarded
def cmd_tokenup(client, message):
    """Register a Drive account from a refresh token produced on a PC
    (gen_token.py) — for clients that can't use the device sign-in flow."""
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return message.reply_text(
            "☁️ <code>/tokenup &lt;name&gt;</code> — then paste the refresh token "
            "in your next message (from <code>gen_token.py</code> on your PC), or reply "
            "to a .txt/.pickle file containing it.")
    name = re.sub(r"[^a-z0-9_-]", "", parts[1].strip().lower())
    if not name:
        return message.reply_text("⚠️ Invalid name.")
    state.set_awaiting_input(message.chat.id, ("drive_token", name), None)
    message.reply_text(f"🔑 Send the refresh token for <b>{esc(name)}</b> now (plain text, or as a file).")


def _handle_token_input(client, message, name):
    token = ""
    if message.reply_to_message and message.reply_to_message.document:
        path = client.download_media(message.reply_to_message,
                                      file_name=os.path.join(config.DATA_DIR, f"tok_{name}"))
        if path:
            token = open(path, encoding="utf-8", errors="replace").read().strip()
            os.remove(path)
    else:
        token = (message.text or "").strip()
    # tolerate pasted JSON from gen_token-style tools
    if token.startswith("{"):
        try:
            import json as _j
            token = _j.loads(token).get("refresh_token", "")
        except Exception:
            pass
    token = [l for l in token.splitlines() if l.strip() and not l.startswith("#")]
    token = token[-1].strip() if token else ""
    if not token or len(token) < 20:
        return message.reply_text("❌ That doesn't look like a refresh token. Try again: /tokenup " + name)
    try:
        _register_account(name, token)
    except Exception as e:
        return message.reply_text(f"❌ {esc(e)}")
    message.reply_text(
        f"✅ Drive account <b>{esc(name)}</b> registered and saved (survives restarts). "
        f"Switch chats to it in /settings → ☁️ Drive.")


def _exchange_auth_code(client, message, name, text):
    """Step 2 of the old-bot flow: user pasted the code or the redirect
    link — exchange it for a refresh token and register the account."""
    raw = (text or "").strip()
    # accept either the bare code or a full redirect URL containing ?code=
    m = re.search(r"[?&]code=([\w\-]+)", raw)
    code = m.group(1) if m else raw.split()[0]
    cid, csec = _client_pair(name)
    if not cid:
        return message.reply_text("❌ Client credentials vanished — start again with /drivelogin.")
    status = message.reply_text("🔑 Exchanging authorization code…")
    import requests as rq
    try:
        r = rq.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": cid,
            "client_secret": csec,
            "redirect_uri": os.environ.get("GOOGLE_REDIRECT_URI", "http://localhost:1/"),
            "grant_type": "authorization_code",
        }, timeout=20)
        d = r.json()
    except Exception as e:
        return safe_edit(client, message.chat.id, status.id, f"❌ {esc(e)}")
    if "refresh_token" not in d:
        err = d.get("error_description") or str(d)[:200]
        hint = ""
        if "code was already redeemed" in err:
            hint = "\n<i>The code can only be used once — send /drivelogin again for a fresh URL.</i>"
        elif "invalid_grant" in err:
            hint = "\n<i>Code expired or wrong — send /drivelogin again.</i>"
        return safe_edit(client, message.chat.id, status.id,
                          f"❌ Google said: <code>{esc(err[:250])}</code>{hint}")
    try:
        _register_account(name, d["refresh_token"])
    except Exception as e:
        return safe_edit(client, message.chat.id, status.id, f"❌ {esc(e)}")
    safe_edit(
        client, message.chat.id, status.id,
        f"✅ Drive account <b>{esc(name)}</b> added and saved (survives restarts).\n"
        f"Switch chats to it in /settings → ☁️ Drive.")


@app.on_message(filters.command(["drivelist", "driveaccounts"]) & AdminOnly)
@guarded
def cmd_drivelist(client, message):
    rows = []
    for name, acct in sorted(config.DRIVE_ACCOUNTS.items()):
        rows.append(f"• <code>{esc(name)}</code> — …{esc(acct['refresh_token'][-6:])}")
    message.reply_text(
        "☁️ <b>Registered Drive accounts</b>\n\n"
        + ("\n".join(rows) if rows else "_none — use /drivelogin or .env_"))
