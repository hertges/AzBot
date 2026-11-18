"""Instagram access token: storage, expiry tracking, and automatic renewal.

Meta's tokens expire, and an expired token is silent — DMs simply stop
arriving. Two mechanisms prevent that:

  * **Automatic.** An Instagram Login token (graph.instagram.com) can be
    refreshed with `GET /refresh_access_token?grant_type=ig_refresh_token`.
    Each refresh returns a token valid for 60 more days, so a bot that
    refreshes itself never expires while it keeps running. This module checks
    every few hours and refreshes once fewer than REFRESH_BELOW_DAYS remain.
    The refreshed token is written to data/ig_token.json (mode 0600) and takes
    precedence over the env value, so a restart keeps the fresh one.

  * **Manual.** `/igtoken <token>` (admin only) stores a token pasted in
    Telegram, validates it against the API first, and deletes the message so
    the token doesn't sit in chat history. This is the path for Facebook Login
    Page tokens, which have no refresh endpoint, and for a token that expired
    while the bot was off.

The token itself is never logged. Errors log the API's own message only.
"""
import json
import os
import threading
import time

import requests

from . import config, log

logger = log.get(__name__)

STORE = os.path.join(config.DATA_DIR, "ig_token.json")
REFRESH_BELOW_DAYS = 10      # refresh with this much runway left
CHECK_EVERY_S = 6 * 3600     # how often the watchdog looks

_lock = threading.Lock()
_started = False

def _days(n):
    n = int(round(n))
    return f"{n} day{'' if n == 1 else 's'}"


STATUS = {
    "expired":    "❌ expired — paste a new token with <code>/igtoken &lt;token&gt;</code>",
    "expiring":   "⚠️ expiring soon",
    "ok":         "✅ valid",
    "unknown":    "❔ expiry unknown",
    "no_token":   "❌ no token configured",
}


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
            json.dump(d, f)
        os.replace(tmp, STORE)
        os.chmod(STORE, 0o600)
    except Exception as e:
        logger.warning(f"ig_token: couldn't save token: {e}")


def token():
    """The token in force: the stored one if present, else the env value.

    File wins because a refresh (or a manual /igtoken) must survive a restart;
    rewriting .env at runtime would be a worse idea than keeping a 0600 file.
    """
    return (_load().get("token") or config.IG_PAGE_TOKEN or "").strip()


def expires_at():
    exp = _load().get("expires_at")
    return float(exp) if isinstance(exp, (int, float)) else None


def days_left():
    exp = expires_at()
    return None if exp is None else (exp - time.time()) / 86400


def state():
    if not token():
        return "no_token"
    d = days_left()
    if d is None:
        return "unknown"
    if d <= 0:
        return "expired"
    if d < REFRESH_BELOW_DAYS:
        return "expiring"
    return "ok"


def save(tok, expires_in=None, source="manual"):
    """Persist a token. expires_in is seconds; omit if the API didn't say."""
    tok = (tok or "").strip()
    if not tok:
        return False
    with _lock:
        d = _load()
        d.update({"token": tok, "source": source, "saved_at": time.time()})
        if expires_in:
            d["expires_at"] = time.time() + float(expires_in)
        else:
            d.pop("expires_at", None)
        _save(d)
    logger.info(f"ig_token: stored token from {source}"
                + (f", valid {_days(float(expires_in) / 86400)}" if expires_in else ""))
    return True


def validate(tok):
    """Ask Meta whether the token works. Returns (ok, description)."""
    try:
        r = requests.get(
            f"{config.IG_API_HOST}/{config.IG_API_VERSION}/me",
            params={"fields": "id,username", "access_token": tok},
            timeout=(10, 25))
    except Exception as e:
        return False, f"couldn't reach Meta: {e}"
    try:
        body = r.json()
    except Exception:
        body = {}
    if r.status_code == 200 and not body.get("error"):
        who = body.get("username") or body.get("user_id") or body.get("id") or "account"
        return True, f"@{who}" if not str(who).startswith("@") else str(who)
    err = (body.get("error") or {}).get("message") or f"HTTP {r.status_code}"
    return False, err


def refresh(force=False):
    """Renew the long-lived token. Returns (ok, message).

    Only graph.instagram.com has a refresh endpoint. A Facebook-Login Page
    token has none — it either doesn't expire (derived from a long-lived user
    token) or must be regenerated in the App Dashboard, so say that instead of
    pretending to fix it.
    """
    tok = token()
    if not tok:
        return False, "no token configured — paste one with /igtoken <token>"
    if "graph.instagram.com" not in config.IG_API_HOST:
        return False, ("this host has no refresh endpoint — Facebook Login Page "
                       "tokens don't auto-renew; get a new one from the App "
                       "Dashboard and paste it with /igtoken <token>")
    left = days_left()
    if not force and left is not None and left > REFRESH_BELOW_DAYS:
        return True, f"not due yet{life_text()}"

    try:
        r = requests.get(
            f"{config.IG_API_HOST}/refresh_access_token",
            params={"grant_type": "ig_refresh_token", "access_token": tok},
            timeout=(15, 40))
    except Exception as e:
        return False, f"couldn't reach Meta: {e}"
    try:
        body = r.json()
    except Exception:
        body = {}
    new = body.get("access_token")
    if r.status_code == 200 and new:
        save(new, body.get("expires_in"), source="auto-refresh")
        days = float(body.get("expires_in") or 0) / 86400
        return True, f"refreshed — valid {_days(days)} more"
    err = (body.get("error") or {}).get("message") or f"HTTP {r.status_code}"
    logger.warning(f"ig_token: refresh failed: {err}")
    return False, err


def _watch():
    """Refresh on a schedule so a long-running bot never lapses."""
    while True:
        try:
            st = state()
            if st in ("expiring", "expired") and "graph.instagram.com" in config.IG_API_HOST:
                ok, msg = refresh()
                if ok:
                    logger.info(f"ig_token: auto-refresh ok — {msg}")
                elif st == "expiring":
                    logger.warning(f"ig_token: auto-refresh failed — {msg}")
        except Exception:
            logger.exception("ig_token: watchdog iteration failed")
        time.sleep(CHECK_EVERY_S)


def life_text():
    """Suffix for the remaining life: ``' — 45 days left'``.

    Empty when the expiry is unknown, and empty for a token that is already
    expired — STATUS already spells that out, and appending a second
    "— …" produced "❌ expired — paste a new token — -0 days left" (the -0
    coming from rounding a negative fraction of a day).
    """
    d = days_left()
    if d is None or d < 0:
        return ""
    return f" — {_days(d)} left"


def start():
    """Start the watchdog once. No-op when no token is configured."""
    global _started
    if _started or not token():
        return False
    _started = True
    threading.Thread(target=_watch, daemon=True, name="ig-token-watch").start()
    d = days_left()
    logger.info("ig_token: watchdog started"
                + (life_text() or " (expiry unknown)"))
    return True
