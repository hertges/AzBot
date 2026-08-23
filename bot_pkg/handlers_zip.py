"""Zip leech/mirror as standalone commands.

/zipl  — reply to a file or a message with files, or pass links: everything
         is downloaded, zipped into ONE archive and leeched to this chat.
/zipm  — same, but the archive is mirrored to Drive (#folder works).
/unzipl / unzipm — reply to a .zip/.rar/.7z archive (or pass a link to
         one): it is extracted and every inner file is sent to this chat.

Both support the shared payload grammar:
    /zipl url1 url2 | myname #folder
"""
import os, re, shutil, subprocess, threading, time, zipfile

from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from . import config, state, log, uploader
from .core import app
from .utils import (new_task_id, throttled_edit, fmtsz, guarded, esc,
                    autoclean_if_enabled, zip_dir, parse_payload)
from .handlers_core import Authorized, cancel_kb, queue_leech_job

logger = log.get(__name__)

ARCHIVE_EXTS = (".zip", ".rar", ".7z", ".tar", ".tar.gz", ".tgz", ".tbz2",
                ".gz", ".bz2", ".xz")


def _is_archive(name):
    low = name.lower()
    return any(low.endswith(e) for e in ARCHIVE_EXTS)


def _extract(src, dest_dir):
    """Tries Python's zipfile first (fast, no deps), falls back to 7z/unar
    for rar/7z/tar. Returns list of extracted top-level entries."""
    os.makedirs(dest_dir, exist_ok=True)
    low = src.lower()
    if zipfile.is_zipfile(src):
        with zipfile.ZipFile(src) as zf:
            zf.extractall(dest_dir)
            return zf.namelist()
    if shutil.which("7z"):
        r = subprocess.run(["7z", "x", "-y", f"-o{dest_dir}", src],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(f"7z failed: {r.stderr.strip()[:200]}")
        return os.listdir(dest_dir)
    if shutil.which("unar"):
        r = subprocess.run(["unar", "-q", "-f", "-o", dest_dir, src],
                           capture_output=True, text=True, timeout=3600)
        if r.returncode != 0:
            raise RuntimeError(f"unar failed: {r.stderr.strip()[:200]}")
        return os.listdir(dest_dir)
    raise RuntimeError("No extractor available (install p7zip-full)")


@app.on_message(filters.command(["unzipl", "unzipm"]) & Authorized)
@guarded
def cmd_unzip(client, message):
    """Unpack archives: /unzipl → inner files to Telegram.
    /unzipm → accepted as an alias; inner files still go to this chat
    (mirroring an unpacked archive back to Drive would just duplicate it)."""
    from .utils import parse_payload
    links, rename, folder = parse_payload(message.text.split(maxsplit=1)[1]
                                          if len(message.text.split(maxsplit=1)) > 1 else "")
    reply = message.reply_to_message

    if not links:
        if reply and reply.document and _is_archive(reply.document.file_name or ""):
            return _queue_unzip_local(client, message, reply)
        return message.reply_text(
            "⚠️ Reply to an archive file (<code>.zip .rar .7z .tar…</code>) "
            "or pass link(s): <code>/unzipl &lt;link&gt;</code>")

    # Archive(s) by link → download then extract. Reuse the leech pipeline
    # per link; extraction happens in run_leech_job? No — do it here.
    for url in links:
        task_id = new_task_id()
        status = message.reply_text(f"🔎 Queued <code>/unzip</code> — <code>{esc(task_id)}</code>",
                                    reply_markup=cancel_kb(task_id))
        task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
        state.register_job(task_id, message.chat.id, status.id, "unzip", task_dir)
        state.task_queue.put((run_unzip_url_job,
                               (client, message.chat.id, status.id, url, task_id, message.id)))


def _queue_unzip_local(client, message, archive_msg):
    task_id = new_task_id()
    status = message.reply_text(f"🔎 Queued <code>/unzip</code> — <code>{esc(task_id)}</code>",
                                reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "unzip", task_dir)
    state.task_queue.put((run_unzip_local_job,
                           (client, message.chat.id, status.id, archive_msg, task_id, message.id)))


def _finish_unzip(client, chat_id, msg_id, task_id, src_path, task_dir, reply_to):
    """Extract + send every inner file."""
    try:
        throttled_edit(client, chat_id, msg_id, "📦 Extracting…", markup=cancel_kb(task_id), force=True)
        out_dir = os.path.join(task_dir, "extracted")
        names = _extract(src_path, out_dir)
        files = [os.path.join(r, f) for r, _, fs in os.walk(out_dir) for f in fs
                 if os.path.getsize(os.path.join(r, f)) > 0]
        if not files:
            return throttled_edit(client, chat_id, msg_id, "❌ Archive was empty.", force=True)

        throttled_edit(client, chat_id, msg_id,
                       f"📤 Sending {len(files)} file(s)…", force=True)
        sent = 0
        for fp in files:
            if state.is_cancelled(task_id):
                break
            try:
                uploader.upload_to_telegram(client, chat_id, fp, msg_id,
                                            reply_to=reply_to, task_id=task_id)
                sent += 1
            finally:
                try:
                    os.remove(fp)
                except OSError:
                    pass
        summary = f"✅ Unpacked — {sent}/{len(files)} file(s)"
        throttled_edit(client, chat_id, msg_id, summary, force=True)
        autoclean_if_enabled(client, chat_id, msg_id, reply_to, "telegram")
    except Exception as e:
        logger.exception("unzip job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)


def run_unzip_local_job(client, chat_id, msg_id, archive_msg, task_id, reply_to):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    try:
        throttled_edit(client, chat_id, msg_id, "⬇️ Fetching archive…", markup=cancel_kb(task_id), force=True)
        fpath = client.download_media(archive_msg, file_name=os.path.join(task_dir, ""))
        if not fpath:
            return throttled_edit(client, chat_id, msg_id, "❌ Couldn't fetch that file.", force=True)
        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
        _finish_unzip(client, chat_id, msg_id, task_id, fpath, task_dir, reply_to)
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)


def run_unzip_url_job(client, chat_id, msg_id, url, task_id, reply_to):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    try:
        throttled_edit(client, chat_id, msg_id, "⬇️ Downloading archive…", markup=cancel_kb(task_id), force=True)
        from . import engine
        fpath = engine.download_direct(url, chat_id, msg_id, task_id, client, task_dir)
        if not _is_archive(fpath) and not zipfile.is_zipfile(fpath):
            return throttled_edit(client, chat_id, msg_id,
                                   "❌ That link isn't an archive I can unpack.", force=True)
        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
        _finish_unzip(client, chat_id, msg_id, task_id, fpath, task_dir, reply_to)
    except engine.CancelledError:
        throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
    except engine.DownloadError as e:
        throttled_edit(client, chat_id, msg_id, f"❌ Couldn't download:\n<code>{esc(str(e)[:250])}</code>", force=True)
    except Exception as e:
        logger.exception("unzip-url job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)


# ── /zip — gather files (reply media, multiple replies, or links) → one zip ──

@app.on_message(filters.command(["zipl", "zipm"]) & Authorized)
@guarded
def cmd_zip(client, message):
    """Zip-leech (/zipl → Telegram) / zip-mirror (/zipm → Drive)."""
    dest = "drive" if message.command[0] == "zipm" else "telegram"
    if dest == "drive" and not drive.enabled():
        return message.reply_text("☁️ Drive isn't configured — use <code>/zipl</code> to get the archive here.")

    raw = message.text.split(maxsplit=1)[1] if len(message.text.split(maxsplit=1)) > 1 else ""
    links, rename, folder = parse_payload(raw)

    sources = []          # either ("msg", Message) or ("url", str)
    reply = message.reply_to_message
    if reply and reply.media and not (reply.text or reply.caption):
        sources.append(("msg", reply))
    elif reply:
        m = config.URL_RE.search(reply.text or reply.caption or "")
        if m:
            links.append(m.group(0).rstrip(").,]>\"'"))
    for u in links:
        sources.append(("url", u))

    if not sources:
        return message.reply_text(
            "⚠️ Reply to one or more files with <code>/zip</code>, or pass link(s):\n"
            "<code>/zip &lt;link1&gt; &lt;link2&gt; | name.zip</code>")

    if len(links) > 1 and rename:
        rename = None  # ambiguous for batch — keep auto names
    task_id = new_task_id()
    tag = "→☁️ Drive" if dest == "drive" else "→📱 Telegram"
    label = f"<code>{esc(rename)}</code>" if rename else f"— <code>{esc(task_id)}</code>"
    status = message.reply_text(f"📦 Queued <code>/{esc(message.command[0])}</code> {tag} {label}",
                                reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "zip", task_dir)
    state.task_queue.put((run_zip_job,
                           (client, message.chat.id, status.id, sources, task_id, message.id,
                            dest, rename, folder)))


def run_zip_job(client, chat_id, msg_id, sources, task_id, reply_to, dest, rename, folder):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    work = os.path.join(task_dir, "files")
    os.makedirs(work, exist_ok=True)
    try:
        got, failed = [], []
        total_src = len(sources)
        for i, (kind, obj) in enumerate(sources, 1):
            if state.is_cancelled(task_id):
                break
            throttled_edit(client, chat_id, msg_id,
                           f"⬇️ Gathering source {i}/{total_src}…", markup=cancel_kb(task_id))
            try:
                if kind == "msg":
                    fp = client.download_media(obj, file_name=os.path.join(work, ""))
                    if fp:
                        got.append(fp)
                else:
                    from . import engine
                    sub = os.path.join(work, f"src{i:02d}")
                    os.makedirs(sub, exist_ok=True)
                    if engine.is_direct_file_link(obj) or engine.probe_is_direct_file(obj):
                        fp = engine.download_direct(obj, chat_id, msg_id, task_id, client, sub)
                    else:
                        fp, _ = engine.download(obj, chat_id, msg_id, task_id, client, sub)
                    got.append(fp)
            except Exception as e:
                logger.warning(f"[{task_id}] zip source failed ({kind}): {e}")
                failed.append(str(obj)[:60])

        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
        if not got:
            return throttled_edit(client, chat_id, msg_id,
                                   "❌ Nothing could be gathered for zipping.", force=True)

        zip_name = None
        if rename:
            zip_name = rename if rename.lower().endswith(".zip") else rename + ".zip"
        else:
            stamp = time.strftime("%Y%m%d-%H%M%S")
            base = folder or "bundle"
            zip_name = f"{base}-{stamp}.zip"
        zpath = os.path.join(task_dir, zip_name)
        throttled_edit(client, chat_id, msg_id, "📦 Zipping…", force=True)
        zip_dir(work, zpath)

        size = os.path.getsize(zpath)
        throttled_edit(client, chat_id, msg_id,
                       f"☁️ Uploading… (<code>{fmtsz(size)}</code>)", force=True)
        if dest == "drive":
            info = drive.upload_file_full(zpath, task_id=task_id, rename=rename,
                                          folder=folder, chat_id=chat_id)
            summary = f"✅ Zipped <code>{len(got)}</code> file(s) → ☁️ Drive"
            kb = None
            if info:
                summary += f"\n{info['link']}"
                kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔗 Get link", callback_data=f"qlink:{info['id']}"),
                    InlineKeyboardButton("🗑 Delete", callback_data=f"qdel:{info['id']}"),
                ]])
            throttled_edit(client, chat_id, msg_id, summary, markup=kb, force=True)
        else:
            uploader.upload_to_telegram(client, chat_id, zpath, msg_id, reply_to=reply_to,
                                        task_id=task_id)
            throttled_edit(client, chat_id, msg_id,
                           f"✅ Zipped <code>{len(got)}</code> file(s) — <code>{fmtsz(size)}</code>",
                           force=True)
        if failed:
            throttled_edit(client, chat_id, msg_id,
                           f"⚠️ {len(failed)} source(s) failed:\n<code>{esc(chr(10).join(failed[:5]))}</code>")
        autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)
    except state.CancelledError:
        throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
    except Exception as e:
        logger.exception("zip job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)
