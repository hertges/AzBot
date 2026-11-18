"""Self-check for Instagram DM downloads. No bot, no network, no Meta.

Run: python3 selftest_igd m.py     (needs pyrogram + requests installed)
Prints OK or raises AssertionError.
"""
import asyncio
import hashlib
import hmac
import json
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
    API_ID="12345", API_HASH="0" * 32, BOT_TOKEN="1:test", ADMIN_ID="1",
    IG_APP_SECRET="s3cret", IG_PAGE_TOKEN="tok", IG_ACCOUNT_ID="17841400000",
    IG_VERIFY_TOKEN="verify-me", IG_WEBHOOK_PORT="0",
)

pkg = types.ModuleType("bot_pkg")
pkg.__path__ = [os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot_pkg")]
sys.modules["bot_pkg"] = pkg

from bot_pkg import ig_dm, ig_webhook  # noqa: E402

ig_dm.STORE = os.path.join(TMP, "ig_dm.json")
ig_dm.config = types.SimpleNamespace(DATA_DIR=TMP, DOWNLOAD_DIR=TMP,
                                     URL_RE=__import__("re").compile(r"https?://\S+"),
                                     IG_PAGE_TOKEN="tok", IG_APP_SECRET="s3cret",
                                     IG_ACCOUNT_ID="17841400000", IG_VERIFY_TOKEN="verify-me",
                                     IG_WEBHOOK_PORT=0, IG_WEBHOOK_PATH="/ig/webhook")

# ── 1) content classification, one case per feature in the request ────────
cases = [
    ("https://www.instagram.com/p/CabcDEF123/", "post"),
    ("https://instagram.com/reel/Cxyz987/", "reel"),
    ("https://www.instagram.com/reels/Cxyz987/", "reel"),
    ("https://www.instagram.com/stories/someone/3456789012/", "story"),
    ("https://www.instagram.com/stories/highlights/17912345678901234/", "highlight"),
    ("https://www.instagram.com/live/17912345678901234/", "live"),
    ("look at this https://www.instagram.com/p/CabcDEF123/ nice", "post"),
    ("https://www.instagram.com/someuser/p/CabcDEF123/", "post"),
]
for url, want in cases:
    got, found = ig_dm.classify(url)
    assert got == want, (url, got, want)
    assert found, url

# a highlight must NOT be read as a story for user "highlights" (order bug)
assert ig_dm.classify(cases[4][0])[0] == "highlight"
# carousel = a /p/ link; same handler, Meta pages the slides
assert ig_dm.classify("https://www.instagram.com/p/Ccar0usel/")[0] == "post"
# non-Instagram and empty input
assert ig_dm.classify("https://youtube.com/watch?v=x") == (None, None)
assert ig_dm.classify("") == (None, None)

# ── 2) every promised feature is either supported or explicitly not ───────
marks = {k: m for k, m, _ in ig_dm.FEATURES}
assert marks["live"] == "❌", marks          # live is declared unsupported
for k in ("post", "carousel", "reel", "story", "highlight"):
    assert marks[k] == "✅", (k, marks)

# ── 3) webhook signature is mandatory ────────────────────────────────────
raw = json.dumps({"object": "instagram", "entry": []}).encode()
good = "sha256=" + hmac.new(b"s3cret", raw, hashlib.sha256).hexdigest()
assert ig_webhook._valid_signature(raw, good)
assert not ig_webhook._valid_signature(raw, None)
assert not ig_webhook._valid_signature(raw, "")
assert not ig_webhook._valid_signature(raw, "sha256=deadbeef")
assert not ig_webhook._valid_signature(raw + b" ", good)      # body tampered
bad_secret = "sha256=" + hmac.new(b"other", raw, hashlib.sha256).hexdigest()
assert not ig_webhook._valid_signature(raw, bad_secret)

# ── 4) linking: a code maps IGSID → chat, once, and expires ──────────────
code = ig_dm.issue_code(4242)
assert len(code) == 6 and code.isupper(), code
assert ig_dm.chat_for("IGSID1") is None
assert ig_dm.link("IGSID1", "ZZZZZZ") is None            # unknown code
assert ig_dm.chat_for("IGSID1") is None
assert ig_dm.link("IGSID1", code) == 4242
assert ig_dm.chat_for("IGSID1") == 4242
assert ig_dm.link("IGSID2", code) is None                # single use
# expiry
d = ig_dm._load()
code2 = ig_dm.issue_code(4243)
d = ig_dm._load()
d["codes"][code2]["exp"] = 0
ig_dm._save(d)
assert ig_dm.link("IGSID2", code2) is None, "expired code accepted"

# unlink drops only that chat's links
code3 = ig_dm.issue_code(999)
ig_dm.link("IGSID3", code3)
assert ig_dm.chat_for("IGSID3") == 999
assert ig_dm.unlink(999) == 1
assert ig_dm.chat_for("IGSID3") is None
assert ig_dm.chat_for("IGSID1") == 4242, "unlink touched another chat"

# ── 5) events: unsupported and unlinked senders get an honest answer ──────
sent = []
ig_dm.send_ig_message = lambda igsid, text: sent.append((igsid, text)) or True


def ev(text, igsid="IGSIDX", **msg):
    m = {"text": text}
    m.update(msg)
    return {"sender": {"id": igsid}, "message": m}


assert ig_dm.handle_event({"message": {"is_echo": True}, "sender": {"id": "x"}}) \
    == "ignored: echo/deleted/unsupported"
assert ig_dm.handle_event(ev("hi", is_deleted=True)).startswith("ignored")

sent.clear()
assert ig_dm.handle_event(ev("https://www.instagram.com/live/179123/")) == "live refused"
assert "legal" in sent[-1][1].lower() and "aren't supported" in sent[-1][1].lower(), sent[-1]

sent.clear()
assert ig_dm.handle_event(ev("https://www.instagram.com/reel/Cabc/", igsid="NOLINK")) \
    == "unlinked sender"
assert "directdownload" in sent[-1][1], sent[-1]

sent.clear()
assert ig_dm.handle_event(ev("hello there")) == "no link, unlinked sender"

# a gif/sticker-only DM never reaches us at all (Meta sends no webhook) — the
# closest we can do is an empty event, which is ignored rather than crashing
assert ig_dm.handle_event({"sender": {"id": "x"}, "message": {}}) == "no link, unlinked sender"

print("OK — classification, feature marks, signature check, linking, and "
      "live/unlinked handling all behave")
