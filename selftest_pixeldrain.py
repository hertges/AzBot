"""Self-check for handlers_pixeldrain rotation. No bot, no network.

Run: python3 selftest_pixeldrain.py     (needs pyrogram + requests installed)
Prints OK or raises AssertionError.
"""
import asyncio
import os
import sys
import tempfile
import types

try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

TMP = tempfile.mkdtemp()
os.environ.update(PIXELDRAIN_API_KEYS="keyA, keyB ,keyC", PIXELDRAIN_COOLDOWN_H="6",
                  API_ID="12345", API_HASH="0" * 32, BOT_TOKEN="1:test", ADMIN_ID="1")

# ── stub the bot package so the module imports without a Telegram client ────
pkg = types.ModuleType("bot_pkg")
pkg.__path__ = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_pkg")]
sys.modules["bot_pkg"] = pkg

from bot_pkg import handlers_pixeldrain as hp  # noqa: E402

hp.STATE_FILE = os.path.join(TMP, "pd_keys.json")
hp.config = types.SimpleNamespace(DATA_DIR=TMP, DOWNLOAD_DIR=TMP)


class FakeErr(hp.UploadError):
    pass


calls = []


def fake_put(path, key, progress=None):
    calls.append(key)
    if key == "keyA":
        raise hp._Err(400, {"value": "user_out_of_space", "message": "full"})
    if key == "keyB":
        raise hp._Err(401, {"value": "authentication_required", "message": "bad key"})
    return {"success": True, "id": "xyz789"}


hp._put = fake_put
f = os.path.join(TMP, "video.mp4")
open(f, "wb").write(b"x" * 1024)

# 1) rotates past a full key and a dead key, returns a pixeldrain link
res = hp.upload(f)
assert res["host"] == "pixeldrain" and res["url"] == "https://pixeldrain.com/u/xyz789", res
assert res["direct"].endswith("/api/file/xyz789?download"), res
assert calls == ["keyA", "keyB", "keyC"], calls

# 2) keyA is now in cooldown, keyB is dead → next upload must skip both
calls.clear()
hp.upload(f)
assert calls == ["keyC"], calls  # _last_good tried first, and it is keyC

# 3) cooldown expiry brings a key back, dead keys stay out
st = hp._load()
assert st[hp._fp("keyB")]["state"] == "dead", st
assert st[hp._fp("keyA")]["state"] == "full", st
st[hp._fp("keyA")]["until"] = 0
hp._save(st)
order = hp._order(hp._keys(), hp._load(), __import__("time").time())
assert order == ["keyC", "keyA"], order   # last-good first, revived key back in, dead key gone

# 3b) while keys are cooling down, an upload still probes them instead of failing
hp.STATE_FILE = os.path.join(TMP, "cooling.json")
hp._save({hp._fp("keyA"): {"state": "full", "until": 9e18},
          hp._fp("keyB"): {"state": "dead"},
          hp._fp("keyC"): {"state": "full", "until": 9e18}})
calls.clear()
hp._put = fake_put          # keyA=full, keyB=dead, keyC=ok
res = hp.upload(f)
assert res["host"] == "pixeldrain" and "keyA" in calls and "keyC" in calls, (res, calls)
hp.STATE_FILE = os.path.join(TMP, "pd_keys.json")

# 4) every key full → keyless fallback
def all_full(path, key, progress=None):
    calls.append(key)
    raise hp._Err(400, {"value": "user_out_of_space", "message": "full"})


hp._put = all_full
hp.STATE_FILE = os.path.join(TMP, "fresh.json")
hp._x0 = lambda p: {"host": "x0.at", "url": "https://x0.at/abc.mp4",
                    "direct": "https://x0.at/abc.mp4", "note": "fallback"}
res = hp.upload(f)
assert res["host"] == "x0.at", res
assert len(set(calls)) == 3, calls                 # all three keys were tried first

# 5) per-IP rate limit must NOT burn through keys — one attempt, honest error
hp.STATE_FILE = os.path.join(TMP, "fresh2.json")


def rate_limited(path, key, progress=None):
    calls.append(key)
    raise hp._Err(429, {"value": "ip_rate_limit_reached", "message": "slow down"})


calls.clear()
hp._put = rate_limited
try:
    hp.upload(f)
    raise AssertionError("should have raised")
except hp.UploadError as e:
    assert "ip_rate_limit_reached" in str(e), e
    assert len(calls) == 1, calls                  # no key rotation for an IP-level limit

# 6) no keys + fallback disabled → honest error, no crash
os.environ["PIXELDRAIN_API_KEYS"] = ""
os.environ["PIXELDRAIN_FALLBACK"] = "0"
try:
    hp.upload(f)
    raise AssertionError("should have raised")
except hp.UploadError as e:
    assert "no PIXELDRAIN_API_KEYS" in str(e), e

print("OK — rotation, cooldown, dead-key, fallback, IP-limit and no-key paths all behave")
