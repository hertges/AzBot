import os, sys, time, re, subprocess, requests

from yt_dlp import YoutubeDL

from . import config, state, log
from .utils import bar, fmtsz, fmt_time, smooth_speed, throttled_edit, cancel_kb

logger = log.get(__name__)

CancelledError = state.CancelledError

ARIA2_PROGRESS_RE = re.compile(r"\[#\w+\s+([\d.]+\w+)/([\d.]+\w+)\((\d+)%\).*?DL:([\d.]+\w+)")

# Content-Types that mean "this is a webpage, let yt-dlp parse it for an
# embedded video" rather than "this IS the file, just download it".
_PAGE_CONTENT_TYPES = ("text/html", "application/xhtml+xml")

class DownloadError(Exception):
    pass

def update_yt_dlp(restart_on_update=False):
    """yt-dlp ships near-daily patches for YouTube extraction breakage —
    exactly the class of bug behind "The page needs to be reloaded" /
    "Sign in to confirm you're not a bot". A pinned version goes stale
    fast, so this auto-updates via pip, same approach mature mirror-bots
    (WZML-X included) take. Best-effort: never raises.

    Important: re-importing an already-loaded package like yt_dlp doesn't
    reliably pick up on-disk changes mid-process (Python module reloading
    isn't safe for packages this interconnected) — the only correct way
    to actually run the new version is a clean process restart. Pass
    restart_on_update=True only when it's safe to do so (startup, or a
    periodic check confirming no jobs are running)."""
    try:
        before = __import__("yt_dlp").version.__version__
    except Exception:
        before = None
    try:
        result = subprocess.run(
            ["pip", "install", "--break-system-packages", "-q", "-U", "yt-dlp"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.warning(f"yt-dlp update failed: {result.stderr.strip()[:300]}")
            return before, before
    except Exception as e:
        logger.warning(f"yt-dlp update check failed: {e}")
        return before, before

    try:
        check = subprocess.run(
            ["python3", "-c", "import yt_dlp; print(yt_dlp.version.__version__)"],
            capture_output=True, text=True, timeout=15,
        )
        after = check.stdout.strip() or before
    except Exception:
        after = before

    if after and after != before:
        logger.info(f"yt-dlp updated {before} -> {after}")
        if restart_on_update:
            logger.info("Restarting to load the new yt-dlp version…")
            os.execv(sys.executable, [sys.executable] + sys.argv)
    else:
        logger.info(f"yt-dlp already up to date ({before})")
    return before, after

def _ydl_base_opts(chat_id, outtmpl):
    opts = {
        "quiet": True, "no_warnings": True, "noprogress": True,
        "nocheckcertificate": True, "geo_bypass": True,
        "socket_timeout": 30, "retries": 10, "fragment_retries": 10,
        "concurrent_fragment_downloads": 8,
        "outtmpl": outtmpl,
        # False, not True: "restrict" mode strips non-ASCII characters,
        # which mangles perfectly valid filenames (Persian/Arabic/etc. in
        # particular) into underscores. Keep the real name; Telegram and
        # the filesystem both handle Unicode fine.
        "restrictfilenames": False,
        "windowsfilenames": False,
        "trim_file_name": 150,
        "http_headers": {
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
        },
        # Robust, standard yt-dlp selector: best video+audio (any codec/
        # container) merged, falling back to best combined. Filtering on
        # ext=mp4/filesize<N throws away formats that don't report those
        # fields (very common for YouTube's adaptive/DASH streams) and can
        # leave zero candidates — that's what caused "Requested format is
        # not available" on otherwise-normal videos. Telegram's own size
        # cap is enforced by our own splitting logic after download.
        "format": "bv*+ba/b",
        "merge_output_format": "mp4",
        # aria2c does real multi-connection segmented downloading, which
        # is meaningfully faster than yt-dlp's own single-stream fetcher
        # on most CDNs — it's already a dependency for /torrent, so put it
        # to use here too. Scoped to plain http/https only (dict form) —
        # HLS/DASH fragment downloads stay on yt-dlp's native downloader,
        # since aria2c doesn't handle segment decryption/merging for those.
        "external_downloader": {"http": "aria2c", "https": "aria2c"},
        "external_downloader_args": {
            "aria2c": ["-x", "16", "-s", "16", "-k", "1M", "--summary-interval=0"]
        },
        # "The page needs to be reloaded" / "Sign in to confirm you're not
        # a bot" are yt-dlp's web client hitting YouTube's PO-token wall —
        # not something a cookie fixes on its own. The standard workaround
        # (what WZML-X and most current mirror-bots do) is skipping the
        # broken web client and using clients that don't need a PO token.
        # tv/android/ios are the most reliable as of yt-dlp 2026.x; web is
        # last-resort. A degraded client list is what produces the
        # "only 360p with black thumbnail" symptom.
        "extractor_args": {"youtube": {"player_client": ["tv", "android", "ios", "web"]}},
        # Black thumbnails: some clients return storyboards instead of real
        # thumbs. Prefer an embedded thumbnail when converting.
        "postprocessors": [{"key": "FFmpegMetadata", "add_metadata": True}],
    }
    ck = state.active_cookie_file(chat_id)
    if ck:
        opts["cookiefile"] = ck
    if config.YT_DLP_OPTIONS:
        opts.update(config.YT_DLP_OPTIONS)
    return opts

def probe(url, chat_id):
    """Lightweight metadata-only probe, no download."""
    opts = _ydl_base_opts(chat_id, "%(id)s.%(ext)s")
    opts["skip_download"] = True
    with YoutubeDL(opts) as ydl:
        return ydl.extract_info(url, download=False)

# ── WZML-X-style format listing ──────────────────────────────────────────
# WZML-X never guesses resolution buckets: it extracts the REAL format table
# and renders one button per usable format (exact size, container, fps),
# then asks which audio track to merge into video-only formats. This module
# mirrors that flow.

def _fmt_size(b):
    if not b:
        return ""
    b = float(b)
    for unit in ("B", "KB", "MB", "GB"):
        if b < 1024:
            return f"{b:.0f}{unit}" if unit == "B" else f"{b:.1f}{unit}"
        b /= 1024
    return f"{b:.1f}TB"


def _est_bytes(f, duration):
    """Exact size when reported; otherwise bitrate × duration estimate."""
    s = f.get("filesize") or f.get("filesize_approx")
    if s:
        return int(s)
    br = f.get("tbr") or 0
    return int(br * 1000 / 8 * duration) if br and duration else 0


def probe_qualities(url, chat_id):
    """Returns (title, options) where options is a list of dicts:
      {label, selector, needs_audio}
    built straight from the extractor's format table — WZML-X style."""
    info = probe(url, chat_id)
    if info.get("_type") == "playlist":
        entries = info.get("entries") or [{}]
        info = entries[0] or {}

    formats = [f for f in (info.get("formats") or [])
               if f.get("format_id")
               and f.get("vcodec") != "mhtml"          # storyboards
               and not (f.get("vcodec") == "none" and f.get("acodec") == "none")]
    duration = info.get("duration") or 0

    # ── video formats: one entry per (height, ext), highest bitrate wins,
    #    sorted high→low — exactly what WZML-X's YtDlpButtonMenu shows.
    vids = {}
    auds = []
    for f in formats:
        if f.get("vcodec", "none") != "none" and f.get("height"):
            key = (f["height"], f.get("ext", "?"))
            cur = vids.get(key)
            if cur is None or (_est_bytes(f, duration) or 0) > (_est_bytes(cur, duration) or 0):
                vids[key] = f
        elif f.get("acodec", "none") != "none":
            auds.append(f)

    options = [{"label": "⚡ BEST (V+A)", "selector": "bv*+ba/b", "needs_audio": False}]

    def codec_tag(c):
        c = (c or "").split(".")[0]
        return {"av01": "av1", "vp9": "vp9", "vp09": "vp9",
                "h264": "h264", "mp4a": "aac"}.get(c, c[:4])

    for (h, ext_) in sorted(vids.keys(), reverse=True):
        f = vids[(h, ext_)]
        fps = f.get("fps")
        fps_s = f"{fps}@" if fps and fps != 25 else ""
        sz = _est_bytes(f, duration)
        tag = codec_tag(f.get("vcodec"))
        note = " (no audio)" if f.get("acodec") == "none" else ""
        label = f"{h}p{fps_s} {ext_} {_fmt_size(sz)} {tag}{note}".strip()
        options.append({
            "label": label[:60],
            # bare format id: the audio-merge step composes "id+audio".
            # (Appending "+ba/b" here too would produce "id+ba/b+ba".)
            "selector": f["format_id"],
            "needs_audio": f.get("acodec") == "none",
            "vid_id": f["format_id"],
        })

    for f in sorted(auds, key=lambda x: x.get("abr") or 0, reverse=True)[:3]:
        abr = int(f.get("abr") or 0)
        sz = _est_bytes(f, duration)
        options.append({
            "label": f"🎵 {abr}K {f.get('ext','?')} {_fmt_size(sz)}".strip(),
            "selector": f["format_id"],
            "needs_audio": False,
        })

    return info.get("title", "video"), options


def download(url, chat_id, msg_id, task_id, client, dest_dir, format_selector=None):
    """Downloads url via yt-dlp into dest_dir, editing the status message
    with progress. format_selector overrides the default "best" pick —
    used by the /yt-family quality picker. Pass "audio" for an
    audio-only extraction (converted to mp3). Returns the local file path."""
    os.makedirs(dest_dir, exist_ok=True)
    outtmpl = os.path.join(dest_dir, "%(title).150B [%(id)s].%(ext)s")
    opts = _ydl_base_opts(chat_id, outtmpl)

    if format_selector == "audio":
        opts["format"] = "ba/b"
        opts["postprocessors"] = opts.get("postprocessors", []) + [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ]
    elif format_selector:
        opts["format"] = format_selector

    samples = []
    last_edit = [0.0]

    def hook(d):
        if state.is_cancelled(task_id):
            raise CancelledError("cancelled by user")
        if d["status"] == "downloading":
            now = time.time()
            if now - last_edit[0] < 1.5:
                return
            last_edit[0] = now
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            done = d.get("downloaded_bytes") or 0
            samples.append((done, now))
            if len(samples) > 6:
                samples.pop(0)
            speed = smooth_speed(samples)
            eta = d.get("eta")
            text = (
                f"⬇️ Downloading…\n{bar(done, total)}\n"
                f"`{fmtsz(done)} / {fmtsz(total)}`  •  {speed}"
                + (f"  •  ETA {fmt_time(eta)}" if eta else "")
            )
            throttled_edit(client, chat_id, msg_id, text, markup=cancel_kb(task_id))
        elif d["status"] == "finished":
            throttled_edit(client, chat_id, msg_id, "⚙️ Processing…", force=True)

    opts["progress_hooks"] = [hook]

    logger.info(f"[{task_id}] yt-dlp download starting — {url}")
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
            fpath = ydl.prepare_filename(info)
            if not os.path.exists(fpath):
                # merge_output_format / audio-extraction postprocessing may
                # have changed the extension from what prepare_filename predicts
                base, _ = os.path.splitext(fpath)
                for ext in (".mp4", ".mkv", ".webm", ".mp3", ".m4a"):
                    if os.path.exists(base + ext):
                        fpath = base + ext
                        break
            size = os.path.getsize(fpath) if os.path.exists(fpath) else 0
            logger.info(f"[{task_id}] yt-dlp download done — {fpath} ({fmtsz(size)})")
            return fpath, info
    except CancelledError:
        logger.info(f"[{task_id}] yt-dlp download cancelled")
        raise
    except Exception as e:
        logger.warning(f"[{task_id}] yt-dlp download failed — {e}")
        raise DownloadError(str(e))

def is_direct_file_link(url):
    lower = url.split("?")[0].lower()
    return lower.endswith(config.DIRECT_FILE_EXT)

def probe_is_direct_file(url):
    """For a URL whose extension doesn't already give it away: check the
    real Content-Type before deciding whether to hand it to yt-dlp's
    generic extractor (meant for parsing a *webpage* to find an embedded
    video) or download it directly as-is. Without this, a direct link to
    a PDF/zip/whatever with no recognizable extension in the URL (e.g. a
    query-string-only download endpoint) gets misrouted through the video
    extractor and ends up saved as something like "generic_video…unknown_video"
    instead of being recognized as the plain file it is.
    Returns True (download directly), False (let yt-dlp handle it), or
    None if the probe itself was inconclusive (network error, no
    Content-Type header, HEAD blocked, etc.) — caller should fall back to
    yt-dlp in that case, same as before this existed."""
    try:
        r = requests.head(url, allow_redirects=True, timeout=8)
        ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if not ctype or r.status_code >= 400:
            # Some servers don't answer HEAD properly — try a tiny ranged GET.
            r = requests.get(url, headers={"Range": "bytes=0-0"}, stream=True, timeout=8)
            ctype = r.headers.get("content-type", "").split(";")[0].strip().lower()
        if not ctype:
            return None
        return ctype not in _PAGE_CONTENT_TYPES
    except requests.RequestException:
        return None

def download_direct(url, chat_id, msg_id, task_id, client, dest_dir):
    """Direct file link — downloaded via aria2c (16 connections) instead of
    a single-stream `requests` fetch for real multi-connection speed, and
    with --content-disposition=true so the file is named exactly what the
    server calls it rather than guessed from the URL path."""
    os.makedirs(dest_dir, exist_ok=True)
    logger.info(f"[{task_id}] direct download starting (aria2c) — {url}")
    args = [
        "aria2c", "-x", "16", "-s", "16", "-k", "1M",
        "--summary-interval=1", "--content-disposition=true",
        "--file-allocation=none", "--auto-file-renaming=false",
        "-d", dest_dir, url,
    ]
    proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             universal_newlines=True, bufsize=1)
    state.attach_proc(task_id, proc)

    last_edit = 0.0
    try:
        for line in iter(proc.stdout.readline, ""):
            if state.is_cancelled(task_id):
                break
            now = time.time()
            if now - last_edit < 1.5:
                continue
            m = ARIA2_PROGRESS_RE.search(line)
            if not m:
                continue
            last_edit = now
            done, total, pct, spd = m.groups()
            text = f"⬇️ Downloading…\n{bar(float(pct), 100)}\n`{done}/{total}` • {spd}/s"
            throttled_edit(client, chat_id, msg_id, text, markup=cancel_kb(task_id))
    except (AttributeError, ValueError):
        # stdout can be None/closed when the process was just terminated
        # by /cancel — that's expected teardown, not an error.
        pass

    proc.wait()
    if state.is_cancelled(task_id):
        logger.info(f"[{task_id}] direct download cancelled")
        raise CancelledError("cancelled by user")
    if proc.returncode != 0:
        raise DownloadError(f"aria2c exited with code {proc.returncode} — the link may be dead or blocked")

    files = [f for f in os.listdir(dest_dir) if not f.endswith(".aria2")]
    if not files:
        raise DownloadError("Download reported success but no file was found")
    fpath = os.path.join(dest_dir, files[0])
    logger.info(f"[{task_id}] direct download done — {fpath} ({fmtsz(os.path.getsize(fpath))})")
    return fpath

def _ffprobe_meta(fpath):
    """Returns {duration, width, height} via ffprobe, or {} if unavailable/
    not a media file. Cheap (reads only headers)."""
    import subprocess as sp
    try:
        out = sp.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,duration:format=duration",
             "-of", "json", fpath],
            capture_output=True, text=True, timeout=20,
        ).stdout
        import json as _j
        d = _j.loads(out or "{}")
        st = ((d.get("streams") or [{}])[0])
        meta = {
            "duration": float(st.get("duration") or d.get("format", {}).get("duration") or 0) or None,
            "width": st.get("width"),
            "height": st.get("height"),
        }
        return {k: v for k, v in meta.items() if v}
    except Exception:
        return {}


def fix_media_metadata(fpath):
    """WZML-X-style metadata pass before upload. yt-dlp's ffmpeg merge can
    produce files whose container-level duration/dimensions are missing or
    wrong (Telegram then displays the video as "0 seconds"). Remuxing with
    `-c copy` rewrites the header from the actual elementary streams without
    recoding — near-instant, lossless, and fixes the tag problem at the
    source. Falls back silently to the original file if ffmpeg is missing
    or the file isn't remuxable."""
    ext = os.path.splitext(fpath)[1].lower().lstrip(".")
    if ext not in ("mp4", "mkv", "webm", "mov", "m4v", "ts", "mpg"):
        return fpath
    if not shutil_which("ffmpeg"):
        return fpath
    # Temp output keeps the ORIGINAL extension — ffmpeg picks its muxer from
    # it (mp4/matroska/mpegts…), then we atomically swap the remuxed file in.
    tmp = f"{fpath}.remux.{ext}"
    args = ["ffmpeg", "-y", "-loglevel", "error", "-i", fpath,
            "-map", "0", "-c", "copy", "-movflags", "+faststart", tmp]
    try:
        import subprocess as sp
        r = sp.run(args, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not os.path.exists(tmp) or os.path.getsize(tmp) == 0:
            _rm(tmp)
            return fpath
        # Keep the ORIGINAL extension (the container type doesn't change in
        # a stream-copy remux; only headers are rewritten).
        os.replace(tmp, fpath)
    except Exception as e:
        logger.warning(f"metadata remux failed for {fpath}: {e}")
        _rm(tmp)
    return fpath


def _rm(p):
    try:
        if p and os.path.exists(p):
            os.remove(p)
    except OSError:
        pass


def probe_video_meta(fpath):
    """Duration/dimensions for Telegram upload attributes — read straight
    off the final file so they're always right, never guessed."""
    m = _ffprobe_meta(fpath)
    return {
        "duration": int(m["duration"]) if m.get("duration") else 0,
        "width": int(m["width"]) if m.get("width") else 0,
        "height": int(m["height"]) if m.get("height") else 0,
    }


def shutil_which(name):
    import shutil
    return shutil.which(name)



def shutil_which(name):
    import shutil
    return shutil.which(name)
