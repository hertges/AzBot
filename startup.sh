#!/bin/sh
# ─────────────────────────────────────────────────────────────────────────────
# AzLeechBot — startup script for Wispbyte (Pterodactyl-style panel).
#
# Panel setup: see WISPBYTE.md in this repo.
#   Startup command : sh startup.sh
#   Environment      : API_ID, API_HASH, BOT_TOKEN  (+ optional ADMIN_ID / Drive)
#
# What this script does, in order:
#   1. picks a Python interpreter (3.11 preferred — pyrogram 2.x + tgcrypto
#      wheels line up there)
#   2. creates a venv and installs requirements.txt (PEP 668 safe)
#   3. installs the binaries the bot shells out to and the image lacks:
#      ffmpeg/ffprobe, aria2c, 7z (as `7z`), GNU wget — all STATIC builds so
#      they work on the panel's Alpine image without root
#   4. checks the Telegram credentials
#   5. runs bot.py and restarts it if it dies
#
# Every step is idempotent: a restart reuses what is already in place.
# ─────────────────────────────────────────────────────────────────────────────
set -e
cd "$(dirname "$0")" || exit 1

BIN="$HOME/bin"
mkdir -p "$BIN"
export PATH="$BIN:$PATH"

say() { echo "[azbot] $*"; }
die() { echo "[azbot] FATAL: $*" >&2; exit 1; }

GETPIP_URL="https://bootstrap.pypa.io/get-pip.py"

# Download helper. curl first, wget second; if the image has NEITHER,
# bootstrap with python's urllib (always present). Must be defined before
# anything calls it.
fetch() {  # $1 = url, $2 = output file
    if command -v curl >/dev/null 2>&1; then
        curl -fsSL --retry 3 --connect-timeout 20 -o "$2" "$1" 2>/dev/null && return 0
    fi
    if command -v wget >/dev/null 2>&1; then
        wget -q -O "$2" "$1" 2>/dev/null && return 0
    fi
    "$PY" - "$1" "$2" <<'PYEOF' 2>/dev/null
import shutil, sys, urllib.request
url, dst = sys.argv[1], sys.argv[2]
with urllib.request.urlopen(url, timeout=60) as r, open(dst, "wb") as f:
    shutil.copyfileobj(r, f)
PYEOF
    [ -s "$2" ]
}

# ── 1. Python ───────────────────────────────────────────────────────────────
PY=""
for c in python3.11 python3.12 python3.10 python3.13 python3 python; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
[ -n "$PY" ] || die "no python interpreter in this image"
say "python: $($PY -V 2>&1)"

# ── 2. venv + dependencies ─────────────────────────────────────────────────
VENV="$HOME/venv"
if [ ! -x "$VENV/bin/python" ]; then
    say "creating venv at $VENV"
    "$PY" -m venv "$VENV" 2>/dev/null || true
fi
if [ -x "$VENV/bin/python" ]; then
    PYBIN="$VENV/bin/python"
    # "-m pip" survives venvs created without pip (--without-pip); a bare
    # $VENV/bin/pip does not always exist.
    PIP="$PYBIN -m pip"
else
    say "venv unavailable — falling back to the image's python"
    PYBIN="$PY"
    PIP="$PY -m pip"
fi
export PIP_ROOT_USER_ACTION=ignore
PIPLOG="$HOME/.azbot-pip.log"

# Some slim images ship python without ensurepip, so even "-m pip" is
# missing. Bootstrap it once via get-pip.py.
if ! $PIP --version >/dev/null 2>&1; then
    say "pip missing — bootstrapping with get-pip.py"
    if fetch "$GETPIP_URL" "$HOME/.get-pip.py" 2>/dev/null; then
        "$PYBIN" "$HOME/.get-pip.py" --quiet >>"$PIPLOG" 2>&1 || true
    fi
fi

pip_install() {  # $1 = requirements file
    $PIP install --no-cache-dir -r "$1" >"$PIPLOG" 2>&1 && return 0
    # PEP 668 "externally managed" images need the override flag; older pip
    # rejects the flag, but we only reach here after the first attempt
    # already failed.
    $PIP install --no-cache-dir --break-system-packages -r "$1" >>"$PIPLOG" 2>&1 && return 0
    return 1
}

if [ -f requirements.txt ]; then
    if pip_install requirements.txt; then
        say "dependencies installed"
    else
        # One retry without the native crypto accelerator: tgcrypto 1.2.5 has
        # no wheels for python >= 3.12 and needs a C compiler, which slim
        # panel images often lack. Everything else still installs normally;
        # crypto_guard.py falls back to pure-python crypto on its own.
        say "install failed (see $PIPLOG) — retrying without tgcrypto"
        grep -vi '^tgcrypto' requirements.txt > "$HOME/.req-notgc.txt"
        pip_install "$HOME/.req-notgc.txt" || { tail -20 "$PIPLOG"; die "dependency install failed"; }
    fi
fi
[ -x "$PYBIN" ] || die "no usable python after install"

# ── 3. binaries ─────────────────────────────────────────────────────────────
CACHE="$HOME/.azbot-cache"
mkdir -p "$CACHE"

unpack() {  # $1 = archive, $2 = dest dir
    "$PY" - "$1" "$2" <<'PYEOF' 2>/dev/null
import sys, tarfile
src, dst = sys.argv[1], sys.argv[2]
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
    command -v ffmpeg >/dev/null 2>&1 && { say "ffmpeg already present"; return 0; }
    A=$(bin_arch); [ -n "$A" ] || { say "no static ffmpeg build for $(uname -m)"; return 0; }
    F="ffmpeg-release-$A-static.tar.xz"
    say "downloading ffmpeg (static)"
    fetch "https://johnvansickle.com/ffmpeg/releases/$F" "$CACHE/$F" || { say "ffmpeg download failed"; return 0; }
    unpack "$CACHE/$F" "$CACHE/ffmpeg" || { say "ffmpeg extract failed"; return 0; }
    find "$CACHE/ffmpeg" -maxdepth 2 -type f \( -name ffmpeg -o -name ffprobe \) \
        -exec cp {} "$BIN/" \; 2>/dev/null
    chmod +x "$BIN/ffmpeg" "$BIN/ffprobe" 2>/dev/null
    command -v ffmpeg >/dev/null 2>&1 && say "ffmpeg installed" || say "ffmpeg unavailable (thumbnails/metadata skipped)"
}

install_aria2() {
    command -v aria2c >/dev/null 2>&1 && { say "aria2c already present"; return 0; }
    A=$(bin_arch); [ -n "$A" ] || { say "no static aria2 build for $(uname -m)"; return 0; }
    F="aria2-1.36.0-static-linux-$A.tar.gz"
    say "downloading aria2c (static)"
    fetch "https://github.com/P3TERX/Aria2-Pro-Core/releases/download/1.36.0_2021.08.22/$F" "$CACHE/$F" \
        || { say "aria2 download failed"; return 0; }
    unpack "$CACHE/$F" "$CACHE/aria2" || { say "aria2 extract failed"; return 0; }
    find "$CACHE/aria2" -type f -name aria2c -exec cp {} "$BIN/aria2c" \; 2>/dev/null
    chmod +x "$BIN/aria2c" 2>/dev/null
    command -v aria2c >/dev/null 2>&1 && say "aria2c installed" || say "aria2c unavailable (direct-link downloads fall back)"
}

install_7z() {
    command -v 7z >/dev/null 2>&1 && { say "7z already present"; return 0; }
    A=$(bin_arch); [ -n "$A" ] || { say "no static 7z build for $(uname -m)"; return 0; }
    case "$A" in
        amd64) Z="7z2501-linux-x64.tar.xz" ;;
        arm64) Z="7z2501-linux-arm64.tar.xz" ;;
    esac
    say "downloading 7z (static)"
    fetch "https://www.7-zip.org/a/$Z" "$CACHE/$Z" || { say "7z download failed"; return 0; }
    unpack "$CACHE/$Z" "$CACHE/7z" || { say "7z extract failed"; return 0; }
    # 7zzs is the fully static build (7zz is dynamically linked against glibc
    # and will not load on a musl image). 7zzs handles zip/7z/rar/rar5 — the
    # bot calls it as `7z x -y -o<dir> -bsp1 -bso0 <file>`.
    find "$CACHE/7z" -type f -name 7zzs -exec cp {} "$BIN/7z" \; 2>/dev/null
    find "$CACHE/7z" -type f -name 7zzs -exec cp {} "$BIN/7zzs" \; 2>/dev/null
    chmod +x "$BIN/7z" "$BIN/7zzs" 2>/dev/null
    command -v 7z >/dev/null 2>&1 && say "7z installed" || say "7z unavailable (rar/7z extraction disabled)"
}

install_wget() {
    # busybox wget has no --mirror/--convert-links, which the /clone command
    # needs. GNU wget static (musl build) works on Alpine without root.
    if command -v wget >/dev/null 2>&1 && wget --help 2>&1 | grep -q -- "--mirror"; then
        say "GNU wget already present"; return 0
    fi
    A=$(bin_arch); [ -n "$A" ] || return 0
    V="v20260301.1"
    say "downloading GNU wget (static musl)"
    fetch "https://github.com/chromebrew/static-binaries/releases/download/$V/wget_${V}_linux_$A" "$CACHE/wget" \
        || { say "wget download failed"; return 0; }
    chmod +x "$CACHE/wget" 2>/dev/null
    "$CACHE/wget" --help 2>&1 | grep -q -- "--mirror" || { say "static wget unusable"; return 0; }
    cp "$CACHE/wget" "$BIN/wget"
    say "GNU wget installed"
}

install_ffmpeg
install_aria2
install_7z
install_wget

# `zip` is only needed for the rare "zip -FF" index-repair path; the bot
# degrades gracefully without it (python's zipfile handles normal zips).
command -v zip >/dev/null 2>&1 && say "zip present" || say "zip missing — the zip-index repair path stays disabled"

# ── 4. credentials ──────────────────────────────────────────────────────────
missing=""
for v in API_ID API_HASH BOT_TOKEN; do
    eval "val=\${$v:-}"
    [ -n "$val" ] || missing="$missing $v"
done
[ -z "$missing" ] || die "missing env vars:$missing — set them in Startup → Server Configuration"

# ── 5. run ──────────────────────────────────────────────────────────────────
say "starting bot (cwd $(pwd))"
while true; do
    code=0
    "$PYBIN" bot.py || code=$?
    say "bot exited (code $code) — restarting in 15s"
    sleep 15
done
