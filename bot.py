import os
import sys
import shutil
import subprocess

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

# Low-RAM (<512MB) & zero-bytecode disk-saving flags
os.environ.setdefault("MALLOC_ARENA_MAX", "2")
os.environ.setdefault("PYTHONDONTWRITEBYTECODE", "1")

# Add ~/bin and ~/.local/bin to PATH so existing tools are always detected
home_bin = os.path.join(os.path.expanduser("~"), "bin")
local_bin = os.path.join(os.path.expanduser("~"), ".local", "bin")
for p in (local_bin, home_bin):
    if os.path.isdir(p) and p not in os.environ.get("PATH", ""):
        os.environ["PATH"] = f"{p}:{os.environ.get('PATH', '')}"

# Only run bootstrap if a required binary is genuinely missing from the server
startup_sh = os.path.join(BASE_DIR, "startup.sh")
if os.name == "posix" and os.path.exists(startup_sh) and os.environ.get("AZBOT_BOOTSTRAPPED") != "1":
    missing = [b for b in ("ffmpeg", "ffprobe", "aria2c", "7z") if not shutil.which(b)]
    if missing:
        print(f"[bot.py] Missing {missing} on server — fetching only missing binaries...", flush=True)
        os.environ["AZBOT_BOOTSTRAPPED"] = "1"
        subprocess.run(["sh", startup_sh, "--bootstrap-only"], check=False)

from bot_pkg.core import start
start()
