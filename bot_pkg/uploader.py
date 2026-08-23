import os, time, shutil

from pyrogram.errors import FloodWait
from pyrogram.enums import ParseMode

from . import config, state, log
from .utils import bar, fmtsz, fmt_time, smooth_speed, throttled_edit, split_file, cancel_kb

logger = log.get(__name__)

CancelledError = state.CancelledError

VIDEO_EXT = {".mp4", ".mkv", ".mov", ".webm", ".m4v"}
AUDIO_EXT = {".mp3", ".flac", ".m4a", ".wav", ".ogg", ".opus"}
PHOTO_EXT = {".jpg", ".jpeg", ".png"}
# .webp is deliberately NOT in PHOTO_EXT: Telegram turns small webp uploads
# into STICKERS. Send webp as an animation if it's animated, otherwise as a
# plain document — either way it stays a real file, not a sticker.
STICKERISH_EXT = {".webp", ".tgs"}

def _is_animated_webp(fpath):
    """WEBP with an ANIM chunk = animated (send as animation); otherwise
    static. Reads just the first 64 bytes."""
    try:
        with open(fpath, "rb") as f:
            head = f.read(64)
        return b"ANIM" in head
    except OSError:
        return False

def _progress_cb(client, chat_id, msg_id, label, task_id=None):
    samples = []
    last_edit = [0.0]

    def cb(current, total):
        if task_id and state.is_cancelled(task_id):
            raise CancelledError("upload cancelled by user")
        now = time.time()
        if now - last_edit[0] < 1.5 and current != total:
            return
        last_edit[0] = now
        samples.append((current, now))
        if len(samples) > 6:
            samples.pop(0)
        text = (f"{label}\n{bar(current, total)}\n"
                f"`{fmtsz(current)} / {fmtsz(total)}`  •  {smooth_speed(samples)}")
        markup = cancel_kb(task_id) if task_id else None
        throttled_edit(client, chat_id, msg_id, text, markup=markup, force=(current == total))
    return cb

def _clean_caption(fpath):
    """File names (especially from generic-extractor/gallery-dl output)
    can contain brackets, asterisks, underscores etc. that Telegram's
    default markdown parser misreads as formatting — that's what produced
    the garbled reply + phantom link-preview bubble. Captions are sent
    with parse_mode disabled below, but keep this human-readable too."""
    name = os.path.splitext(os.path.basename(fpath))[0]
    name = name.replace("_", " ").strip()
    return name[:1020] or "file"

def _send_one(client, chat_id, fpath, msg_id, caption=None, reply_to=None, task_id=None, video_meta=None):
    ext = os.path.splitext(fpath)[1].lower()
    kwargs = dict(
        chat_id=chat_id,
        caption=caption or _clean_caption(fpath),
        parse_mode=ParseMode.DISABLED,  # captions are arbitrary file names, not markdown
        progress=_progress_cb(client, chat_id, msg_id, "☁️ Uploading…", task_id=task_id),
        reply_to_message_id=reply_to,
    )
    # WZML-X passes the media's real duration/dimensions to Telegram so the
    # player shows them and streaming thumbnails work. Values come from an
    # ffprobe of the final file (see engine.probe_video_meta) — a missing
    # duration is exactly why uploads used to display as "0 seconds".
    meta = video_meta or {}
    duration = int(meta.get("duration") or 0) or None
    width = int(meta.get("width") or 0) or None
    height = int(meta.get("height") or 0) or None
    thumb = state.get_thumb(chat_id)   # custom /setthumb image if the user set one
    for attempt in range(3):
        try:
            as_doc = state.get_as_document(chat_id)
            if as_doc:
                r = client.send_document(document=fpath, **kwargs)
            elif ext in VIDEO_EXT:
                r = client.send_video(video=fpath, supports_streaming=True,
                                       duration=duration, width=width, height=height,
                                       thumb=thumb, **kwargs)
            elif ext in AUDIO_EXT:
                r = client.send_audio(audio=fpath, duration=duration, performer="AzLeechBot",
                                       thumb=thumb, **kwargs)
            elif ext in PHOTO_EXT:
                r = client.send_photo(photo=fpath, **kwargs)
            elif ext == ".webp":
                # Animated webp → animation (loops inline); static webp →
                # document. Never photo: Telegram would make it a sticker.
                if _is_animated_webp(fpath):
                    r = client.send_animation(animation=fpath, **kwargs)
                else:
                    r = client.send_document(document=fpath, **kwargs)
            else:
                r = client.send_document(document=fpath, **kwargs)
            logger.info(f"uploaded to Telegram: {fpath} ({fmtsz(os.path.getsize(fpath))}) -> chat {chat_id}")
            return r
        except CancelledError:
            logger.info(f"[{task_id}] upload cancelled by user")
            raise
        except FloodWait as e:
            logger.warning(f"FloodWait {e.value}s while sending {fpath}")
            time.sleep(e.value + 1)
        except Exception as e:
            name = type(e).__name__
            # PHOTO_INVALID_DIMENSIONS (and friends) mean Telegram refuses
            # this image as a photo — usually extreme dimensions or an odd
            # aspect ratio from web-scraped images. Retry ONCE as a plain
            # document, which always works and preserves the file exactly.
            if ext in PHOTO_EXT and attempt == 0 and (
                    "PHOTO_INVALID_DIMENSIONS" in str(e) or
                    "PHOTO_EXT" in name or "Invalid" in name or "Dimensions" in name):
                logger.warning(f"{name} for {fpath} — falling back to document")
                try:
                    r = client.send_document(document=fpath, **kwargs)
                    logger.info(f"uploaded as document after photo-dimension error: {fpath}")
                    return r
                except Exception as e2:
                    raise e2 if attempt == 2 else e
            logger.warning(f"send attempt {attempt + 1}/3 failed for {fpath}: {e}")
            if attempt == 2:
                raise
            time.sleep(2)

def upload_to_telegram(client, chat_id, fpath, msg_id, reply_to=None, task_id=None, video_meta=None):
    """Uploads fpath natively over MTProto (Pyrogram). Files above Telegram's
    ~2GB cap are split — WZML-X style: VIDEOS get ffmpeg-segmented into
    playable parts (each with correct duration/metadata), everything else is
    byte-split. Parts are uploaded and deleted one by one (low RAM/disk)."""
    size = os.path.getsize(fpath)
    if size <= config.TG_MAX_FILE_BYTES:
        return [_send_one(client, chat_id, fpath, msg_id, reply_to=reply_to, task_id=task_id,
                          video_meta=video_meta)]

    ext = os.path.splitext(fpath)[1].lower()
    if ext in VIDEO_EXT and shutil.which("ffmpeg"):
        return _split_upload_video(client, chat_id, fpath, msg_id, reply_to, task_id)

    logger.info(f"{fpath} is {fmtsz(size)}, splitting for upload")
    throttled_edit(client, chat_id, msg_id,
                    f"✂️ File is {fmtsz(size)}, splitting into parts (Telegram's "
                    f"~2GB per-file limit applies to every client, Pyrogram included)…",
                    markup=cancel_kb(task_id) if task_id else None, force=True)
    parts = split_file(fpath, config.TG_MAX_FILE_BYTES)
    results = []
    total = len(parts)
    for i, p in enumerate(parts, 1):
        if task_id and state.is_cancelled(task_id):
            _cleanup(p)
            raise CancelledError("upload cancelled by user")
        caption = f"{_clean_caption(fpath)}.part{i:03d}/{total:03d}"
        results.append(_send_one(client, chat_id, p, msg_id, caption=caption, reply_to=reply_to,
                                 task_id=task_id))
        _cleanup(p)   # delete each part right after its upload
    try:
        os.remove(fpath)
    except OSError:
        pass
    return results


def _split_upload_video(client, chat_id, fpath, msg_id, reply_to, task_id):
    """WZML-X parity: split a >2GB VIDEO with ffmpeg stream-copy into
    equal-duration segments that remain playable files with real metadata
    (not raw byte slices). Each segment uploads then deletes immediately."""
    import subprocess as sp
    meta = engine_probe_meta(fpath)
    duration = float(meta.get("duration") or 0)
    if duration <= 0:
        # can't determine duration — fall back to byte split
        parts = split_file(fpath, config.TG_MAX_FILE_BYTES)
        total = len(parts)
        out = []
        for i, p in enumerate(parts, 1):
            caption = f"{_clean_caption(fpath)}.part{i:03d}/{total:03d}"
            out.append(_send_one(client, chat_id, p, msg_id, caption=caption,
                                 reply_to=reply_to, task_id=task_id))
            _cleanup(p)
        try:
            os.remove(fpath)
        except OSError:
            pass
        return out

    n_parts = -(-os.path.getsize(fpath) // config.TG_MAX_FILE_BYTES)   # ceil
    seg_len = duration / n_parts
    base = os.path.splitext(fpath)[0]
    ext = os.path.splitext(fpath)[1] or ".mp4"
    total = n_parts
    results = []
    throttled_edit(client, chat_id, msg_id,
                    f"✂️ Video is {fmtsz(os.path.getsize(fpath))} — segmenting into "
                    f"{total} playable parts…", force=True)
    vmeta = {"duration": int(seg_len), "width": int(meta.get("width") or 0),
             "height": int(meta.get("height") or 0)}
    for i in range(1, total + 1):
        if task_id and state.is_cancelled(task_id):
            raise CancelledError("upload cancelled by user")
        start = (i - 1) * seg_len
        part = f"{base}.part{i:03d}{ext}"
        args = ["ffmpeg", "-y", "-loglevel", "error",
                "-ss", f"{start:.3f}", "-i", fpath, "-t", f"{seg_len:.3f}",
                "-map", "0", "-c", "copy", "-movflags", "+faststart", part]
        r = sp.run(args, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not os.path.exists(part) or os.path.getsize(part) == 0:
            logger.warning(f"segment {i} failed: {r.stderr[-200:] if r.stderr else 'empty'}")
            continue
        caption = f"{_clean_caption(fpath)}.part{i:03d}/{total:03d}"
        results.append(_send_one(client, chat_id, part, msg_id, caption=caption,
                                 reply_to=reply_to, task_id=task_id, video_meta=vmeta))
        _cleanup(part)   # delete right after upload — our own rule
    try:
        os.remove(fpath)
    except OSError:
        pass
    return results


def engine_probe_meta(fpath):
    """Lazy import to avoid a circular import at module load."""
    from . import engine
    return engine.probe_video_meta(fpath)


def _cleanup(p):
    try:
        os.remove(p)
    except OSError:
        pass
