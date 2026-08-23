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
         InlineKeyboardButton("🗃 Zip", callback_data="help:zip")],
        [InlineKeyboardButton("📷 Instagram", callback_data="help:ig"),
         InlineKeyboardButton("🍪 Cookies", callback_data="help:cookies")],
        [InlineKeyboardButton("🔧 Admin", callback_data="help:admin")],
    ])
    text = (
        "👋 <b>AzLeechBot</b>\n\n"
        "Paste a link — YouTube, Instagram, TikTok, Twitter/X, Reddit, a "
        "magnet link, hundreds of other sites — and I'll <b>leech</b> it "
        "(send it back here) automatically. No command needed for that.\n\n"
        "<code>/m &lt;link&gt;</code> — <b>mirror</b> to Drive\n"
        "<code>/l &lt;link&gt;</code> — <b>leech</b> to Telegram (same as pasting)\n"
        "<code>/zm</code> / <code>/zl</code> — same, zipped first\n\n"
        "<i>All commands accept multiple links, <code>| newname</code>, and "
        "<code>#folder</code> for Drive uploads.</i>\n\n"
        "Uploads go over MTProto directly (Pyrogram) — files up to Telegram's "
        "~2GB per-file cap go through natively.\n\n"
        "Tap a section below for the full command list."
    )
    if msg_id:
        safe_edit(app, chat_id, msg_id, text, reply_markup=kb)
    else:
        app.send_message(chat_id, text, reply_markup=kb)


HELP = {
    "dl": ("⬇️ <b>Downloads</b>\n\n"
           "Paste a link — auto-<b>leeched</b> to Telegram, no command needed.\n\n"
           "<b>Every download command supports:</b>\n"
           "• multiple links → one job per link (batch)\n"
           "• <code>| name</code> after a link → saved as that name (extension untouched)\n"
           "• <code>#folder</code> → Drive uploads go into that folder (auto-created)\n\n"
           "<code>/m &lt;link&gt; | name #folder</code> — mirror to Drive\n"
           "<code>/l &lt;link&gt;</code> — leech to Telegram\n"
           "<code>/zm</code> / <code>/zl</code> — same, zipped into one file first\n"
           "<code>/yt &lt;link&gt;</code>, <code>/y</code>/<code>/ytdl</code>, "
           "<code>/yl</code>/<code>/ytdlleech</code> — WZML-X-style aliases (quality picker)\n\n"
           "<code>/torrent &lt;magnet or .torrent url&gt;</code> — force aria2c\n"
           "<code>/gallery &lt;url&gt;</code> / <code>/clone &lt;url&gt;</code> — gallery-dl / wget mirror\n"
           "<code>/zipl</code> · <code>/zipm</code> · <code>/unzipl</code> — see the 🗃 Zip section\n\n"
           "<code>/drive</code> — browse Drive: get links, send files here, rename/delete, new folders • <code>/drivesearch &lt;q&gt;</code>\n\n"
           "Magnet links are auto-detected in plain messages too. Reply to any file or link instead of typing a URL."),
    "ig": ("📷 <b>Instagram</b>\n\n"
           "<code>&lt;post/reel url&gt;</code> — just paste it, leeched like any other link\n"
           "<code>/ig &lt;profile_url&gt;</code> — multi-select picker: Posts/Reels/Stories/"
           "Highlights/Tagged, then ▶️ Start (Stories on by default). Leeches to Telegram.\n"
           "<code>/igm &lt;profile&gt;</code> — same, but archives go to Drive sorted into "
           "<code>Instagram/&lt;user&gt;/&lt;type&gt;/</code> folders\n"
           "<code>/igl &lt;profile&gt;</code> — same as /ig (explicit leech)\n\n"
           "<b>Indexes</b> (what's already fetched) are stored per user/type — mirrored to Drive.\n"
           "<code>/igindex</code> — view + delete indexes per user or per type. "
           "Deleting an index makes the next run re-fetch everything.\n\n"
           "Stories &amp; Highlights need a logged-in cookie profile (<code>/cookie</code>)."),
    "zip": ("🗃 <b>Zip leech / mirror</b>\n\n"
            "<code>/zipl</code> — gather files into ONE archive, sent to this chat:\n"
            "• reply to a file → zip it\n"
            "• pass direct links: <code>/zipl link1 link2 | myname.zip</code>\n"
            "<code>/zipm</code> — same but the archive goes to Drive (<code>#folder</code> works)\n\n"
            "<code>/unzipl</code> — reply to an archive (<code>.zip .rar .7z .tar…</code>) or pass its "
            "link: every inner file is sent to this chat. <code>/unzipm</code> is an alias.\n\n"
            "All support batch links + <code>| name</code>."),
    "cookies": ("🍪 <b>Cookies</b>\n\n"
                "<code>/cookie</code> — list profiles\n<code>/cookie &lt;name&gt;</code> — switch active profile\n"
                "Reply to a Netscape-format <code>cookies.txt</code> with <code>/cookie &lt;name&gt;</code> to add one.\n\n"
                "Needed for private Instagram accounts, stories/highlights, age-gated videos."),
    "admin": ("🔧 <b>Admin</b>\n\n"
              "<code>/stats</code> — disk, RAM, active jobs\n<code>/clean</code> — purge stale temp files\n"
              "<code>/cancel &lt;id&gt;</code> / 🛑 button — stop a job\n<code>/cancelall</code> — stop everything\n"
              "<code>/updateyt</code> — update yt-dlp now (auto: startup + every 6h)\n"
              "<code>/sh</code> — live persistent shell terminal (arrow keys, history, Ctrl+C button; <code>/shexit</code> quits)\n"
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
        state.mirror_cookie(name)   # persist to Drive datastore
        logger.info(f"chat {message.chat.id} saved cookie profile '{name}'")
        return message.reply_text(
            f"🍪 Saved cookie profile <code>{esc(name)}</code> and made it active.\n"
            f"☁️ Mirrored to Drive: <code>AzBotData/cookies/{esc(os.path.basename(path))}</code>")
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
    rows = [
        [InlineKeyboardButton(
            "🧹 Auto-clean: " + ("✅ ON" if clean_on else "⬜ OFF"),
            callback_data="toggle_autoclean")],
        [InlineKeyboardButton(
            "📄 Send as document: " + ("✅ ON" if doc_on else "⬜ OFF"),
            callback_data="toggle_as_document")],
        [InlineKeyboardButton(f"☁️ Drive ({len(config.DRIVE_ACCOUNTS)} account"
                              f"{'s' if len(config.DRIVE_ACCOUNTS) != 1 else ''}) ▸",
                              callback_data="acctmenu")],
    ]
    return InlineKeyboardMarkup(rows)

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
    acct = state.get_drive_account(chat_id) or "—"
    extra = (f"\n\n☁️ <b>Drive</b>: {len(config.DRIVE_ACCOUNTS)} account(s), "
             f"this chat uploads to <code>{esc(acct)}</code>.")
    return f"⚙️ <b>Settings</b> <i>(this chat only)</i>\n\n{clean_line}\n\n{doc_line}{extra}"

@app.on_message(filters.command("settings") & Authorized)
@guarded
def cmd_settings(client, message):
    message.reply_text(_settings_text(message.chat.id), reply_markup=_settings_kb(message.chat.id))

@app.on_callback_query(filters.regex(r"^acctmenu$"))
@guarded
def cb_acct_menu(client, cq):
    """☁️ Drive management section: list accounts, add via OAuth device
    flow, remove Telegram-registered ones, pick the active one."""
    chat_id = cq.message.chat.id
    current = state.get_drive_account(chat_id)
    lines = []
    rows = []
    for name in sorted(config.DRIVE_ACCOUNTS):
        mark = "✅ " if name == current else ""
        lines.append(f"{mark}<code>{esc(name)}</code>")
        row = [InlineKeyboardButton(("🟢 " if name == current else "") + name,
                                     callback_data=f"acctset:{name}")]
        # only Telegram-registered accounts can be removed here
        if name in _tg_registered_accounts():
            row.append(InlineKeyboardButton("🗑", callback_data=f"acctdel:{name}"))
        rows.append(row)
    text = ("☁️ <b>Drive accounts</b>\n\n"
            + ("\n".join(lines) if lines else "_none configured_")
            + f"\n\nThis chat uploads to: <b>{esc(current or '—')}</b>\n\n"
              "➕ adds a new Google account via a sign-in code — no console needed. "
              "🗑 removes an account added through Telegram (.env accounts are managed in .env).")
    rows.append([InlineKeyboardButton("➕ Add account", callback_data="acctadd")])
    rows.append([InlineKeyboardButton("⬅️ Back to settings", callback_data="settingsback")])
    safe_edit(cq, text, reply_markup=InlineKeyboardMarkup(rows))
    cq.answer()


def _tg_registered_accounts():
    """Account names whose tokens came from Telegram /drivelogin."""
    try:
        from . import handlers_driveauth
        data = state._load_json(handlers_driveauth.OVERRIDES_FILE, {})
        return {k[len("GDrive_"):-len("_REFRESH_TOKEN")]
                for k in data if k.startswith("GDrive_") and k.endswith("_REFRESH_TOKEN")}
    except Exception:
        return set()


@app.on_callback_query(filters.regex(r"^acctadd$"))
@guarded
def cb_acct_add(client, cq):
    state.set_awaiting_input(cq.message.chat.id, "drive_acct_name", None)
    safe_edit(cq, "✏️ Reply with a <b>name</b> for the new Drive account "
                  "(letters/digits/-/_, e.g. <code>work</code> or <code>backup2</code>). "
                  "Send /cancel to abort.")
    cq.answer()


@app.on_callback_query(filters.regex(r"^acctdel:"))
@guarded
def cb_acct_del(client, cq):
    name = cq.data.split(":", 1)[1]
    if name not in _tg_registered_accounts():
        return cq.answer(".env-defined accounts can't be removed here.", show_alert=True)
    try:
        from . import handlers_driveauth
        handlers_driveauth.remove_account(name)
        cq.answer(f"🗑 {name} removed.")
        cb_acct_menu(client, cq)   # re-render
    except Exception as e:
        cq.answer(f"❌ {e}", show_alert=True)


@app.on_callback_query(filters.regex(r"^acctset:"))
@guarded
def cb_acct_set(client, cq):
    name = cq.data.split(":", 1)[1]
    ok = state.set_drive_account(cq.message.chat.id, name)
    cq.answer(f"Switched to {name}" if ok else "Unknown account.", show_alert=not ok)
    if ok:
        cb_acct_menu(client, cq)   # re-render with new selection

@app.on_callback_query(filters.regex(r"^settingsback$"))
@guarded
def cb_settings_back(client, cq):
    chat_id = cq.message.chat.id
    safe_edit(cq, _settings_text(chat_id), reply_markup=_settings_kb(chat_id))
    cq.answer()

@app.on_callback_query(filters.regex(r"^acctback$"))
@guarded
def cb_acct_back(client, cq):
    chat_id = cq.message.chat.id
    safe_edit(cq, _settings_text(chat_id), reply_markup=_settings_kb(chat_id))
    cq.answer()

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
IG_DEDICATED_CMDS = ("igm", "igl")

@app.on_message(filters.command(["m", "l", "zm", "zl", "yt", "y", "ytdl", "ytdlleech",
                                 "yl", "torrent", "igm", "igl"]) & Authorized)
@guarded
def cmd_mirror_leech(client, message):
    """Universal mirror/leech entry. Payload grammar (all commands):
        /m <link> [<link2> …]          batch — one job per link
        /m <link> | myname             rename — saved as `myname`, extension untouched
        /m <link> #folder              Drive: upload into folder `folder` (auto-created)
    Reply mode still works: reply to a file/link with no args.

    /igm <profile> — Instagram archive → Drive (per-type subfolders)
    /igl <profile> — Instagram archive → Telegram"""
    from .utils import parse_payload
    cmd = message.command[0]
    dest = "drive" if cmd in ("m", "zm", "y", "ytdl") else "telegram"
    zip_output = cmd in ("zm", "zl")
    if dest == "drive" and not drive.enabled():
        return message.reply_text("☁️ Drive isn't configured on this bot (missing GCP credentials in <code>.env</code>).")

    raw = ""
    parts = message.text.split(maxsplit=1)
    if len(parts) > 1 and parts[1].strip():
        raw = parts[1].strip()
    elif message.reply_to_message and (message.reply_to_message.text or message.reply_to_message.caption):
        raw = message.reply_to_message.text or message.reply_to_message.caption

    links, rename, folder = parse_payload(raw)

    # Instagram archive commands (/igm /igl): route to the IG picker with
    # the right destination — never through the yt-dlp quality picker.
    if cmd in IG_DEDICATED_CMDS:
        if not links:
            return message.reply_text(
                f"⚠️ Usage: <code>/{esc(cmd)} &lt;profile_or_post_url&gt;</code> [links…]")
        from .handlers_instagram import dispatch_instagram
        dest_ig = "drive" if cmd == "igm" else "telegram"
        if dest_ig == "drive" and not drive.enabled():
            return message.reply_text("☁️ Drive isn't configured — use <code>/igl</code> for Telegram delivery.")
        for u in links:
            dispatch_instagram(client, message, u, dest_ig)
        return

    if not links:
        reply = message.reply_to_message
        if reply and reply.media:
            return queue_telegram_media_job(client, message, reply, dest, zip_output=zip_output)
        return message.reply_text(
            f"⚠️ Usage: <code>/{esc(cmd)} &lt;link&gt;</code> [links…] [ | name] [#folder]\n"
            f"or reply to a link/file with <code>/{esc(cmd)}</code>.")

    # Batch: one job per link; rename only applies to a single link.
    for i, url in enumerate(links):
        r = rename if len(links) == 1 else None
        _dispatch_one(client, message, cmd, url, dest, zip_output, r, folder)


def _dispatch_one(client, message, cmd, url, dest, zip_output, rename=None, folder=None):
    if cmd == "torrent":
        from .handlers_subproc import queue_subprocess_job
        return queue_subprocess_job(client, message, "torrent", url,
                                     dest="telegram" if dest != "drive" or not drive.enabled() else "telegram",
                                     zip_output=zip_output)

    # Instagram NEVER goes through yt-dlp's quality picker (WZML-X parity:
    # gallery-dl owns Instagram — photos, carousels and videos alike; the
    # yt-dlp extractor just errors with "No video formats found!").
    host = _host_of(url)
    if any(host == h or host.endswith("." + h) for h in config.IG_HOSTS):
        from .handlers_instagram import dispatch_instagram
        return dispatch_instagram(client, message, url, dest)

    # Media-page links get the WZML-X-style quality picker (real resolutions
    # + sizes). Direct files/magnets skip it.
    is_media_page = any(host == h or host.endswith("." + h) for h in config.YTDLP_AUTO_HOSTS)
    if is_media_page:
        return show_quality_picker(client, message, url, dest, zip_output,
                                    rename=rename, folder=folder)
    dispatch_link(client, message, url, dest, zip_output=zip_output,
                  rename=rename, folder=folder)


def show_quality_picker(client, message, url, dest, zip_output, rename=None, folder=None):
    status = message.reply_text("🔎 Checking available qualities…")
    try:
        title, options = engine.probe_qualities(url, message.chat.id)
    except Exception as e:
        # Tell the user WHY there's no picker instead of silently grabbing
        # a low-quality merged format (the "360p with black thumbnail"
        # report = probe failed and auto-best ran with a degraded format
        # list). yt-dlp is auto-updated every 6h, so most of these are
        # transient extraction walls — the retry button helps.
        logger.warning(f"quality probe failed for {url}: {e}")
        from pyrogram.types import InlineKeyboardMarkup as _IKM, InlineKeyboardButton as _IKB
        kb = _IKM([[_IKB("🔄 Retry", callback_data=f"ytretry:{state.new_retry_token(url, dest, zip_output, rename, folder)}")]])
        client.edit_message_text(
            message.chat.id, status.id,
            f"⚠️ Couldn't read this video's formats ({esc(str(e)[:140])}).\n"
            f"You can retry, or I'll download best-available directly:",
            reply_markup=kb,
        )
        return
    try:
        _do_picker(client, message, status.id, url, dest, zip_output, rename, folder, title, options)
    except Exception:
        logger.exception("picker render failed")
        dispatch_link(client, message, url, dest, zip_output=zip_output,
                      rename=rename, folder=folder)


def _do_picker(client, message, status_id, url, dest, zip_output, rename, folder, title, options):
    token = state.new_quality_session(url, dest, zip_output, message.id)
    state.QualityExtra.save(token, rename=rename, folder=folder)
    rows, row = [], []
    for label, selector in options:
        row.append(InlineKeyboardButton(label, callback_data=f"ytq:{token}:{selector}"))
        if len(row) == 2:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("⬇️ Best (skip menu)", callback_data=f"ytq:{token}:bv*+ba/b"),
                 InlineKeyboardButton("❌ Cancel", callback_data=f"ytqcancel:{token}")])

    client.edit_message_text(
        message.chat.id, status_id,
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
    extra = state.QualityExtra.take(token)
    cq.answer()
    safe_edit(cq, "⚡ Queued…")
    task_id = new_task_id()
    tag = f" 📦→{config.DEST_LABEL[session['dest']]}" if session["zip_output"] else f" →{config.DEST_LABEL[session['dest']]}"
    status = cq.message.reply_text(f"🔎 Queued{tag} — <code>{esc(task_id)}</code>", reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, cq.message.chat.id, status.id, "yt-dlp", task_dir)
    state.task_queue.put((run_leech_job,
                           (client, cq.message.chat.id, status.id, session["url"], task_id,
                            session["reply_to"], session["dest"], session["zip_output"], selector,
                            extra.get("rename"), extra.get("folder"))))


@app.on_callback_query(filters.regex(r"^ytqcancel:"))
@guarded
def cb_quality_cancel(client, cq):
    token = cq.data.split(":", 1)[1]
    state.pop_quality_session(token)
    safe_edit(cq, "Cancelled.")
    cq.answer()


@app.on_callback_query(filters.regex(r"^ytretry:"))
@guarded
def cb_quality_retry(client, cq):
    token = cq.data.split(":", 1)[1]
    t = state.pop_retry_token(token)
    if not t:
        return cq.answer("Expired — send the link again.", show_alert=True)
    cq.answer("Retrying…")
    safe_edit(cq, "🔎 Checking available qualities… (retry)")
    # Re-run the picker with a FRESH probe; yt-dlp may have recovered.
    try:
        title, options = engine.probe_qualities(t["url"], cq.message.chat.id)
        _do_picker(client, cq.message, cq.message.id, t["url"], t["dest"],
                   t["zip_output"], t["rename"], t["folder"], title, options)
    except Exception as e:
        logger.warning(f"retry probe still failing for {t['url']}: {e}")
        dispatch_link(client, cq.message, t["url"], t["dest"],
                      zip_output=t["zip_output"], rename=t["rename"], folder=t["folder"])


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


def dispatch_link(client, message, url, dest, zip_output=False, rename=None, folder=None):
    """Routes url to the right downloader based on what it looks like —
    magnet/torrent, Instagram, a Google Drive file/folder link, or the
    generic yt-dlp/direct-download path — carrying dest/zip/rename/folder
    through to whichever job actually runs."""
    host = _host_of(url)

    if url.startswith("magnet:") or url.lower().endswith(".torrent"):
        from .handlers_subproc import queue_subprocess_job
        return queue_subprocess_job(client, message, "torrent", url, dest, zip_output)
    if host in ("drive.google.com",):
        m = GDRIVE_RE.search(url)
        if m:
            file_id = m.group(1) or m.group(2)
            return queue_gdrive_job(client, message, file_id, dest, zip_output,
                                     rename=rename, folder=folder)

    if any(host == h or host.endswith("." + h) for h in config.IG_HOSTS):
        from .handlers_instagram import dispatch_instagram
        return dispatch_instagram(client, message, url, dest)

    queue_leech_job(client, message, url, dest, zip_output, rename=rename, folder=folder)


def queue_gdrive_job(client, message, file_id, dest, zip_output=False, rename=None, folder=None):
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
                           (client, message.chat.id, status.id, file_id, task_id, message.id,
                            dest, zip_output, rename, folder)))


def run_gdrive_job(client, chat_id, msg_id, file_id, task_id, reply_to, dest,
                   zip_output=False, rename=None, folder=None):
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
            final_name = os.path.basename(fpath)
            if dest == "telegram":
                if rename:
                    rename = rename.strip()
                    ext = os.path.splitext(fpath)[1]
                    if ext and not rename.lower().endswith(ext.lower()):
                        rename = rename + ext
                    new_path = os.path.join(os.path.dirname(fpath), rename)
                    try:
                        os.rename(fpath, new_path)
                        fpath = new_path
                    except OSError:
                        pass
                    size = os.path.getsize(fpath)
                throttled_edit(client, chat_id, msg_id, f"☁️ Uploading… (<code>{fmtsz(size)}</code>)", markup=cancel_kb(task_id), force=True)
                uploader.upload_to_telegram(client, chat_id, fpath, msg_id, reply_to=reply_to, task_id=task_id)
            else:
                throttled_edit(client, chat_id, msg_id, "☁️ Uploading to Drive…", markup=cancel_kb(task_id), force=True)
                info = drive.upload_file_full(fpath, task_id=task_id, rename=rename,
                                               folder=folder, chat_id=chat_id)
                if info:
                    link = info["link"]
                    done_kb = InlineKeyboardMarkup([[
                        InlineKeyboardButton("🔗 Get link", callback_data=f"qlink:{info['id']}"),
                        InlineKeyboardButton("📥 Send to Telegram", callback_data=f"qsnd:{info['id']}"),
                    ], [
                        InlineKeyboardButton("✏️ Rename", callback_data=f"qren:{info['id']}"),
                        InlineKeyboardButton("🗑 Delete", callback_data=f"qdel:{info['id']}"),
                    ]])
            summary = (f"✅ <b>{esc(final_name)}</b>\n"
                       f"📦 <code>{fmtsz(size)}</code> → {config.DEST_LABEL[dest]}")
            if folder and dest == "drive":
                summary += f" 📁<code>{esc(folder)}</code>"
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



def queue_leech_job(client, message, url, dest, zip_output=False, format_selector=None,
                    rename=None, folder=None):
    task_id = new_task_id()
    tag = f" 📦→{config.DEST_LABEL[dest]}" if zip_output else f" →{config.DEST_LABEL[dest]}"
    status = message.reply_text(f"🔎 Queued{tag} — <code>{esc(task_id)}</code>", reply_markup=cancel_kb(task_id))
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    state.register_job(task_id, message.chat.id, status.id, "yt-dlp", task_dir)
    state.task_queue.put((run_leech_job,
                           (client, message.chat.id, status.id, url, task_id, message.id, dest,
                            zip_output, format_selector, rename, folder)))


def run_leech_job(client, chat_id, msg_id, url, task_id, reply_to, dest, zip_output=False,
                  format_selector=None, rename=None, folder=None):
    task_dir = os.path.join(config.DOWNLOAD_DIR, task_id)
    try:
        if not state.ensure_free(config.MIN_FREE_MB):
            return throttled_edit(client, chat_id, msg_id, "❌ Disk full, try again later.", force=True)

        throttled_edit(client, chat_id, msg_id, "🔎 Resolving link…", markup=cancel_kb(task_id), force=True)

        host = _host_of(url)
        known_site = any(host == h or host.endswith("." + h) for h in config.YTDLP_AUTO_HOSTS)
        # Instagram is gallery-dl territory, never yt-dlp (WZML-X parity).
        if any(host == h or host.endswith("." + h) for h in config.IG_HOSTS):
            from .handlers_subproc import queue_subprocess_job
            return queue_subprocess_job(client, message, "gallery", url, dest, zip_output)
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
            if rename:
                # Extension is NEVER replaced: append the real one if the
                # requested name doesn't already end with it ("my.video"
                # keeps ".mp4"; "clip.mp4" stays "clip.mp4").
                rename = rename.strip()
                ext = os.path.splitext(fpath)[1]
                if ext and not rename.lower().endswith(ext.lower()):
                    rename = rename + ext
                new_path = os.path.join(os.path.dirname(fpath), rename)
                try:
                    os.rename(fpath, new_path)
                    fpath = new_path
                except OSError:
                    pass
                size = os.path.getsize(fpath)
            throttled_edit(client, chat_id, msg_id, f"☁️ Uploading… (<code>{fmtsz(size)}</code>)", markup=cancel_kb(task_id), force=True)
            uploader.upload_to_telegram(client, chat_id, fpath, msg_id, reply_to=reply_to, task_id=task_id,
                                        video_meta=engine.probe_video_meta(fpath))
        elif dest == "drive":
            throttled_edit(client, chat_id, msg_id, "☁️ Uploading to Drive…", markup=cancel_kb(task_id), force=True)
            dinfo = drive.upload_file_full(fpath, task_id=task_id, rename=rename,
                                           folder=folder, chat_id=chat_id)
            if dinfo:
                links.append(dinfo["link"])
                file_kb = InlineKeyboardMarkup([[
                    InlineKeyboardButton("🔗 Get link", callback_data=f"qlink:{dinfo['id']}"),
                    InlineKeyboardButton("📥 Send to Telegram", callback_data=f"qsnd:{dinfo['id']}"),
                ], [
                    InlineKeyboardButton("✏️ Rename", callback_data=f"qren:{dinfo['id']}"),
                    InlineKeyboardButton("🗑 Delete", callback_data=f"qdel:{dinfo['id']}"),
                ]])

        summary = (f"✅ <b>{esc(os.path.basename(fpath))}</b>\n"
                   f"📦 <code>{fmtsz(size)}</code> → {config.DEST_LABEL[dest]}")
        if folder and dest == "drive":
            summary += f" 📁<code>{esc(folder)}</code>"
        title = (info or {}).get("title") if isinstance(info, dict) else None
        if title and dest != "telegram":
            summary = f"🎬 {esc(str(title)[:80])}\n{summary}"
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
        if kind == "drive_acct_name":
            from .handlers_driveauth import start_device_login
            return start_device_login(client, message, (message.text or "").strip())
        if isinstance(kind, tuple) and kind[0] == "drive_token":
            from .handlers_driveauth import _handle_token_input
            return _handle_token_input(client, message, kind[1])
        if isinstance(kind, tuple) and kind[0] == "drive_rename":
            from .handlers_drive import handle_drive_rename
            return handle_drive_rename(client, message, kind[1], target)
        if kind == "drive_rename":
            from .handlers_drive import handle_drive_rename
            return handle_drive_rename(client, message, state.DEFAULT_ACCOUNT, target)
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

    # Same grammar as /m: "| name" rename and "#folder" Drive routing.
    from .utils import parse_payload
    _, rename, folder = parse_payload(text)
    dispatch_link(client, message, url, "telegram", rename=rename, folder=folder)
