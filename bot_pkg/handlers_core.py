import os, re, shutil, time
from urllib.parse import urlparse

from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton, Message

from . import config, state, engine, uploader, drive, log
from .core import app
from .utils import (new_task_id, throttled_edit, fmtsz, fix_unknown_ext, cancel_kb, bar,
                    smooth_speed, autoclean_if_enabled, guarded, safe_edit, esc)
from .dispatcher import LiveDispatcher

logger = log.get(__name__)

URL_RE = config.URL_RE


def _auth_gate(_, __, message: Message):
    return bool(message.from_user) and state.is_authorized(message.from_user.id)

Authorized = filters.create(_auth_gate)


def _admin_gate(_, __, message: Message):
    return bool(message.from_user) and str(message.from_user.id) == config.ADMIN_ID

AdminOnly = filters.create(_admin_gate)


def _not_command(_, __, message: Message):
    return not (message.text or "").startswith("/")

NotCommand = filters.create(_not_command)


def _host_of(url):
    try:
        h = urlparse(url).netloc.lower()
        return h[4:] if h.startswith("www.") else h
    except Exception:
        return ""


# ── Basic commands ────────────────────────────────────────────────────

@app.on_message(filters.command("start") & filters.private)
@guarded
def cmd_start(client, message):
    render_help(message.chat.id)


@app.on_message(filters.command("help"))
@guarded
def cmd_help(client, message):
    render_help(message.chat.id)


def render_help(chat_id, msg_id=None):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("⬇️ Downloads", callback_data="help:dl"),
         InlineKeyboardButton("📷 Instagram", callback_data="help:ig")],
        [InlineKeyboardButton("🍪 Cookies", callback_data="help:cookies"),
         InlineKeyboardButton("🔧 Admin", callback_data="help:admin")],
    ])
    text = (
        "👋 <b>AzLeechBot</b>\n\n"
        "Paste a link — YouTube, Instagram, TikTok, Twitter/X, Reddit, a "
        "magnet link, hundreds of other sites — and I'll <b>leech</b> it "
        "(send it back here) automatically. No command needed for that.\n\n"
        "<code>/m &lt;link&gt;</code> — <b>mirror</b> to Drive instead\n"
        "<code>/l &lt;link&gt;</code> — <b>leech</b> to Telegram (same as pasting with no command)\n"
        "<code>/zm</code> / <code>/zl</code> — same, zipped into one file first\n\n"
        "Uploads go over Telegram's MTProto layer directly (Pyrogram), so "
        "the 50MB Bot API limit doesn't apply — files up to Telegram's real "
        "~2GB per-file cap go through natively.\n\n"
        "<code>/settings</code> — toggle auto-clean for this chat (tidies up command + "
        "status messages once a job finishes)\n\n"
        "Tap a section below for the rest of the commands."
    )
    if msg_id:
        safe_edit(app, chat_id, msg_id, text, reply_markup=kb)
    else:
        app.send_message(chat_id, text, reply_markup=kb)


HELP = {
    "dl": ("⬇️ <b>Downloads</b>\n\n"
           "Paste a link — auto-<b>leeched</b> to Telegram, no command needed.\n\n"
           "<code>/m &lt;link&gt;</code> — mirror to Drive  •  <code>/l &lt;link&gt;</code> — leech to Telegram\n"
           "<code>/zm &lt;link&gt;</code> / <code>/zl &lt;link&gt;</code> — same, zipped first\n"
           "<code>/yt &lt;link&gt;</code> — alias for <code>/l</code> (leech). <code>/y</code>/<code>/ytdl</code> — alias for <code>/m</code> "
           "(mirror) and <code>/yl</code>/<code>/ytdlleech</code> — alias for <code>/l</code> (leech), matching "
           "WZML-X's command names.\n\n"
           "All auto-detect the link type (YouTube/etc, Instagram, "
           "magnet/torrent, gallery site) same as the original bot's <code>/m</code>/<code>/l</code>.\n\n"
           "<code>/torrent &lt;magnet or .torrent url&gt;</code> — force aria2c, leeches to Telegram\n"
           "<code>/gallery &lt;url&gt;</code> — force gallery-dl, leeches to Telegram\n"
           "<code>/clone &lt;url&gt;</code> — mirror a website with wget, leeches to Telegram\n"
           "<code>/drive</code> — browse your Drive folder: navigate into folders, see file "
           "sizes, rename/delete anything, make new folders • <code>/drivesearch &lt;query&gt;</code>\n\n"
           "Magnet links are auto-detected in plain messages too, no command needed."),
    "ig": ("📷 <b>Instagram</b>\n\n"
           "<code>&lt;post/reel url&gt;</code> — just paste it, leeched like any other link\n"
           "<code>/ig &lt;profile_url&gt;</code> — multi-select picker: tap Posts/Reels/Stories/"
           "Highlights/Tagged to toggle each on/off, then ▶️ Start to archive all "
           "selected at once (Stories is on by default). Leeches to Telegram.\n"
           "<code>/m &lt;profile_url&gt;</code> — same picker, but mirrors to Drive instead\n\n"
           "Stories &amp; highlights need a logged-in cookie profile (<code>/cookie</code>)."),
    "cookies": ("🍪 <b>Cookies</b>\n\n"
                "<code>/cookie</code> — list profiles\n<code>/cookie &lt;name&gt;</code> — switch active profile\n"
                "Reply to a Netscape-format <code>cookies.txt</code> with <code>/cookie &lt;name&gt;</code> to add one.\n\n"
                "Needed for private Instagram accounts, stories/highlights, age-gated videos."),
    "admin": ("🔧 <b>Admin</b>\n\n"
              "<code>/stats</code> — disk + active jobs\n<code>/clean</code> — purge stale temp files\n"
              "<code>/cancel &lt;id&gt;</code> / 🛑 button — stop a job\n<code>/cancelall</code> — stop everything\n"
              "<code>/updateyt</code> — update yt-dlp now (also runs on startup + every 6h "
              "automatically; restarts the bot to load it, but only when idle)\n"
              "<code>/sh</code> — live persistent shell on the host (admin only): send commands one after "
              "another in a real PTY with arrow keys, history, tab completion and Ctrl+C — see /help → Admin\n"
              "<code>/allow &lt;id&gt;</code> / <code>/ban &lt;id&gt;</code> — access control"),
}


@app.on_callback_query(filters.regex(r"^help:"))
@guarded
def cb_help(client, cq):
    section = cq.data.split(":", 1)[1]
    cq.answer()
    if section == "back":
        return render_help(cq.message.chat.id, cq.message.id)
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Back", callback_data="help:back")]])
    safe_edit(app, cq.message.chat.id, cq.message.id, HELP.get(section, "?"), reply_markup=kb)


@app.on_message(filters.command("cookie") & Authorized)
@guarded
def cmd_cookie(client, message):
    parts = (message.text or message.caption or "").split(maxsplit=1)
    doc_msg = message if message.document else (message.reply_to_message if (message.reply_to_message and message.reply_to_message.document) else None)
    if doc_msg:
        name = parts[1].strip() if len(parts) > 1 else "global"
        path = state.cookie_path(name)
        client.download_media(doc_msg, file_name=path)
        state.set_pref(message.chat.id, "active_cookie", name)
        logger.info(f"chat {message.chat.id} saved cookie profile '{name}'")
        return message.reply_text(f"🍪 Saved cookie profile <code>{esc(name)}</code> and made it active.")
    if len(parts) > 1:
        name = parts[1].strip()
        if os.path.exists(state.cookie_path(name)):
            state.set_pref(message.chat.id, "active_cookie", name)
            return message.reply_text(f"🍪 Switched to cookie profile <code>{esc(name)}</code>.")
        return message.reply_text(
            f"No cookie profile named <code>{esc(name)}</code>. Reply to a cookies.txt file with "
            f"<code>/cookie {esc(name)}</code> to create it.")
    if not state.list_cookie_profiles():
        return message.reply_text(
            "🍪 No cookie profiles yet.\n\n"
            "Send (or reply to) a Netscape-format <code>cookies.txt</code> with <code>/cookie &lt;name&gt;</code> "
            "to add one — needed for private Instagram accounts, stories/highlights, and age-gated videos."
        )
    message.reply_text(_cookie_list_text(message.chat.id), reply_markup=_cookie_list_kb(message.chat.id))


def _cookie_list_text(chat_id):
    active = state.get_prefs(chat_id).get("active_cookie", "global")
    return f"🍪 <b>Cookie profiles</b> — active: <code>{esc(active)}</code>\n\nTap a name to switch, ⚙️ to rename/delete."

def _cookie_list_kb(chat_id):
    active = state.get_prefs(chat_id).get("active_cookie", "global")
    profiles = state.list_cookie_profiles()
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(("✅ " if p == active else "") + p, callback_data=f"ck:{p}"),
         InlineKeyboardButton("⚙️", callback_data=f"ckmgr:{p}")]
        for p in profiles
    ])


@app.on_callback_query(filters.regex(r"^ck:"))
@guarded
def cb_switch_cookie(client, cq):
    name = cq.data.split(":", 1)[1]
    state.set_pref(cq.message.chat.id, "active_cookie", name)
    logger.info(f"chat {cq.message.chat.id} switched to cookie profile '{name}' via button")
    cq.answer(f"Switched to {name}")
    safe_edit(cq, _cookie_list_text(cq.message.chat.id), reply_markup=_cookie_list_kb(cq.message.chat.id))


@app.on_callback_query(filters.regex(r"^ckback$"))
@guarded
def cb_cookie_back(client, cq):
    safe_edit(cq, _cookie_list_text(cq.message.chat.id), reply_markup=_cookie_list_kb(cq.message.chat.id))
    cq.answer()


@app.on_callback_query(filters.regex(r"^ckmgr:"))
@guarded
def cb_cookie_manage(client, cq):
    name = cq.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("✅ Use this profile", callback_data=f"ck:{name}")],
        [InlineKeyboardButton("✏️ Rename", callback_data=f"ckren:{name}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"ckdel:{name}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="ckback")],
    ])
    safe_edit(cq, f"🍪 Managing profile <code>{esc(name)}</code>", reply_markup=kb)
    cq.answer()


@app.on_callback_query(filters.regex(r"^ckdel:"))
@guarded
def cb_cookie_delete_confirm(client, cq):
    name = cq.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, delete", callback_data=f"ckdelok:{name}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"ckmgr:{name}"),
    ]])
    safe_edit(cq, f"🗑 Delete cookie profile <code>{esc(name)}</code>? This can't be undone.", reply_markup=kb)
    cq.answer()


@app.on_callback_query(filters.regex(r"^ckdelok:"))
@guarded
def cb_cookie_delete(client, cq):
    name = cq.data.split(":", 1)[1]
    ok = state.delete_cookie_profile(cq.message.chat.id, name)
    cq.answer("🗑 Deleted." if ok else "❌ Not found.", show_alert=not ok)
    if not state.list_cookie_profiles():
        return safe_edit(cq, "🍪 No cookie profiles left. Send a <code>cookies.txt</code> with <code>/cookie &lt;name&gt;</code> to add one.")
    safe_edit(cq, _cookie_list_text(cq.message.chat.id), reply_markup=_cookie_list_kb(cq.message.chat.id))


@app.on_callback_query(filters.regex(r"^ckren:"))
@guarded
def cb_cookie_rename_prompt(client, cq):
    name = cq.data.split(":", 1)[1]
    state.set_awaiting_input(cq.message.chat.id, "cookie_rename", name)
    safe_edit(cq, f"✏️ Reply with the new name for cookie profile <code>{esc(name)}</code> (or wait 2 minutes to cancel).")
    cq.answer()


def handle_cookie_rename(client, message, old_name):
    """Called from auto_leech when a chat has a pending cookie-rename
    waiting on the next text message."""
    new_name = (message.text or "").strip()
    if not new_name:
        return message.reply_text("⚠️ Empty name, rename cancelled.")
    ok, err = state.rename_cookie_profile(message.chat.id, old_name, new_name)
    if ok:
        message.reply_text(f"✅ Renamed <code>{esc(old_name)}</code> → <code>{esc(new_name)}</code>.")
    else:
        message.reply_text(f"❌ {esc(err)}")


# ── /settings — per-chat toggles ─────────────────────────────────────────
# Destination (mirror/leech) is chosen per-command, not stored here — see
# /m, /l. These are the settings that ARE persistent per chat.

def _settings_kb(chat_id):
    clean_on = state.get_autoclean(chat_id)
    doc_on = state.get_as_document(chat_id)
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(
            "🧹 Auto-clean chat: " + ("✅ ON" if clean_on else "⬜ OFF"),
            callback_data="toggle_autoclean")],
        [InlineKeyboardButton(
            "📄 Always send as document: " + ("✅ ON" if doc_on else "⬜ OFF"),
            callback_data="toggle_as_document")],
    ])

def _settings_text(chat_id):
    clean_on = state.get_autoclean(chat_id)
    doc_on = state.get_as_document(chat_id)
    clean_line = (
        "🧹 <b>Auto-clean</b>: on — a finished job deletes your link/command "
        "message and my status message, leaving only the delivered file "
        "(or the Drive link, if nothing went to Telegram)."
        if clean_on else
        "🧹 <b>Auto-clean</b>: off — command and status messages stay in the chat."
    )
    doc_line = (
        "📄 <b>As document</b>: on — everything uploads as a plain file "
        "instead of a video/audio/photo, skipping Telegram's re-encoding "
        "and thumbnail generation (keeps exact quality, no inline player)."
        if doc_on else
        "📄 <b>As document</b>: off — videos/audio/photos upload as their "
        "native type with previews and streaming."
    )
    return f"⚙️ <b>Settings</b> <i>(this chat only)</i>\n\n{clean_line}\n\n{doc_line}"

@app.on_message(filters.command("settings") & Authorized)
@guarded
def cmd_settings(client, message):
    message.reply_text(_settings_text(message.chat.id), reply_markup=_settings_kb(message.chat.id))

@app.on_callback_query(filters.regex(r"^toggle_autoclean$"))
@guarded
def cb_toggle_autoclean(client, cq):
    chat_id = cq.message.chat.id
    new_val = not state.get_autoclean(chat_id)
    state.set_autoclean(chat_id, new_val)
    logger.info(f"chat {chat_id} auto-clean set to {new_val}")
    cq.answer("Auto-clean " + ("enabled" if new_val else "disabled"))
    safe_edit(cq, _settings_text(chat_id), reply_markup=_settings_kb(chat_id))

@app.on_callback_query(filters.regex(r"^toggle_as_document$"))
@guarded
def cb_toggle_as_document(client, cq):
    chat_id = cq.message.chat.id
    new_val = not state.get_as_document(chat_id)
    state.set_as_document(chat_id, new_val)
    logger.info(f"chat {chat_id} as_document set to {new_val}")
    cq.answer("Send as document " + ("enabled" if new_val else "disabled"))
    safe_edit(cq, _settings_text(chat_id), reply_markup=_settings_kb(chat_id))


@app.on_message(filters.command(["allow", "ban"]) & AdminOnly)
@guarded
def cmd_access(client, message):
    if message.chat.type.name != "PRIVATE":
        return message.reply_text("⚠️ `/allow` and `/ban` only work in a private chat with me.")
    parts = message.text.split()
    if len(parts) != 2:
        return message.reply_text("Usage: <code>/allow &lt;user_id&gt;</code> or <code>/ban &lt;user_id&gt;</code>")
    if parts[0] == "/allow":
        state.allow_user(parts[1]); message.reply_text(f"✅ <code>{esc(parts[1])}</code> authorized.")
    else:
        state.ban_user(parts[1]); message.reply_text(f"🚫 <code>{esc(parts[1])}</code> banned.")


@app.on_callback_query(filters.regex(r"^cancel:"))
@guarded
def cb_cancel(client, cq):
    task_id = cq.data.split(":", 1)[1]
    ok = state.cancel_task(task_id)
    cq.answer("Stopping…" if ok else "Already finished.")


# ── Universal mirror/leech dispatcher: /m, /l, /zm, /zl ─────────────────
# Mirrors the original bot's design — one pair of commands that sniff the
# link type and route it to the right downloader, with the command itself
# (m=Drive vs l=Telegram) choosing the destination.
#
# Aliases: "yt" is ours (leech, like "l" — documented in /help already).
# "y"/"ytdl" (mirror) and "yl"/"ytdlleech" (leech) are WZML-X's actual
# command names for the same thing, added to match it — note "y" mirrors
# while "yt" leeches, which is worth remembering since they look similar.

YT_DEDICATED_CMDS = ("yt", "y", "ytdl", "ytdlleech", "yl")

@app.on_message(filters.command(["m", "l", "zm", "zl", "yt", "y", "ytdl", "ytdlleech", "yl"]) & Authorized)
@guarded
def cmd_mirror_leech(client, message):
    cmd = message.command[0]
    dest = "drive" if cmd in ("m", "zm", "y", "ytdl") else "telegram"
    zip_output = cmd in ("zm", "zl")
    if dest == "drive" and not drive.enabled():
        return message.reply_text("☁️ Drive isn't configured on this bot (missing GCP credentials in `.env`).")

    parts = message.text.split(maxsplit=1)
    url = parts[1].strip() if len(parts) > 1 and parts[1].strip() else None

    if not url:
        reply = message.reply_to_message
        if reply and reply.media:
            return queue_telegram_media_job(client, message, reply, dest, zip_output=zip_output)
        if reply and (reply.text or reply.caption):
            m = URL_RE.search(reply.text or reply.caption)
            if m:
                url = m.group(0).rstrip(").,]>\"'")
            else:
                return message.reply_text("⚠️ That reply doesn't contain a link or a file.")

    if not url:
        return message.reply_text(
            f"⚠️ Usage: <code>/{esc(cmd)} &lt;link&gt;</code>, or reply to a link/file with <code>/{esc(cmd)}</code>.")

    # The yt-dlp-dedicated commands (WZML-X's naming: y/ytdl/yl/ytdlleech,
    # plus our own yt) show a quality picker instead of blindly grabbing
    # "best" — same idea as WZML-X's yt-dlp flow, and it sidesteps
    # "Requested format not available" errors since the options offered
    # come from formats that actually exist for this specific video.
    if cmd in YT_DEDICATED_CMDS:
        return show_quality_picker(client, message, url, dest, zip_output)

    # Every other /m /l /zm /zl link also gets a picker when it looks like
    # a media page (YouTube etc.) — WZML-X's mirror command does exactly
    # this. Non-media links (direct files, magnets, galleries) skip the
    # picker and go straight to the right downloader.
    host = _host_of(url)
    if any(host == h or host.endswith("." + h) for h in config.YTDLP_AUTO_HOSTS):
        return show_quality_picker(client, message, url, dest, zip_output)

    dispatch_link(client, message, url, dest, zip_output=zip_output)


def show_quality_picker(client, message, url, dest, zip_output):
    status = message.reply_text("🔎 Checking available qualities…")
    try:
        title, options = engine.probe_qualities(url, message.chat.id)
    except Exception as e:
        logger.warning(f"quality probe failed for {url}: {e}")
        # Falls back to the normal auto-best path rather than dead-ending —
        # not every yt-dlp-supported site exposes a formats list to probe.
        client.edit_message_text(message.chat.id, status.id,
                                  "ℹ️ Couldn't list qualities for this link — downloading with auto-best instead.")
        return dispatch_link(client, message, url, dest, zip_output=zip_output)

    token = state.new_quality_session(url, dest, zip_output, message.id)
    rows, row = [], []
    for label, selector in options:
        row.append(InlineKeyboardButton(label, callback_data=f"ytq:{token}:{selector}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("❌ Cancel", callback_data=f"ytqcancel:{token}")])

    client.edit_message_text(
        message.chat.id, status.id,
        f"🎬 <b>{esc(title[:80])}</b>\n\nPick a quality:",
        reply_markup=InlineKeyboardMarkup(rows),
    )


@app.on_callback_query(filters.regex(r"^ytq:"))
@guarded
def cb_quality_pick(client, cq):
    _, token, selector = cq.data.split(":", 2)
    session = state.pop_quality_session(token)
    if not session:
        return cq.answer("This picker expired — send the link again.", show_alert=True)
    cq.answer()
    safe_edit(cq, "⚡ Queued…")
    task_id = new_task_id()
    tag = f" 📦→{config.DEST_LABEL[session['dest']]}" if session["zip_output"] else f" →{config.DEST_LABEL[session['dest']]}"
    status = cq.message.reply_text(f"🔎 Queued{tag} — <code>{esc(task_id)}</code>", reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, cq.message.chat.id, status.id, "yt-dlp", task_dir)
    state.task_queue.put((run_leech_job,
                           (client, cq.message.chat.id, status.id, session["url"], task_id,
                            session["reply_to"], session["dest"], session["zip_output"], selector)))


@app.on_callback_query(filters.regex(r"^ytqcancel:"))
@guarded
def cb_quality_cancel(client, cq):
    token = cq.data.split(":", 1)[1]
    state.pop_quality_session(token)
    safe_edit(cq, "Cancelled.")
    cq.answer()


def queue_telegram_media_job(client, message, media_msg, dest, zip_output=False):
    """Handles `/m`/`/l` (etc.) used as a reply to a file already in the
    chat — re-downloads that file via Pyrogram and re-uploads it to the
    requested destination, instead of requiring a URL."""
    if not state.ensure_free(config.MIN_FREE_MB):
        return message.reply_text("❌ Disk full, try again later.")
    task_id = new_task_id()
    tag = f"📦→{config.DEST_LABEL[dest]}" if zip_output else f"→{config.DEST_LABEL[dest]}"
    status = message.reply_text(f"🔎 Queued {tag} — <code>{esc(task_id)}</code>", reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "tg-media", task_dir)
    state.task_queue.put((run_telegram_media_job,
                           (client, message.chat.id, status.id, media_msg, task_id, message.id, dest, zip_output)))


def run_telegram_media_job(client, chat_id, msg_id, media_msg, task_id, reply_to, dest, zip_output=False):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    try:
        throttled_edit(client, chat_id, msg_id, "⬇️ Fetching from Telegram…", markup=cancel_kb(task_id), force=True)
        samples, last_edit = [], [0.0]

        def progress(current, total):
            if state.is_cancelled(task_id):
                raise state.CancelledError("cancelled by user")
            now = time.time()
            if now - last_edit[0] < 1.5 and current != total:
                return
            last_edit[0] = now
            samples.append((current, now))
            if len(samples) > 6:
                samples.pop(0)
            text = (f"⬇️ Fetching from Telegram…\n{bar(current, total)}\n"
                    f"`{fmtsz(current)} / {fmtsz(total)}`  •  {smooth_speed(samples)}")
            throttled_edit(client, chat_id, msg_id, text, markup=cancel_kb(task_id))

        fpath = client.download_media(media_msg, file_name=os.path.join(task_dir, ""), progress=progress)
        if not fpath:
            return throttled_edit(client, chat_id, msg_id, "❌ Couldn't fetch that message's file.", force=True)

        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)

        fpath = fix_unknown_ext(fpath)

        if zip_output:
            throttled_edit(client, chat_id, msg_id, "📦 Zipping…", force=True)
            import zipfile
            zpath = os.path.join(task_dir, os.path.splitext(os.path.basename(fpath))[0] + ".zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                zf.write(fpath, os.path.basename(fpath))
            os.remove(fpath)
            fpath = zpath

        size = os.path.getsize(fpath)
        links = []
        file_kb = None
        if dest == "telegram":
            throttled_edit(client, chat_id, msg_id, f"☁️ Uploading… (<code>{fmtsz(size)}</code>)", markup=cancel_kb(task_id), force=True)
            uploader.upload_to_telegram(client, chat_id, fpath, msg_id, reply_to=reply_to, task_id=task_id)
        else:
            throttled_edit(client, chat_id, msg_id, "☁️ Uploading to Drive…", markup=cancel_kb(task_id), force=True)
            info = drive.upload_file_full(fpath, task_id=task_id)
            if info:
                links.append(info["link"])
                file_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✏️ Rename", callback_data=f"qren:{info['id']}"),
                    InlineKeyboardButton("🗑 Delete", callback_data=f"qdel:{info['id']}"),
                ]])

        summary = f"✅ Done — <code>{fmtsz(size)}</code> → {config.DEST_LABEL[dest]}"
        if links:
            summary += "\n" + "\n".join(links)
        throttled_edit(client, chat_id, msg_id, summary, markup=file_kb, force=True)
        autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)

    except state.CancelledError:
        throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
    except Exception as e:
        logger.exception(f"[{task_id}] telegram-media job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ Failed: <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)


GDRIVE_RE = re.compile(
    r"drive\.google\.com/(?:file/d/|drive/folders/|open\?id=|drive/u/\d+/folders/)([\w-]+)"
    r"|drive\.google\.com/uc\?(?:export=download&)?id=([\w-]+)"
)


def dispatch_link(client, message, url, dest, zip_output=False):
    """Routes url to the right downloader based on what it looks like —
    magnet/torrent, Instagram, a known gallery site, a Google Drive
    file/folder link, or the generic yt-dlp/direct-download path —
    carrying `dest` (telegram/drive) and `zip_output` through to
    whichever job actually runs."""
    host = _host_of(url)

    if url.startswith("magnet:") or url.lower().endswith(".torrent"):
        from .handlers_subproc import queue_subprocess_job
        return queue_subprocess_job(client, message, "torrent", url, dest, zip_output)

    if host in ("drive.google.com",):
        m = GDRIVE_RE.search(url)
        if m:
            file_id = m.group(1) or m.group(2)
            return queue_gdrive_job(client, message, file_id, dest, zip_output)

    if any(host == h or host.endswith("." + h) for h in config.IG_HOSTS):
        from .handlers_instagram import dispatch_instagram
        return dispatch_instagram(client, message, url, dest)

    if any(host == b or host.endswith("." + b) for b in config.BLOCKED_GALLERY_HOSTS):
        return message.reply_text(
            "🚫 That site isn't supported here — it's a dedicated adult-content "
            "gallery/leak board rather than a general art or photo platform."
        )

    if any(host == h or host.endswith("." + h) for h in config.GALLERY_HOSTS):
        from .handlers_subproc import queue_subprocess_job
        return queue_subprocess_job(client, message, "gallery", url, dest, zip_output)

    queue_leech_job(client, message, url, dest, zip_output)


def queue_gdrive_job(client, message, file_id, dest, zip_output=False):
    if not drive.enabled():
        return message.reply_text(
            "☁️ That's a Google Drive link, but Drive isn't configured on this bot "
            "(missing GCP credentials in <code>.env</code>) — I can't fetch it without API access."
        )
    if not state.ensure_free(config.MIN_FREE_MB):
        return message.reply_text("❌ Disk full, try again later.")
    task_id = new_task_id()
    tag = f" 📦→{config.DEST_LABEL[dest]}" if zip_output else f" →{config.DEST_LABEL[dest]}"
    status = message.reply_text(f"🔎 Queued{tag} — <code>{esc(task_id)}</code>", reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "gdrive", task_dir)
    state.task_queue.put((run_gdrive_job,
                           (client, message.chat.id, status.id, file_id, task_id, message.id, dest, zip_output)))


def run_gdrive_job(client, chat_id, msg_id, file_id, task_id, reply_to, dest, zip_output=False):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    os.makedirs(task_dir, exist_ok=True)
    try:
        throttled_edit(client, chat_id, msg_id, "🔎 Checking Drive link…", markup=cancel_kb(task_id), force=True)
        meta = drive.get_file_any(file_id)
        if not meta:
            return throttled_edit(
                client, chat_id, msg_id,
                "❌ Couldn't read that Drive link — it may be private (not shared with "
                "this bot's Drive account) or deleted.",
                force=True,
            )

        if meta.get("mimeType") == "application/vnd.google-apps.folder":
            _download_gdrive_folder(client, chat_id, msg_id, task_id, meta, task_dir)
        else:
            _download_gdrive_single(client, chat_id, msg_id, task_id, meta, task_dir)

        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)

        entries = [os.path.join(r, f) for r, _, fs in os.walk(task_dir) for f in fs]
        if not entries:
            return throttled_edit(client, chat_id, msg_id, "❌ Nothing to upload — the file may be empty.", force=True)

        if zip_output:
            throttled_edit(client, chat_id, msg_id, "📦 Zipping…", force=True)
            import zipfile
            zpath = os.path.join(config.DOWNLOAD_DIR, f"{task_id}.zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                for fp in entries:
                    zf.write(fp, os.path.relpath(fp, task_dir))
            throttled_edit(client, chat_id, msg_id, f"☁️ Uploading… (<code>{fmtsz(os.path.getsize(zpath))}</code>)", force=True)
            if dest == "telegram":
                uploader.upload_to_telegram(client, chat_id, zpath, msg_id, reply_to=reply_to, task_id=task_id)
            else:
                drive.upload_file(zpath, task_id=task_id)
            os.remove(zpath)
            throttled_edit(client, chat_id, msg_id, f"✅ Done → {config.DEST_LABEL[dest]}", force=True)
        elif len(entries) == 1 and meta.get("mimeType") != "application/vnd.google-apps.folder":
            fpath = fix_unknown_ext(entries[0])
            size = os.path.getsize(fpath)
            done_kb = None
            link = None
            if dest == "telegram":
                throttled_edit(client, chat_id, msg_id, f"☁️ Uploading… (<code>{fmtsz(size)}</code>)", markup=cancel_kb(task_id), force=True)
                uploader.upload_to_telegram(client, chat_id, fpath, msg_id, reply_to=reply_to, task_id=task_id)
            else:
                throttled_edit(client, chat_id, msg_id, "☁️ Uploading to Drive…", markup=cancel_kb(task_id), force=True)
                info = drive.upload_file_full(fpath, task_id=task_id)
                if info:
                    link = info["link"]
                    done_kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton("✏️ Rename", callback_data=f"qren:{info['id']}"),
                        InlineKeyboardButton("🗑 Delete", callback_data=f"qdel:{info['id']}"),
                    ]])
            summary = f"✅ Done — <code>{fmtsz(size)}</code> → {config.DEST_LABEL[dest]}"
            if link:
                summary += f"\n{link}"
            throttled_edit(client, chat_id, msg_id, summary, markup=done_kb, force=True)
        else:
            throttled_edit(client, chat_id, msg_id, f"☁️ Uploading {len(entries)} file(s)…", force=True)
            disp = LiveDispatcher(client, chat_id, msg_id, task_dir, dest, reply_to=reply_to, task_id=task_id)
            for fp in entries:
                disp.submit(fix_unknown_ext(fp))
            sent, errors = disp.finalize(timeout=1800)
            summary = f"✅ Done — {sent} file(s) → {config.DEST_LABEL[dest]}"
            if errors:
                summary += f"\n⚠️ {len(errors)} failed."
            throttled_edit(client, chat_id, msg_id, summary, force=True)

        autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)

    except state.CancelledError:
        throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
    except Exception as e:
        logger.exception(f"[{task_id}] gdrive job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ Failed: <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)


def _download_gdrive_single(client, chat_id, msg_id, task_id, meta, task_dir):
    fname = meta.get("name", "file")
    fpath = os.path.join(task_dir, fname)
    throttled_edit(client, chat_id, msg_id, f"⬇️ Downloading from Drive…\n`{fname}`", markup=cancel_kb(task_id), force=True)
    drive.download_file_content(meta["id"], fpath, task_id=task_id, mime_type=meta.get("mimeType"))


def _download_gdrive_folder(client, chat_id, msg_id, task_id, folder_meta, task_dir):
    root = os.path.join(task_dir, folder_meta.get("name", "folder"))
    os.makedirs(root, exist_ok=True)

    def walk(folder_id, local_dir, depth=0):
        if state.is_cancelled(task_id) or depth > 8:
            return
        for f in drive.list_folder_any(folder_id):
            if state.is_cancelled(task_id):
                return
            if f.get("mimeType") == "application/vnd.google-apps.folder":
                sub = os.path.join(local_dir, f.get("name", "folder"))
                os.makedirs(sub, exist_ok=True)
                walk(f["id"], sub, depth + 1)
            else:
                throttled_edit(client, chat_id, msg_id, f"⬇️ Downloading from Drive…\n`{f.get('name','?')}`",
                                markup=cancel_kb(task_id))
                try:
                    drive.download_file_content(
                        f["id"], os.path.join(local_dir, f.get("name", f["id"])),
                        task_id=task_id, mime_type=f.get("mimeType"),
                    )
                except state.CancelledError:
                    raise
                except Exception as e:
                    logger.warning(f"[{task_id}] gdrive folder item failed: {f.get('name')}: {e}")

    walk(folder_meta["id"], root)



def queue_leech_job(client, message, url, dest, zip_output=False, format_selector=None):
    task_id = new_task_id()
    tag = f" 📦→{config.DEST_LABEL[dest]}" if zip_output else f" →{config.DEST_LABEL[dest]}"
    status = message.reply_text(f"🔎 Queued{tag} — <code>{esc(task_id)}</code>", reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "yt-dlp", task_dir)
    state.task_queue.put((run_leech_job,
                           (client, message.chat.id, status.id, url, task_id, message.id, dest, zip_output, format_selector)))


def run_leech_job(client, chat_id, msg_id, url, task_id, reply_to, dest, zip_output=False, format_selector=None):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    try:
        if not state.ensure_free(config.MIN_FREE_MB):
            return throttled_edit(client, chat_id, msg_id, "❌ Disk full, try again later.", force=True)

        throttled_edit(client, chat_id, msg_id, "🔎 Resolving link…", markup=cancel_kb(task_id), force=True)

        host = _host_of(url)
        known_site = any(host == h or host.endswith("." + h) for h in config.YTDLP_AUTO_HOSTS)
        use_direct = engine.is_direct_file_link(url)
        if not use_direct and not known_site:
            probed = engine.probe_is_direct_file(url)
            if probed:
                logger.info(f"[{task_id}] {host} isn't text/html — treating as a direct file, not a video page")
            use_direct = bool(probed)

        if use_direct:
            fpath = engine.download_direct(url, chat_id, msg_id, task_id, client, task_dir)
            info = None
        else:
            fpath, info = engine.download(url, chat_id, msg_id, task_id, client, task_dir, format_selector=format_selector)

        if state.is_cancelled(task_id):
            return throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)

        fpath = fix_unknown_ext(fpath)
        # WZML-X-style metadata pass: yt-dlp's merge can drop the duration/
        # resolution tags Telegram needs (a missing duration is why uploads
        # showed up as "0 seconds" in the player). Re-tag from ffprobe.
        fpath = engine.fix_media_metadata(fpath)

        if zip_output:
            throttled_edit(client, chat_id, msg_id, "📦 Zipping…", force=True)
            import zipfile
            zpath = os.path.join(task_dir, os.path.splitext(os.path.basename(fpath))[0] + ".zip")
            with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as zf:
                zf.write(fpath, os.path.basename(fpath))
            os.remove(fpath)
            fpath = zpath

        size = os.path.getsize(fpath)
        links = []
        file_kb = None

        if dest == "telegram":
            throttled_edit(client, chat_id, msg_id, f"☁️ Uploading… (<code>{fmtsz(size)}</code>)", markup=cancel_kb(task_id), force=True)
            uploader.upload_to_telegram(client, chat_id, fpath, msg_id, reply_to=reply_to, task_id=task_id,
                                        video_meta=engine.probe_video_meta(fpath))
        elif dest == "drive":
            throttled_edit(client, chat_id, msg_id, "☁️ Uploading to Drive…", markup=cancel_kb(task_id), force=True)
            dinfo = drive.upload_file_full(fpath, task_id=task_id)
            if dinfo:
                links.append(dinfo["link"])
                file_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("✏️ Rename", callback_data=f"qren:{dinfo['id']}"),
                    InlineKeyboardButton("🗑 Delete", callback_data=f"qdel:{dinfo['id']}"),
                ]])

        summary = f"✅ Done — <code>{fmtsz(size)}</code> → {config.DEST_LABEL[dest]}"
        title = (info or {}).get("title") if isinstance(info, dict) else None
        if title:
            summary = f"🎬 <b>{esc(str(title)[:80])}</b>\n{summary}"
        if links:
            summary += "\n" + "\n".join(links)
        throttled_edit(client, chat_id, msg_id, summary, markup=file_kb, force=True)
        autoclean_if_enabled(client, chat_id, msg_id, reply_to, dest)

    except engine.CancelledError:
        throttled_edit(client, chat_id, msg_id, "🛑 Cancelled.", force=True)
    except engine.DownloadError as e:
        throttled_edit(client, chat_id, msg_id, f"❌ Couldn't fetch that link:\n<code>{esc(str(e)[:300])}</code>", force=True)
    except Exception as e:
        logger.exception(f"[{task_id}] leech job failed")
        throttled_edit(client, chat_id, msg_id, f"❌ Failed: <code>{esc(str(e)[:300])}</code>", force=True)
    finally:
        state.drop_job(task_id)
        shutil.rmtree(task_dir, ignore_errors=True)


# ── Auto-leech: any message containing a link, no command required ─────
# Always leeches to Telegram — use /m if you want it mirrored to Drive.

@app.on_message(filters.text & Authorized & NotCommand & ~filters.via_bot)
@guarded
def auto_leech(client, message):
    text = message.text or ""

    pending = state.pop_awaiting_input(message.chat.id)
    if pending:
        kind, target = pending
        if kind == "cookie_rename":
            return handle_cookie_rename(client, message, target)
        if kind == "drive_rename":
            from .handlers_drive import handle_drive_rename
            return handle_drive_rename(client, message, target)
        if kind == "drive_newfolder":
            from .handlers_drive import handle_drive_new_folder
            return handle_drive_new_folder(client, message, target)

    # Live shell owns plain text while a session is open (admin only) —
    # this is what makes /sh a continuous terminal instead of one-shot runs.
    from .handlers_shell import shell_feed_or_none
    if shell_feed_or_none(client, message.chat.id,
                          message.from_user.id if message.from_user else 0, text):
        return

    magnet = config.MAGNET_RE.search(text)
    if magnet:
        logger.info(f"chat {message.chat.id} auto-leech: magnet link")
        return dispatch_link(client, message, magnet.group(0), "telegram")

    m = URL_RE.search(text)
    if not m:
        return
    url = m.group(0).rstrip(").,]>\"'")
    logger.info(f"chat {message.chat.id} auto-leech: {url}")
    dispatch_link(client, message, url, "telegram")
