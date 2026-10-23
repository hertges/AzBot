"""Exercise the real _seven_preflight against actual 7z archives.

    python selfcheck_7z.py /path/to/7zz

Creates a good archive (must pass), a traversal archive (must be blocked),
and an entry-cap fixture (must be blocked), all through the shipped code path.
"""
import os
import subprocess
import sys
import tempfile

os.environ.setdefault("API_ID", "1")
os.environ.setdefault("API_HASH", "0" * 32)
os.environ.setdefault("BOT_TOKEN", "1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")

SEVEN = sys.argv[1]
WORK = tempfile.mkdtemp(prefix="pf7z-")
BIN = os.path.join(WORK, "bin")
os.makedirs(BIN)
os.symlink(SEVEN, os.path.join(BIN, "7z"))
os.environ["PATH"] = BIN + os.pathsep + os.environ["PATH"]

from bot_pkg.handlers_zip import _seven_preflight, _extract   # noqa: E402


def build(name, entries):
    """entries: [(stored_path, content)] -> archive path"""
    src = os.path.join(WORK, "src_" + name)
    for rel, data in entries:
        p = os.path.join(src, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(data)
    out = os.path.join(WORK, name + ".7z")
    files = [os.path.join(src, rel) for rel, _ in entries]
    subprocess.run([os.path.join(BIN, "7z"), "a", "-bso0", "-bsp0", out] + files,
                   cwd=src, check=True, stdout=subprocess.DEVNULL)
    return out


def check_clean_passes():
    arc = build("clean", [("a.txt", "hello"), ("sub/b.txt", "world")])
    dest = os.path.join(WORK, "out_clean")
    _seven_preflight(arc, dest)                       # must not raise
    names = _extract(arc, dest)
    assert "a.txt" in names, names


def check_traversal_blocked():
    # 7z normalises ../../ on archive creation, so the escape can only come
    # from a hostile archive's own listing. Drive the parser with a shim.
    shim = os.path.join(WORK, "shim")
    os.makedirs(shim, exist_ok=True)
    fake = os.path.join(shim, "7z")
    with open(fake, "w") as f:
        f.write("#!/bin/sh\n"
                "echo 'Listing archive: x'\n"
                "echo 'Path = /tmp/x.7z'\n"
                "echo 'Type = 7z'\n"
                "echo '----------'\n"
                "echo 'Path = ../../pwned.txt'\n"
                "echo 'Size = 4'\n")
    os.chmod(fake, 0o755)
    old = os.environ["PATH"]
    os.environ["PATH"] = shim + os.pathsep + old
    try:
        import importlib
        import bot_pkg.handlers_zip as hz
        importlib.reload(hz)
        try:
            hz._seven_preflight(os.path.join(WORK, "clean.7z"),
                                os.path.join(WORK, "out_trav"))
        except RuntimeError as e:
            assert "unsafe entry" in str(e), e
        else:
            raise AssertionError("traversal entry not blocked")
    finally:
        os.environ["PATH"] = old
        import importlib
        import bot_pkg.handlers_zip as hz
        importlib.reload(hz)


def check_entry_cap():
    arc = build("many", [(f"f{i}.txt", "x") for i in range(6)])
    try:
        _seven_preflight(arc, os.path.join(WORK, "out_many"), max_entries=3)
    except RuntimeError as e:
        assert "entries" in str(e), e
    else:
        raise AssertionError("entry cap not enforced on 7z path")


def check_garbage_reports_error():
    bad = os.path.join(WORK, "bad.7z")
    with open(bad, "wb") as f:
        f.write(b"not an archive at all")
    try:
        _seven_preflight(bad, os.path.join(WORK, "out_bad"))
    except RuntimeError as e:
        assert "7z list failed" in str(e), e
    else:
        raise AssertionError("garbage archive not reported")


if __name__ == "__main__":
    fails = 0
    for fn in (check_clean_passes, check_traversal_blocked, check_entry_cap,
               check_garbage_reports_error):
        try:
            fn()
            print("ok   " + fn.__name__)
        except Exception as e:
            fails += 1
            print(f"FAIL {fn.__name__}: {type(e).__name__}: {e}")
    print("7z preflight checks passed" if not fails else f"{fails} failed")
    sys.exit(1 if fails else 0)
