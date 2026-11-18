"""Self-check for Instagram token storage, expiry states and refresh paths.

No network: the Graph calls are stubbed. Run: python3 selftest_igtoken.py
"""
import asyncio, os, sys, tempfile, types, time
try: asyncio.get_event_loop()
except RuntimeError: asyncio.set_event_loop(asyncio.new_event_loop())

TMP = tempfile.mkdtemp()
os.environ.update(API_ID="1", API_HASH="0"*32, BOT_TOKEN="1:t", ADMIN_ID="1",
                  IG_APP_SECRET="s", IG_PAGE_TOKEN="", IG_ACCOUNT_ID="1",
                  IG_VERIFY_TOKEN="v", IG_API_HOST="https://graph.instagram.com")
pkg = types.ModuleType("bot_pkg")
pkg.__path__ = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_pkg")]
sys.modules["bot_pkg"] = pkg
from bot_pkg import ig_token
ig_token.STORE = os.path.join(TMP, "tok.json")

# 1) nothing configured
assert ig_token.state() == "no_token"
assert ig_token.token() == ""
ok, why = ig_token.refresh()
assert not ok and "no token" in why, why

# 2) storing a token with a known lifetime
ig_token.save("T" * 40, expires_in=60 * 86400, source="test")
assert ig_token.token() == "T" * 40
assert ig_token.state() == "ok", ig_token.state()
assert 59 < ig_token.days_left() < 61, ig_token.days_left()

# 3) the stored token beats the env value (a refresh must survive restart)
ig_token.config.IG_PAGE_TOKEN = "ENVTOKEN"
assert ig_token.token() == "T" * 40, "env overrode the refreshed token"
ig_token.config.IG_PAGE_TOKEN = ""

# 4) expiry states
ig_token.save("E" * 40, expires_in=5 * 86400)
assert ig_token.state() == "expiring", ig_token.state()
ig_token.save("X" * 40, expires_in=-10)
assert ig_token.state() == "expired", ig_token.state()
# unknown lifetime (Facebook Login tokens): valid but no countdown
ig_token.save("F" * 40)
assert ig_token.state() == "unknown" and ig_token.days_left() is None

# 5) Facebook-Login host has no refresh endpoint — honest refusal
ig_token.config.IG_API_HOST = "https://graph.facebook.com"
ok, why = ig_token.refresh()
assert not ok and "no refresh endpoint" in why, why
ig_token.config.IG_API_HOST = "https://graph.instagram.com"

# 6) refresh: success stores the new token and extends the clock
class Resp:
    def __init__(self, code, body): self.status_code, self._b = code, body
    def json(self): return self._b

calls = []
def fake_get(url, params=None, timeout=None):
    calls.append((url, dict(params or {})))
    if "refresh_access_token" in url:
        assert params["grant_type"] == "ig_refresh_token"
        return Resp(200, {"access_token": "NEW" * 10, "token_type": "bearer",
                          "expires_in": 60 * 86400})
    return Resp(200, {"id": "17841400000", "username": "someuser"})

ig_token.requests = types.SimpleNamespace(get=fake_get)
ig_token.save("OLD" * 10, expires_in=2 * 86400)
assert ig_token.state() == "expiring"
ok, why = ig_token.refresh()
assert ok, why
assert ig_token.token() == "NEW" * 10, ig_token.token()
assert ig_token.state() == "ok", ig_token.state()
assert 59 < ig_token.days_left() < 61

# 7) refresh refused when not due, forced anyway
ig_token.save("MID" * 10, expires_in=40 * 86400)
ok, why = ig_token.refresh()
assert ok and "not due" in why, why
ok, why = ig_token.refresh(force=True)
assert ok and "refreshed" in why, why

# 8) failed refresh reports Meta's own message, keeps the old token
ig_token.save("KEEP" * 10, expires_in=2 * 86400)
ig_token.requests = types.SimpleNamespace(get=lambda url, params=None, timeout=None: Resp(
    400, {"error": {"message": "Session has expired"}}))
ok, why = ig_token.refresh()
assert not ok and "Session has expired" in why, why
assert ig_token.token() == "KEEP" * 10, "old token lost on failed refresh"

# 9) validate(): good and bad tokens
ig_token.requests = types.SimpleNamespace(
    get=lambda url, params=None, timeout=None: Resp(200, {"id": "1", "username": "u"}))
ok, who = ig_token.validate("GOOD")
assert ok and who == "@u", (ok, who)
ig_token.requests = types.SimpleNamespace(
    get=lambda url, params=None, timeout=None: Resp(401, {"error": {"message": "Invalid OAuth"}}))
ok, why = ig_token.validate("BAD")
assert not ok and "Invalid OAuth" in why, (ok, why)

# 10) file permissions stay private
mode = oct(os.stat(ig_token.STORE).st_mode & 0o777)
assert mode == "0o600", mode

print("OK — token precedence, expiry states, refresh due/forced/failed, "
      "Facebook-Login refusal, validation and 0600 perms all behave")
