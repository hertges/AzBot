import os, re, shutil, subprocess, time, threading

from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from . import config, state, drive, log
from .core import app
from .utils import new_task_id, throttled_edit, autoclean_if_enabled, guarded, safe_edit, esc
from .dispatcher import LiveDispatcher
from .handlers_core import Authorized, cancel_kb

logger = log.get(__name__)

POST_RE = re.compile(r"instagram\.com/(p|reel|tv)/")
PROFILE_RE = re.compile(r"instagram\.com/([A-Za-z0-9._]+)/?(?:\?|$)")

ARCHIVE_URLS = {
    "posts": "https://www.instagram.com/{u}/",
    "reels": "https://www.instagram.com/{u}/reels/",
    "stories": "https://www.instagram.com/stories/{u}/",
    "highlights": "https://www.instagram.com/{u}/",  # gallery-dl pulls highlights via --highlights flag
    "tagged": "https://www.instagram.com/{u}/tagged/",
}
ARCHIVE_LABEL = {
    "posts": "🖼 Posts", "reels": "🎬 Reels", "stories": "⏳ Stories",
    "highlights": "⭐ Highlights", "tagged": "🏷 Tagged",
}


def dispatch_instagram(client, message, url, dest):
    """Entry point used both by /ig and by the universal /m /l dispatcher."""
    if POST_RE.search(url):
        # gallery-dl (not yt-dlp) here on purpose: yt-dlp only pulls video
        # and errors out with "No video formats found!" on photo-only or
        # carousel posts. gallery-dl handles photos/carousels/videos alike.
        from .handlers_subproc import queue_subprocess_job
        return queue_subprocess_job(client, message, "gallery", url, dest)

    m = PROFILE_RE.search(url)
    if not m:
        return message.reply_text("Couldn't parse that as an Instagram post or profile link.")
    username = m.group(1)
    if username.lower() in ("p", "reel", "tv", "stories", "explore"):
        return message.reply_text("Couldn't parse that as a profile link.")

    token = state.new_ig_picker_session(username, dest)
    message.reply_text(_ig_picker_text(username, dest), reply_markup=_ig_picker_kb(token))


def _ig_picker_text(username, dest):
    return (
        f"📷 <b>@{esc(username)}</b> — pick what to archive (→ {config.DEST_LABEL[dest]}):\n\n"
        f"Tap to select/deselect, then ▶️ Start. Stories are on by default. "
        f"Stories &amp; Highlights need a logged-in cookie profile (<code>/cookie</code>), "
        f"since Instagram doesn't serve those to logged-out requests."
    )


def _ig_picker_kb(token):
    s = state.get_ig_picker_session(token)
    selected = s["selected"] if s else set()
    order = ["posts", "reels", "stories", "highlights", "tagged"]

    def btn(t):
        mark = "✅ " if t in selected else "⬜ "
        return InlineKeyboardButton(mark + ARCHIVE_LABEL[t], callback_data=f"igtoggle:{token}:{t}")

    rows = [
        [btn("posts"), btn("reels")],
        [btn("stories"), btn("highlights")],
        [btn("tagged")],
        [InlineKeyboardButton("▶️ Start", callback_data=f"igstart:{token}"),
         InlineKeyboardButton("❌ Cancel", callback_data=f"igcancel:{token}")],
    ]
    return InlineKeyboardMarkup(rows)


@app.on_message(filters.command("ig") & Authorized)
@guarded
def cmd_ig(client, message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return message.reply_text(
            "⚠️ <code>/ig &lt;post_url&gt;</code> or <code>/ig &lt;profile_url&gt;</code> "
            "(leeches to Telegram — use <code>/m &lt;url&gt;</code> to mirror to Drive instead)")
    dispatch_instagram(client, message, parts[1].strip(), "telegram")


@app.on_callback_query(filters.regex(r"^igtoggle:"))
@guarded
def cb_ig_toggle(client, cq):
    _, token, archive_type = cq.data.split(":", 2)
    s = state.toggle_ig_picker_type(token, archive_type)
    if not s:
        return cq.answer("This picker expired — send the link again.", show_alert=True)
    safe_edit(cq, _ig_picker_text(s["username"], s["dest"]), reply_markup=_ig_picker_kb(token))
    cq.answer()


@app.on_callback_query(filters.regex(r"^igcancel:"))
@guarded
def cb_ig_cancel(client, cq):
    token = cq.data.split(":", 1)[1]
    state.pop_ig_picker_session(token)
    safe_edit(cq, "Cancelled.")
    cq.answer()


@app.on_callback_query(filters.regex(r"^igstart:"))
@guarded
def cb_ig_start(client, cq):
    token = cq.data.split(":", 1)[1]
    s = state.pop_ig_picker_session(token)
    if not s:
        return cq.answer("This picker expired — send the link again.", show_alert=True)
    if not s["selected"]:
        return cq.answer("Select at least one option first.", show_alert=True)

    username, dest = s["username"], s["dest"]
    types = sorted(s["selected"], key=["posts", "reels", "stories", "highlights", "tagged"].index)
    cq.answer(f"Queued {len(types)} archive(s) for @{username}")
    safe_edit(cq, f"⚡ Queued <b>{esc(', '.join(types))}</b> for @{esc(username)} → {config.DEST_LABEL[dest]}…")

    if not state.ensure_free(config.MIN_FREE_MB):
        return cq.message.reply_text("❌ Disk full.")

    chat_id = cq.message.chat.id
    for archive_type in types:
        task_id = new_task_id()
        status = cq.message.reply_text(
            f"⚡ Queued <code>{esc(archive_type)}</code> for @{esc(username)} — <code>{esc(task_id)}</code>",
            reply_markup=cancel_kb(task_id))
        task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
        state.register_job(task_id, chat_id, status.id, f"ig:{archive_type}", task_dir)
        state.task_queue.put((run_ig_archive_job,
                               (client, chat_id, status.id, archive_type, username, task_id, dest)))


def run_ig_archive_job(client, chat_id, msg_id, archive_type, username, task_id, dest):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    cookie_file = state.active_cookie_file(chat_id)
    disp = LiveDispatcher(client, chat_id, msg_id, task_dir, dest, task_id=task_id)

    # gallery-dl's own download-archive keeps a persistent per-(chat,user,type)
    # ledger of already-fetched post IDs, so re-running an archive only pulls
    # what's new since last time — this is the "tracker" from the original bot.
    #
    # If Drive is configured, that ledger lives *in Drive* (like the
    # original) — pulled down before the run, pushed back up after, and
    # never left on disk. Without Drive it falls back to a local file so
    # the tracker still works, just on the box instead of in the cloud.
    archive_name = f"igtracker_{chat_id}_{username}_{archive_type}.txt"
    use_drive_ledger = drive.enabled()
    if use_drive_ledger:
        archive_db = os.path.join(task_dir, "_archive.txt")  # scratch copy, wiped with task_dir
        throttled_edit(client, chat_id, msg_id, "📥 Fetching tracker index from Drive…", force=True)
        drive.download_named_file(archive_name, archive_db)  # no-op (leaves nothing) if it doesn't exist yet
    else:
        local_dir = os.path.join(config.DATA_DIR, "ig_archives")
        os.makedirs(local_dir, exist_ok=True)
        archive_db = os.path.join(local_dir, archive_name)

    url = ARCHIVE_URLS[archive_type].format(u=username)
    args = ["gallery-dl", "--dest", task_dir, "--download-archive", archive_db]
    if archive_type == "highlights":
        args += ["-o", "instagram.highlights=true", "-o", "instagram.stories=false"]
    if cookie_file:
        args += ["--cookies", cookie_file]
    elif archive_type in ("stories", "highlights"):
        return throttled_edit(client, chat_id, msg_id,
                               "❌ Stories/Highlights need a cookie profile — set one with `/cookie`.", force=True)
    args.append(url)

    full_log = []
    try:
        if not shutil.which("gallery-dl"):
            return throttled_edit(client, chat_id, msg_id, "❌ `gallery-dl` isn't installed on this host.", force=True)

        logger.info(f"[{task_id}] /ig {archive_type} for @{username} starting (dest={dest})")
        proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 universal_newlines=True, bufsize=1)
        state.attach_proc(task_id, proc)

        last_activity = [time.time()]
        stalled = threading.Event()

        def _watchdog():
            while proc.poll() is None:
                if state.is_cancelled(task_id):
                    return
                if time.time() - last_activity[0] > config.STALL_TIMEOUT_S:
                    stalled.set()
                    logger.warning(f"[{task_id}] /ig stalled — no output for {config.STALL_TIMEOUT_S}s, stopping")
                    try:
                        proc.terminate()
                        proc.wait(timeout=5)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    return
                time.sleep(5)

        threading.Thread(target=_watchdog, daemon=True).start()

        last_edit = 0.0
        for line in iter(proc.stdout.readline, ""):
            last_activity[0] = time.time()
            if state.is_cancelled(task_id):
                break
            full_log.append(line.rstrip())
            now = time.time()
            if now - last_edit >= 3:
                last_edit = now
                throttled_edit(client, chat_id, msg_id,
                                f"📷 @{esc(username)} — {archive_type}\n<code>{esc(line.strip()[:120])}</code>",
                                markup=cancel_kb(task_id))
        proc.wait()

        if stalled.is_set():
            disp.stop()
            mins = config.STALL_TIMEOUT_S // 60
            return throttled_edit(
                client, chat_id, msg_id,
                f"⏱️ Stopped — no progress for {mins} minutes fetching @{username} ({archive_type}). "
                f"Instagram may be rate-limiting or blocking the request.",
                force=True,
            )

        if state.is_cancelled(task_id):
            disp.stop()
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)

        if use_drive_ledger and os.path.exists(archive_db):
            throttled_edit(client, chat_id, msg_id, "📤 Saving tracker index to Drive…", force=True)
            try:
                drive.upload_named_file(archive_name, archive_db)
            except Exception as e:
                logger.warning(f"[{task_id}] Drive tracker save failed: {e}")

        throttled_edit(client, chat_id, msg_id, "☁️ Uploading results…", markup=cancel_kb(task_id), force=True)
        sent, errors = disp.finalize(timeout=1800)

        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)

        if sent == 0:
            msg = f"ℹ️ Nothing new to fetch for @{esc(username)} ({archive_type})."
            tail = "\n".join(full_log[-10:])
            if "error" in tail.lower() or "403" in tail or "login" in tail.lower():
                msg = f"❌ Couldn't fetch @{esc(username)} ({archive_type}) — may need a fresh cookie.\n<pre>{esc(tail[-1200:])}</pre>"
            return throttled_edit(client, chat_id, msg_id, msg, force=True)

        summary = f"✅ @{esc(username)} ({archive_type}) — {sent} new file(s) → {config.DEST_LABEL[dest]}"
        if errors:
            summary += f"\n⚠️ {len(errors)} failed."
        throttled_edit(client, chat_id, msg_id, summary, force=True)
        autoclean_if_enabled(client, chat_id, msg_id, None, dest)

    except Exception as e:
        logger.exception(f"[{task_id}] /ig job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)
