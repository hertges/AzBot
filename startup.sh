#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# AzLeechBot — Ultra-Low-Storage & Low-RAM Startup Script
#   • Reuses host Python & host binaries (ffmpeg, aria2c, 7z, wget, rclone)
#     whenever already installed on the server (0 MB wasted).
#   • If a binary is missing, downloads ONLY that binary and immediately
#     deletes the archive & temp files (~150 MB saved vs old startup.sh).
#   • Never creates a venv unless one already exists from a prior run.
#   • Never downloads Python unless literally zero Python exists.
# ─────────────────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")" || exit 1

BOOTSTRAP_ONLY=0
[ "${1:-}" = "--bootstrap-only" ] && BOOTSTRAP_ONLY=1

# Low-RAM (<512MB) & zero-.pyc disk-saving flags
export MALLOC_ARENA_MAX=2
export PYTHONDONTWRITEBYTECODE=1
export PIP_NO_CACHE_DIR=1
export PIP_DISABLE_PIP_VERSION_CHECK=1

BIN="$HOME/bin"
mkdir -p "$BIN"
export PATH="$BIN:$PATH"

say() { echo "[azbot] $*"; }
die() { echo "[azbot] FATAL: $*" >&2; exit 1; }

# ── 1. Find existing system Python (NEVER reinstall if present) ─────────────
PY=""
for c in python3.11 python3.12 python3.10 python3.13 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || die "No Python found on this server! Switch your panel egg/image to Python."
say "using existing python: $($PY -V 2>&1) at $(command -v "$PY")"

fetch() {  # $1 = url, $2 = output file
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 --connect-timeout 20 -o "$2" "$1" 2>/dev/null && return 0
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1" 2>/dev/null && return 0
    fi
    "$PY" - "$1" "$2" <<'PYEOF' 2>/dev/null
import shutil, sys, urllib.request
with urllib.request.urlopen(sys.argv[1], timeout=60) as r, open(sys.argv[2], "wb") as f:
    shutil.copyfileobj(r, f)
PYEOF
    [ -s "$2" ]
}

# ── 2. Install Python packages WITHOUT a bulky venv if possible ─────────────
PYBIN="$PY"
USER_BASE="$("$PY" -m site --user-base 2>/dev/null || echo "$HOME/.local")"
export PATH="$USER_BASE/bin:$PATH"

if [ -x "$HOME/venv/bin/python" ]; then
    # Reuse existing venv if one was already created previously
    PYBIN="$HOME/venv/bin/python"
fi

if [ -f requirements.txt ] && [ "$BOOTSTRAP_ONLY" = "0" ]; then
    # Fast check: if all core modules already import, skip pip entirely!
    if "$PYBIN" -c "import pyrogram, yt_dlp, gallery_dl, dotenv, googleapiclient" 2>/dev/null; then
        say "python dependencies already satisfied — skipping pip install"
    else
        say "installing python dependencies (no-cache, low-disk mode)..."
        PIPLOG="/tmp/azbot-pip.log"
        pip_try() {
            "$PYBIN" -m pip install --no-cache-dir --user -r "$1" >"$PIPLOG" 2>&1 && return 0
            "$PYBIN" -m pip install --no-cache-dir --user --break-system-packages -r "$1" >>"$PIPLOG" 2>&1 && return 0
            "$PYBIN" -m pip install --no-cache-dir -r "$1" >>"$PIPLOG" 2>&1 && return 0
            return 1
        }
        if ! pip_try requirements.txt; then
            say "retrying without tgcrypto (no C compiler on host)..."
            grep -vi '^tgcrypto' requirements.txt > /tmp/req-notgc.txt
            pip_try /tmp/req-notgc.txt || { tail -20 "$PIPLOG"; rm -f "$PIPLOG" /tmp/req-notgc.txt; die "pip install failed"; }
            rm -f /tmp/req-notgc.txt
        fi
        rm -f "$PIPLOG"
    fi
fi

# ── 3. Only fetch MISSING binaries & delete temp archives immediately ───────
# Wipe any leftover cache from older startup.sh versions to reclaim ~150MB!
rm -rf "$HOME/.azbot-cache" "$HOME/.get-pip.py" "$HOME/.azbot-pip.log" "$HOME/.req-notgc.txt"

CACHE="/tmp/azbot-dl"

unpack() {
    "$PY" - "$1" "$2" <<'PYEOF' 2>/dev/null
import sys, tarfile, zipfile
src, dst = sys.argv[1], sys.argv[2]
if src.endswith(".zip"):
    with zipfile.ZipFile(src) as z:
        z.extractall(dst)
else:
    with tarfile.open(src) as t:
        try:
            t.extractall(dst, filter="data")
        except TypeError:
            t.extractall(dst)
PYEOF
}

bin_arch() {
    case "$(uname -m)" in
        x86_64|amd64)   echo amd64 ;;
        aarch64|arm64)  echo arm64 ;;
        *)              echo "" ;;
    esac
}

install_ffmpeg() {
    if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
        say "ffmpeg & ffprobe already on server — skipping download"
        return 0
    fi
    A=$(bin_arch); [ -n "$A" ] || return 0
    mkdir -p "$CACHE"
    F="ffmpeg-release-$A-static.tar.xz"
    say "ffmpeg missing — downloading static binary..."
    if fetch "https://johnvansickle.com/ffmpeg/releases/$F" "$CACHE/$F"; then
        unpack "$CACHE/$F" "$CACHE/ffmpeg" || true
        find "$CACHE/ffmpeg" -maxdepth 2 -type f \( -name ffmpeg -o -name ffprobe \) -exec cp {} "$BIN/" \; 2>/dev/null
        chmod +x "$BIN/ffmpeg" "$BIN/ffprobe" 2>/dev/null
        command -v strip >/dev/null 2>&1 && strip "$BIN/ffmpeg" "$BIN/ffprobe" 2>/dev/null || true
    fi
    rm -rf "$CACHE"
}

install_aria2() {
    if command -v aria2c >/dev/null 2>&1; then
        say "aria2c already on server — skipping download"
        return 0
    fi
    A=$(bin_arch); [ -n "$A" ] || return 0
    mkdir -p "$CACHE"
    F="aria2-1.36.0-static-linux-$A.tar.gz"
    say "aria2c missing — downloading static binary..."
    if fetch "https://github.com/P3TERX/Aria2-Pro-Core/releases/download/1.36.0_2021.08.22/$F" "$CACHE/$F"; then
        unpack "$CACHE/$F" "$CACHE/aria2" || true
        find "$CACHE/aria2" -type f -name aria2c -exec cp {} "$BIN/aria2c" \; 2>/dev/null
        chmod +x "$BIN/aria2c" 2>/dev/null
    fi
    rm -rf "$CACHE"
}

install_7z() {
    if command -v 7z >/dev/null 2>&1; then
        say "7z already on server — skipping download"
        return 0
    fi
    A=$(bin_arch); [ -n "$A" ] || return 0
    mkdir -p "$CACHE"
    case "$A" in
        amd64) Z="7z2501-linux-x64.tar.xz" ;;
        arm64) Z="7z2501-linux-arm64.tar.xz" ;;
    esac
    say "7z missing — downloading static binary..."
    if fetch "https://www.7-zip.org/a/$Z" "$CACHE/$Z"; then
        unpack "$CACHE/$Z" "$CACHE/7z" || true
        # Copy ONLY 7zzs (not both 7z and 7zzs, which wasted 5MB duplicating the exact same binary!)
        find "$CACHE/7z" -type f -name 7zzs -exec cp {} "$BIN/7z" \; 2>/dev/null
        chmod +x "$BIN/7z" 2>/dev/null
    fi
    rm -rf "$CACHE"
}

install_wget() {
    if command -v wget >/dev/null 2>&1 && wget --help 2>&1 | grep -q -- "--mirror"; then
        say "GNU wget already on server — skipping download"
        return 0
    fi
    A=$(bin_arch); [ -n "$A" ] || return 0
    mkdir -p "$CACHE"
    V="v20260301.1"
    say "GNU wget missing — downloading static binary..."
    if fetch "https://github.com/chromebrew/static-binaries/releases/download/$V/wget_${V}_linux_$A" "$CACHE/wget"; then
        chmod +x "$CACHE/wget" 2>/dev/null
        "$CACHE/wget" --help 2>&1 | grep -q -- "--mirror" && cp "$CACHE/wget" "$BIN/wget"
    fi
    rm -rf "$CACHE"
}

install_rclone() {
    # 1. Skip if the user didn't request Rclone
    if [ "${INSTALL_RCLONE:-0}" != "1" ]; then
        say "INSTALL_RCLONE is not set to 1 — skipping rclone to save disk space"
        return 0
    fi

    # 2. Skip download if rclone already exists on the server
    if command -v rclone >/dev/null 2>&1; then
        say "rclone already present on server — skipping download"
        return 0
    fi

    A=$(bin_arch); [ -n "$A" ] || return 0
    mkdir -p "$CACHE"
    R="rclone-current-linux-$A.zip"
    say "INSTALL_RCLONE=1 — downloading static rclone..."

    if fetch "https://downloads.rclone.org/$R" "$CACHE/$R"; then
        unpack "$CACHE/$R" "$CACHE/rclone_tmp" || true
        # Copy ONLY the rclone binary (skips docs, man pages, and test files)
        find "$CACHE/rclone_tmp" -type f -name rclone -exec cp {} "$BIN/rclone" \; 2>/dev/null
        chmod +x "$BIN/rclone" 2>/dev/null

        # Strip debug symbols to shave 15-20MB off the final binary
        command -v strip >/dev/null 2>&1 && strip "$BIN/rclone" 2>/dev/null || true
    fi

    # Clean up zip and temp folder immediately
    rm -rf "$CACHE"
    command -v rclone >/dev/null 2>&1 && say "rclone installed successfully"
}

install_ffmpeg
install_aria2
install_7z
install_wget
install_rclone

if [ "$BOOTSTRAP_ONLY" = "1" ]; then
    exit 0
fi

# ── 4. Run ──────────────────────────────────────────────────────────────────
export AZBOT_BOOTSTRAPPED=1
say "starting bot (cwd $(pwd))"
while true; do
    code=0
    "$PYBIN" -B bot.py || code=$?
    say "bot exited (code $code) — restarting in 15s"
    sleep 15
done
