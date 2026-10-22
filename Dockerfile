FROM python:3.12-slim

# ffmpeg            — media remux/metadata pass (WZML-X-style) + thumbnails
# aria2             — multi-connection downloads (direct links + torrents)
# p7zip-full        — 7z AND rar extraction: `7z x file.rar` (p7zip's RAR
#                     plugin handles RAR/rar5 + multipart volumes; unrar-free
#                     is a dead-end package that can't open modern archives)
# wget/curl         — /clone mirrors + health checks
# gcc/python3-dev   — only needed if tgcrypto has no wheel for your arch;
#                     kept so the build never fails on fresh platforms
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg aria2 p7zip-full zip wget curl ca-certificates gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt requirements-optional.txt ./
# No `|| echo` swallowing: if a dependency fails to install, the BUILD fails
# loudly instead of crashing at runtime with ModuleNotFoundError.
# requirements-optional.txt (tgcrypto) is installed best-effort: on python
# 3.12 it has no wheel and needs a source build. If that build fails the
# image still ships — crypto_guard.py self-tests tgcrypto at every startup
# and falls back to pure-Python crypto.
RUN pip install --no-cache-dir -r requirements.txt \
    && (pip install --no-cache-dir -r requirements-optional.txt || echo "tgcrypto unavailable — pure-Python crypto") \
    && python -c "import pyrogram, yt_dlp, gallery_dl; print('deps OK')" \
    && (python -c "import tgcrypto; print('tgcrypto OK')" || echo "tgcrypto not installed — pure-Python crypto")

COPY . .

CMD ["python", "bot.py"]
