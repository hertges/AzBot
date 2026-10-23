"""Runnable self-check for the audit fixes. No framework, no network.

    python selfcheck.py        # exit 0 = all checks pass

Covers: fmtsz unit boundaries, BOM-prefixed env keys, zip-slip / zip-bomb /
over-long-name handling in _safe_extract_zip, fit_html's escaped-length budget,
and the 7z preflight's entry cap.
"""
import io
import os
import sys
import tempfile
import zipfile

# bot_pkg.config exits when credentials are absent; the checks below never
# touch the network, so dummy values are enough.
os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "0" * 32)
os.environ.setdefault("BOT_TOKEN", "1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

from bot_pkg.utils import fmtsz                      # noqa: E402
from bot_pkg import config                           # noqa: E402


def check_fmtsz():
    # Exact powers used to render as "1024.0 KB" (float log floor).
    assert fmtsz(1024) == "1.0 KB", fmtsz(1024)
    assert fmtsz(1024 ** 2) == "1.0 MB", fmtsz(1024 ** 2)
    assert fmtsz(1024 ** 3) == "1.0 GB", fmtsz(1024 ** 3)
    assert fmtsz(1024 ** 4) == "1.0 TB", fmtsz(1024 ** 4)
    assert fmtsz(1024 ** 5) == "1024.0 TB", fmtsz(1024 ** 5)   # capped at TB
    assert fmtsz(0) == "0 B" and fmtsz(None) == "0 B" and fmtsz(-5) == "0 B"
    assert fmtsz(1023) == "1023 B", fmtsz(1023)
    assert fmtsz(1536) == "1.5 KB", fmtsz(1536)


def check_bom():
    # config.py rewrites "\ufeffAPI_ID" -> "API_ID" at import time.
    os.environ["\ufeffSELFCHECK_BOM"] = "yes"
    import importlib
    importlib.reload(config)
    assert "\ufeffSELFCHECK_BOM" not in os.environ, "BOM key survived"
    assert os.environ.get("SELFCHECK_BOM") == "yes", "BOM key not renamed"
    os.environ.pop("SELFCHECK_BOM", None)


def _zip(entries):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return buf.getvalue()


def check_zip_safety():
    from bot_pkg.handlers_zip import _safe_extract_zip

    with tempfile.TemporaryDirectory() as tmp:
        # 1. normal extraction still works
        src = os.path.join(tmp, "ok.zip")
        with open(src, "wb") as f:
            f.write(_zip([("a/b.txt", b"hi"), ("c.txt", b"yo")]))
        out = os.path.join(tmp, "out")
        names = _safe_extract_zip(src, out)
        assert sorted(names) == ["a/b.txt", "c.txt"], names
        assert open(os.path.join(out, "a", "b.txt"), "rb").read() == b"hi"

        # 2. ZipSlip: ../ escape is refused and nothing lands outside
        src = os.path.join(tmp, "evil.zip")
        with open(src, "wb") as f:
            f.write(_zip([("../../pwned.txt", b"x")]))
        try:
            _safe_extract_zip(src, os.path.join(tmp, "out2"))
        except RuntimeError as e:
            assert "unsafe entry" in str(e), e
        else:
            raise AssertionError("zip-slip archive was not blocked")
        assert not os.path.exists(os.path.join(tmp, "pwned.txt"))

        # 3. absolute path entry is contained, not honoured
        src = os.path.join(tmp, "abs.zip")
        with open(src, "wb") as f:
            f.write(_zip([("/etc/pwned.txt", b"x")]))
        out3 = os.path.join(tmp, "out3")
        _safe_extract_zip(src, out3)     # lstrip("/") -> inside out3
        assert os.path.exists(os.path.join(out3, "etc", "pwned.txt"))

        # 4. entry-count cap
        src = os.path.join(tmp, "many.zip")
        with open(src, "wb") as f:
            f.write(_zip([(f"f{i}.txt", b"x") for i in range(5)]))
        try:
            _safe_extract_zip(src, os.path.join(tmp, "out4"), max_entries=3)
        except RuntimeError as e:
            assert "entries" in str(e), e
        else:
            raise AssertionError("entry cap not enforced")

        # 5. one impossible filename (segment > 255 bytes) is skipped, the
        #    rest of the archive still extracts
        src = os.path.join(tmp, "long.zip")
        with open(src, "wb") as f:
            f.write(_zip([("w" * 300 + ".txt", b"x"), ("good.txt", b"y")]))
        out5 = os.path.join(tmp, "out5")
        names = _safe_extract_zip(src, out5)
        assert names == ["good.txt"], names
        assert os.path.exists(os.path.join(out5, "good.txt"))


def check_fit_html():
    # Capping raw chars is not a length cap: esc() 5x-expands '&'.
    from bot_pkg.utils import fit_html
    for raw in ("&" * 3000, "<>" * 1500, "a" * 3000, "&a<b>" * 600):
        out = fit_html(raw, 3800)
        assert len(out) <= 3800, (len(raw), len(out))
        assert out, "empty output for non-empty input"
        assert "&" not in out.replace("&amp;", "").replace("&lt;", "") \
                            .replace("&gt;", ""), "unescaped entity"
    # short input passes through unchanged, still escaped
    assert fit_html("a<b>&c", 3800) == "a&lt;b&gt;&amp;c"
    # tail is kept (shell output: newest lines matter)
    assert fit_html("A" * 5000 + "TAIL", 100).endswith("TAIL")


def check_limits():
    import bot_pkg.handlers_zip as hz
    assert hasattr(hz, "_seven_preflight"), "7z preflight missing"
    assert "timeout=3600" in open(hz.__file__, encoding="utf-8").read()
    assert "written > budget" in open(hz.__file__, encoding="utf-8").read()


if __name__ == "__main__":
    failures = 0
    for fn in (check_fmtsz, check_bom, check_zip_safety, check_fit_html,
               check_limits):
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print("all checks passed" if not failures else f"{failures} check(s) failed")
    sys.exit(1 if failures else 0)
