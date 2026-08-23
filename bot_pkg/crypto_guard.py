"""Startup guard against TgCrypto segfaults.

TgCrypto is a C extension; on some container hosts (musl/glibc mismatches,
buildpack Python ABI drift, low-memory nodes) it can randomly SEGFAULT the
whole bot mid-transfer (exit 139). Pyrogram cannot survive that, and the
exception system can't catch a signal — so the only defense is to test the
extension in a throwaway subprocess BEFORE Pyrogram loads it, and block its
import if it fails. Pyrogram then silently falls back to its built-in
pure-Python crypto: slower uploads/downloads, but immune to the crash.

Env switches:
  DISABLE_TGCRYPTO=1     skip the test, always run pure-Python
  TGCRYPTO_SELFTTEST=0   skip the test, always trust TgCrypto
"""
import os, sys, subprocess

_log = lambda msg: print(f"[crypto-guard] {msg}", flush=True)


def _stress_code():
    """Runs in a subprocess using THIS interpreter + environment, exercising
    pyrogram's own AES wrappers (same call path the bot uses at runtime)."""
    return (
        "import os, sys\n"
        "from pyrogram.crypto import aes\n"
        "for size in (16, 1024, 65536, 1 << 20):\n"
        "    data = os.urandom(size); key = os.urandom(32); iv = os.urandom(32)\n"
        "    ct = aes.ige_encrypt(data, key, iv)\n"
        "    assert aes.ige_decrypt(ct, key, iv) == data, 'ige mismatch'\n"
        "    c, st = aes.ctr256(data, key, os.urandom(16), bytearray(16))\n"
        "    d, _ = aes.ctr256(c, key, os.urandom(16), bytearray(16))\n"
        "print('CRYPTO_OK')\n"
    )


class _ImportBlocker:
    """Meta-path finder that makes `import tgcrypto` raise ImportError."""

    def find_spec(self, fullname, path=None, target=None):
        if fullname == "tgcrypto":
            raise ImportError(
                "tgcrypto disabled by crypto-guard (failed startup self-test)")
        return None


def install():
    """Decide whether TgCrypto may load, BEFORE pyrogram is imported."""
    if os.environ.get("DISABLE_TGCRYPTO", "").strip() in ("1", "true", "yes"):
        sys.meta_path.insert(0, _ImportBlocker())
        _log("DISABLE_TGCRYPTO set — running pure-Python crypto")
        return False

    if os.environ.get("TGCRYPTO_SELFTTEST", "1").strip() in ("0", "false", "no"):
        _log("self-test skipped — trusting TgCrypto")
        return True

    # Fast pre-check: not installed at all -> nothing to guard.
    try:
        import importlib.util
        if importlib.util.find_spec("tgcrypto") is None:
            _log("tgcrypto not installed — pure-Python crypto (fine)")
            return False
    except Exception:
        pass

    try:
        r = subprocess.run(
            [sys.executable, "-c", _stress_code()],
            capture_output=True, text=True, timeout=90,
        )
        crashed = (r.returncode != 0)          # segfault => negative (e.g. -11)
        ok = (not crashed) and "CRYPTO_OK" in r.stdout
    except subprocess.TimeoutExpired:
        crashed, ok = True, False

    if crashed or not ok:
        sys.meta_path.insert(0, _ImportBlocker())
        why = f"exit={r.returncode}" if 'r' in dir() else "timeout"
        _log(f"TgCrypto FAILED its self-test ({why}) — blocking it. "
             f"Pyrogram will use pure-Python crypto (stable, ~2-3x slower crypto).")
        return False

    _log("TgCrypto passed self-test — using native crypto")
    return True


def enabled():
    """True if tgcrypto is currently importable (post-install decision)."""
    try:
        import tgcrypto  # noqa
        return True
    except Exception:
        return False
