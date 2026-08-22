from pyrogram import filters
from pyrogram.types import InlineKeyboardMarkup, InlineKeyboardButton

from . import drive, state, log
from .core import app
from .utils import fmtsz, guarded, safe_edit
from .handlers_core import Authorized

logger = log.get(__name__)

PAGE_SIZE = 10
FOLDER_MIME = "application/vnd.google-apps.folder"


def _need_drive(message_or_query):
    if drive.enabled():
        return False
    text = "☁️ Drive isn't configured on this bot (missing GCP credentials in `.env`)."
    if hasattr(message_or_query, "data"):
        message_or_query.answer(text, show_alert=True)
    else:
        message_or_query.reply_text(text)
    return True


def _get_listing(chat_id, folder_id):
    files = state.get_cached_listing(chat_id, folder_id)
    if files is None:
        files = drive.list_folder_full(folder_id)
        state.set_cached_listing(chat_id, folder_id, files)
    return files


def _file_row(f):
    is_folder = f.get("mimeType") == FOLDER_MIME
    icon = "📁" if is_folder else "📄"
    size = f" · {fmtsz(int(f['size']))}" if f.get("size") else ""
    name = f.get("name", "?")
    label = f"{icon} {name[:28]}{'…' if len(name) > 28 else ''}{size}"
    cb = f"drvcd:{f['id']}" if is_folder else f"drvopen:{f['id']}"
    return [InlineKeyboardButton(label, callback_data=cb)]


def _render(chat_id, page=0):
    nav = state.get_drive_nav(chat_id)
    folder_id, folder_name = nav[-1]
    files = _get_listing(chat_id, folder_id)

    total_pages = max(1, -(-len(files) // PAGE_SIZE))  # ceil div
    page = max(0, min(page, total_pages - 1))
    page_files = files[page * PAGE_SIZE: (page + 1) * PAGE_SIZE]

    breadcrumb = " › ".join(n for _, n in nav[-3:])
    n = len(files)
    count_line = "_(empty)_" if n == 0 else f"{n} item{'s' if n != 1 else ''}"
    if total_pages > 1:
        count_line += f" — page {page + 1} of {total_pages}"
    text = f"☁️ **Drive**\n📍 {breadcrumb}\n\n{count_line}"

    rows = []
    if len(nav) > 1:
        rows.append([InlineKeyboardButton(f"⬆️ Back to {nav[-2][1][:20]}", callback_data="drvup")])
    rows += [_file_row(f) for f in page_files]

    nav_row = []
    if page > 0:
        nav_row.append(InlineKeyboardButton("◀️", callback_data=f"drvpg:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"{page + 1}/{total_pages}", callback_data="drvnoop"))
    if page < total_pages - 1:
        nav_row.append(InlineKeyboardButton("▶️", callback_data=f"drvpg:{page + 1}"))
    if len(nav_row) > 1:
        rows.append(nav_row)

    bottom = [InlineKeyboardButton("📁 New folder", callback_data="drvnewfolder"),
              InlineKeyboardButton("🔄 Refresh", callback_data="drvrefresh")]
    if len(nav) > 1:  # never offer to delete the configured root folder
        bottom.insert(1, InlineKeyboardButton("🗑 Delete this folder", callback_data=f"drvdelfolder:{folder_id}"))
    rows.append(bottom)

    return text, InlineKeyboardMarkup(rows)


def _show(chat_id, msg_id, page=0, cq=None):
    text, kb = _render(chat_id, page)
    if cq:
        safe_edit(cq, text, reply_markup=kb, disable_web_page_preview=True)
    else:
        app.edit_message_text(chat_id, msg_id, text, reply_markup=kb, disable_web_page_preview=True)


@app.on_message(filters.command("drive") & Authorized)
@guarded
def cmd_drive(client, message):
    if _need_drive(message):
        return
    state.reset_drive_nav(message.chat.id)
    state.invalidate_listing(message.chat.id)
    text, kb = _render(message.chat.id, 0)
    message.reply_text(text, reply_markup=kb, disable_web_page_preview=True)


@app.on_message(filters.command("drivesearch") & Authorized)
@guarded
def cmd_drivesearch(client, message):
    if _need_drive(message):
        return
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2 or not parts[1].strip():
        return message.reply_text("⚠️ `/drivesearch <query>`")
    files = drive.search_files(parts[1].strip())
    if not files:
        return message.reply_text(f"☁️ No results for `{parts[1].strip()}`.")
    rows = [_file_row(f) for f in files[:PAGE_SIZE]]
    message.reply_text(f"☁️ **Search:** {parts[1].strip()}", reply_markup=InlineKeyboardMarkup(rows))


@app.on_callback_query(filters.regex(r"^drvnoop$"))
@guarded
def cb_drive_noop(client, cq):
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvpg:"))
@guarded
def cb_drive_page(client, cq):
    page = int(cq.data.split(":", 1)[1])
    _show(cq.message.chat.id, cq.message.id, page, cq=cq)
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvcd:"))
@guarded
def cb_drive_cd(client, cq):
    folder_id = cq.data.split(":", 1)[1]
    files = _get_listing(cq.message.chat.id, state.get_drive_nav(cq.message.chat.id)[-1][0])
    name = next((f["name"] for f in files if f["id"] == folder_id), folder_id)
    state.push_drive_nav(cq.message.chat.id, folder_id, name)
    _show(cq.message.chat.id, cq.message.id, 0, cq=cq)
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvup$"))
@guarded
def cb_drive_up(client, cq):
    state.pop_drive_nav(cq.message.chat.id)
    _show(cq.message.chat.id, cq.message.id, 0, cq=cq)
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvrefresh$"))
@guarded
def cb_drive_refresh(client, cq):
    state.invalidate_listing(cq.message.chat.id)
    _show(cq.message.chat.id, cq.message.id, 0, cq=cq)
    cq.answer("Refreshed")


@app.on_callback_query(filters.regex(r"^drvopen:"))
@guarded
def cb_drive_open(client, cq):
    file_id = cq.data.split(":", 1)[1]
    f = drive.get_file(file_id)
    if not f:
        return cq.answer("File not found — it may have already been deleted.", show_alert=True)
    size = fmtsz(int(f["size"])) if f.get("size") else "—"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("🔗 Open in Drive", url=f["webViewLink"])],
        [InlineKeyboardButton("✏️ Rename", callback_data=f"drvren:{file_id}"),
         InlineKeyboardButton("🗑 Delete", callback_data=f"drvdel:{file_id}")],
        [InlineKeyboardButton("⬅️ Back", callback_data="drvback")],
    ])
    safe_edit(cq, f"📄 **{f['name']}**\n📦 {size}", reply_markup=kb)
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvback$"))
@guarded
def cb_drive_back(client, cq):
    _show(cq.message.chat.id, cq.message.id, 0, cq=cq)
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvdel:"))
@guarded
def cb_drive_delete_confirm(client, cq):
    file_id = cq.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, delete", callback_data=f"drvdelok:{file_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data=f"drvopen:{file_id}"),
    ]])
    safe_edit(cq, "🗑 Delete this file from Drive? This can't be undone.", reply_markup=kb)
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvdelok:"))
@guarded
def cb_drive_delete(client, cq):
    file_id = cq.data.split(":", 1)[1]
    try:
        ok = drive.delete_file(file_id)
    except Exception as e:
        logger.warning(f"drive delete failed for {file_id}: {e}")
        ok = False
    cq.answer("🗑 Deleted." if ok else "❌ Failed to delete.", show_alert=not ok)
    state.invalidate_listing(cq.message.chat.id)
    _show(cq.message.chat.id, cq.message.id, 0, cq=cq)


@app.on_callback_query(filters.regex(r"^drvdelfolder:"))
@guarded
def cb_drive_delete_folder_confirm(client, cq):
    folder_id = cq.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("⚠️ Yes, delete folder", callback_data=f"drvdelfolderok:{folder_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="drvback"),
    ]])
    safe_edit(cq, "🗑 Delete this folder **and everything in it**? This can't be undone.", reply_markup=kb)
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvdelfolderok:"))
@guarded
def cb_drive_delete_folder(client, cq):
    folder_id = cq.data.split(":", 1)[1]
    try:
        ok = drive.delete_file(folder_id)
    except Exception as e:
        logger.warning(f"drive folder delete failed for {folder_id}: {e}")
        ok = False
    if ok:
        state.pop_drive_nav(cq.message.chat.id)
        state.invalidate_listing(cq.message.chat.id)
        cq.answer("🗑 Folder deleted.")
    else:
        cq.answer("❌ Failed to delete folder.", show_alert=True)
    _show(cq.message.chat.id, cq.message.id, 0, cq=cq)


@app.on_callback_query(filters.regex(r"^drvren:"))
@guarded
def cb_drive_rename_prompt(client, cq):
    file_id = cq.data.split(":", 1)[1]
    state.set_awaiting_input(cq.message.chat.id, "drive_rename", file_id)
    safe_edit(cq, "✏️ Reply with the new file name (or wait 2 minutes to cancel).")
    cq.answer()


@app.on_callback_query(filters.regex(r"^drvnewfolder$"))
@guarded
def cb_drive_new_folder_prompt(client, cq):
    folder_id = state.get_drive_nav(cq.message.chat.id)[-1][0]
    state.set_awaiting_input(cq.message.chat.id, "drive_newfolder", folder_id)
    safe_edit(cq, "📁 Reply with the new folder's name (or wait 2 minutes to cancel).")
    cq.answer()


def handle_drive_rename(client, message, file_id):
    """Called from handlers_core.auto_leech when a chat has a pending
    drive-rename waiting on the next text message."""
    new_name = (message.text or "").strip()
    if not new_name:
        return message.reply_text("⚠️ Empty name, rename cancelled.")
    try:
        ok = drive.rename_file(file_id, new_name)
    except Exception as e:
        logger.warning(f"drive rename failed for {file_id}: {e}")
        ok = False
    state.invalidate_listing(message.chat.id)
    if ok:
        message.reply_text(f"✅ Renamed to `{new_name}`.")
    else:
        message.reply_text("❌ Rename failed — the file may have been deleted.")


def handle_drive_new_folder(client, message, parent_id):
    """Called from handlers_core.auto_leech for a pending 'new folder' prompt."""
    name = (message.text or "").strip()
    if not name:
        return message.reply_text("⚠️ Empty name, folder not created.")
    try:
        fid = drive.create_folder(name, parent_id)
    except Exception as e:
        logger.warning(f"drive create_folder failed: {e}")
        fid = None
    state.invalidate_listing(message.chat.id)
    if fid:
        message.reply_text(f"✅ Created folder `{name}`. Use /drive to see it.")
    else:
        message.reply_text("❌ Couldn't create the folder.")


# ── "Quick actions" on a mirror-completion message ──────────────────────
# Same rename/delete capability as the in-browser drvren:/drvdel:, but
# these stand alone on a job's "✅ Done" message rather than inside a
# /drive session — so acting on them shows a plain confirmation and
# leaves that message alone, instead of turning it into a folder browser.

@app.on_callback_query(filters.regex(r"^qdel:"))
@guarded
def cb_quick_delete_confirm(client, cq):
    file_id = cq.data.split(":", 1)[1]
    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ Yes, delete", callback_data=f"qdelok:{file_id}"),
        InlineKeyboardButton("❌ Cancel", callback_data="qcancel"),
    ]])
    safe_edit(cq, "🗑 Delete this file from Drive? This can't be undone.", reply_markup=kb)
    cq.answer()


@app.on_callback_query(filters.regex(r"^qdelok:"))
@guarded
def cb_quick_delete(client, cq):
    file_id = cq.data.split(":", 1)[1]
    try:
        ok = drive.delete_file(file_id)
    except Exception as e:
        logger.warning(f"quick delete failed for {file_id}: {e}")
        ok = False
    state.invalidate_listing(cq.message.chat.id)
    safe_edit(cq, "🗑 Deleted." if ok else "❌ Failed to delete — it may already be gone.")
    cq.answer()


@app.on_callback_query(filters.regex(r"^qren:"))
@guarded
def cb_quick_rename_prompt(client, cq):
    file_id = cq.data.split(":", 1)[1]
    state.set_awaiting_input(cq.message.chat.id, "drive_rename", file_id)
    safe_edit(cq, "✏️ Reply with the new file name (or wait 2 minutes to cancel).")
    cq.answer()


@app.on_callback_query(filters.regex(r"^qcancel$"))
@guarded
def cb_quick_cancel(client, cq):
    safe_edit(cq, "Cancelled.")
    cq.answer()
