"""Self-check for handlers_uploadam poll/snapshot logic. No network.

Run: python3 selftest_uploadam.py     (needs pyrogram + requests installed)
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
os.environ.update(UA_TOKEN="ua_live_test", API_ID="12345", API_HASH="0" * 32,
                  BOT_TOKEN="1:test", ADMIN_ID="1")

pkg = types.ModuleType("bot_pkg")
pkg.__path__ = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_pkg")]
sys.modules["bot_pkg"] = pkg

from bot_pkg import handlers_uploadam as hu  # noqa: E402

# 1) snapshot diff: only the new uuid counts, even when older files are listed
before = {"a", "b"}
files = [
    {"uuid": "b", "name": "old.mp4", "size_bytes": 10, "created_at": "2026-01-01T00:00:00Z"},
    {"uuid": "a", "name": "older.mp4", "size_bytes": 10, "created_at": "2025-01-01T00:00:00Z"},
    {"uuid": "c", "name": "fresh.mp4", "size_bytes": 5, "created_at": "2026-11-16T10:00:00Z"},
]
got = hu._pick_new(before, files)
assert got and got["uuid"] == "c", got

# 2) nothing new → None (the poll keeps waiting instead of reporting a lie)
assert hu._pick_new({"a", "b", "c"}, files) is None

# 3) two files landed together → newest created_at wins
two = files + [{"uuid": "d", "name": "newest.mp4", "size_bytes": 7,
                "created_at": "2026-11-16T11:00:00Z"}]
assert hu._pick_new(before, two)["uuid"] == "d"

# 4) file object without download_page_url still yields a usable link
name, size, link = hu._describe({"uuid": "z1", "name": "x.bin", "size_bytes": 42})
assert name == "x.bin" and size == 42 and link == "https://upload.am/d/z1", (name, size, link)

# 5) error envelope → UaError with the server's code, 429 handled separately
class R:
    def __init__(self, code, body, headers=None):
        self.status_code, self._b, self.text, self.headers = code, body, str(body), headers or {}

    def json(self):
        return self._b


def fake_request(code, body, headers=None):
    def _req(*a, **kw):
        return R(code, body, headers)
    return _req


hu.requests.request = fake_request(422, {"error": "unsafe_url"})
try:
    hu.remote_fetch("http://127.0.0.1/x")
    raise AssertionError("should have raised")
except hu.UaError as e:
    assert e.code == "unsafe_url", e.code

hu.requests.request = fake_request(429, {}, {"Retry-After": "42"})
try:
    hu.remote_fetch("https://example.com/x")
    raise AssertionError("should have raised")
except hu.UaError as e:
    assert e.code == "rate_limited" and "42" in str(e), (e.code, str(e))

hu.requests.request = fake_request(200, {"status": "queued", "data": {"uuid": "q1"}})
assert hu.remote_fetch("https://example.com/x")["status"] == "queued"

# 6) folder goes into the payload only when asked
seen = {}


def capture(method, path, **kw):
    seen["path"], seen["json"] = path, kw.get("json")
    return R(200, {"status": "queued"})


hu.requests.request = capture
hu.remote_fetch("https://example.com/a.bin")
assert "folder_uuid" not in seen["json"], seen
hu.remote_fetch("https://example.com/a.bin", "folder-1")
assert seen["json"]["folder_uuid"] == "folder-1", seen

print("OK — snapshot diff, tie-break, link fallback, error codes, folder payload")
