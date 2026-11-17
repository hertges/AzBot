"""Self-check for /px and /ua credential rotation. No bot, no network.

Run: python3 selftest_uploads.py     (needs pyrogram + requests installed)
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
os.environ.update(
    PIXELDRAIN_API_KEYS="keyA, keyB ,keyC", PIXELDRAIN_COOLDOWN_H="6",
    UA_TOKENS="tokA,tokB", UA_COOLDOWN_H="6",
    API_ID="12345", API_HASH="0" * 32, BOT_TOKEN="1:test", ADMIN_ID="1",
)

pkg = types.ModuleType("bot_pkg")
pkg.__path__ = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_pkg")]
sys.modules["bot_pkg"] = pkg

from bot_pkg import handlers_pixeldrain as hp  # noqa: E402
from bot_pkg import handlers_uploadam as hu  # noqa: E402

for mod, name in ((hp, "pixeldrain"), (hu, "uploadam")):
    mod.config = types.SimpleNamespace(DATA_DIR=TMP, DOWNLOAD_DIR=TMP)
    mod.KEYS.file = os.path.join(TMP, f"{name}_keys.json")

f = os.path.join(TMP, "video.mp4")
open(f, "wb").write(b"x" * 1024)


# ── pixeldrain ──────────────────────────────────────────────────────────────
def px_keyring():
    hp.KEYS.file = os.path.join(TMP, "px_keys.json")
    if os.path.exists(hp.KEYS.file):
        os.remove(hp.KEYS.file)
    return hp.KEYS


calls = []


def fake_put(path, key, progress=None):
    calls.append(key)
    if key == "keyA":
        raise hp._Err(400, {"value": "user_out_of_space", "message": "full"})
    if key == "keyB":
        raise hp._Err(401, {"value": "authentication_required", "message": "bad key"})
    return {"success": True, "id": "xyz789"}


hp._put = fake_put
kr = px_keyring()

# 1) rotates past a full key and a dead key, returns a pixeldrain link
res = hp.upload(f)
assert res["host"] == "pixeldrain" and res["url"] == "https://pixeldrain.com/u/xyz789", res
assert res["direct"].endswith("/api/file/xyz789?download"), res
assert calls == ["keyA", "keyB", "keyC"], calls

# 2) keyA cooling down, keyB dead → keyC (last good) is tried first and alone
calls.clear()
hp.upload(f)
assert calls == ["keyC"], calls

# 3) cooldown expiry revives a key; dead keys stay out; last-good stays first
st = kr.load()
assert st[kr.fp("keyB")]["state"] == "dead", st
assert st[kr.fp("keyA")]["state"] == "full", st
st[kr.fp("keyA")]["until"] = 0
kr._save(st)
assert kr.order() == ["keyC", "keyA"], kr.order()

# 3b) all keys cooling → still probed rather than failing outright
kr._save({kr.fp("keyA"): {"state": "full", "until": 9e18},
          kr.fp("keyB"): {"state": "dead"},
          kr.fp("keyC"): {"state": "full", "until": 9e18}})
calls.clear()
res = hp.upload(f)
assert res["host"] == "pixeldrain" and "keyA" in calls and "keyC" in calls, (res, calls)

# 4) every key full → keyless fallback, after all were tried
def all_full(path, key, progress=None):
    calls.append(key)
    raise hp._Err(400, {"value": "user_out_of_space", "message": "full"})


hp._put = all_full
kr = px_keyring()
hp._x0 = lambda p: {"host": "x0.at", "url": "https://x0.at/abc.mp4",
                    "direct": "https://x0.at/abc.mp4", "note": "fallback"}
calls.clear()
assert hp.upload(f)["host"] == "x0.at"
assert len(set(calls)) == 3, calls

# 5) per-IP rate limit must NOT burn keys — one attempt, honest error
kr = px_keyring()


def rate_limited(path, key, progress=None):
    calls.append(key)
    raise hp._Err(429, {"value": "ip_rate_limit_reached", "message": "slow down"})


hp._put = rate_limited
calls.clear()
try:
    hp.upload(f)
    raise AssertionError("should have raised")
except hp.UploadError as e:
    assert "ip_rate_limit_reached" in str(e), e
    assert len(calls) == 1, calls

# 6) no keys + fallback off → honest error, no crash
os.environ["PIXELDRAIN_API_KEYS"] = ""
os.environ["PIXELDRAIN_FALLBACK"] = "0"
try:
    hp.upload(f)
    raise AssertionError("should have raised")
except hp.UploadError as e:
    assert "no PIXELDRAIN_API_KEYS" in str(e), e

# 7) singular env var still works
os.environ["PIXELDRAIN_API_KEY"] = "solo"
assert hp.KEYS.keys() == ["solo"], hp.KEYS.keys()
os.environ.pop("PIXELDRAIN_API_KEY")


# ── upload.am ───────────────────────────────────────────────────────────────
hu.KEYS.file = os.path.join(TMP, "ua_keys.json")
if os.path.exists(hu.KEYS.file):
    os.remove(hu.KEYS.file)
ua_calls = []
FAILING = {"tokA": "quota_exceeded"}


def fake_call(method, path, tok, **kw):
    ua_calls.append(tok)
    if tok in FAILING:
        raise hu.UaError(FAILING[tok], FAILING[tok])
    return {"data": [{"uuid": "u1", "name": "a.mp4", "size_bytes": 10,
                      "download_page_url": "https://upload.am/d/u1"}]}


hu._call = fake_call

# 8) rotates past a full token, reports which account took it
body, tok = hu._call_rotating("POST", "/remote-upload", json={"url": "https://x/y.mp4"})
assert tok == "tokB", tok
assert ua_calls == ["tokA", "tokB"], ua_calls
assert hu.KEYS.load()[hu.KEYS.fp("tokA")]["state"] == "full"

# 9) an invalid token is dropped for good, not just cooled down
FAILING["tokA"] = "invalid_token"
st = hu.KEYS.load()
st[hu.KEYS.fp("tokA")]["until"] = 0          # cooldown over → it gets retried
st.pop("_last_good", None)                   # and tried first, ahead of tokB
hu.KEYS._save(st)
ua_calls.clear()
_, tok = hu._call_rotating("GET", "/files")
assert tok == "tokB", tok
assert hu.KEYS.load()[hu.KEYS.fp("tokA")]["state"] == "dead", hu.KEYS.load()

# 10) unsafe_url is not a rotation trigger — fail on the first token
FAILING.clear()
FAILING["tokB"] = "unsafe_url"
ua_calls.clear()
try:
    hu._call_rotating("POST", "/remote-upload", json={"url": "http://127.0.0.1/x"})
    raise AssertionError("should have raised")
except hu.UaError as e:
    assert e.code == "unsafe_url", e.code
    assert len(ua_calls) == 1, ua_calls

# 11) every token unusable → honest aggregate error
FAILING.update({"tokA": "quota_exceeded", "tokB": "quota_exceeded"})
hu.KEYS.file = os.path.join(TMP, "ua_keys2.json")
try:
    hu._call_rotating("GET", "/files")
    raise AssertionError("should have raised")
except hu.UaError as e:
    assert e.code == "all_tokens_exhausted", e.code

# 12) snapshot diff picks the newest genuinely new file, ignores old ones
assert hu._pick_new({"old"}, [{"uuid": "old", "created_at": "2999"},
                              {"uuid": "new", "created_at": "2020"}])["uuid"] == "new"
assert hu._pick_new({"a"}, [{"uuid": "a"}]) is None

print("OK — pixeldrain + upload.am rotation, cooldown, dead keys, fallbacks and error paths")
