from pyrogram import filters

from . import log
from .utils import guarded
from .core import app
from .handlers_core import Authorized, AdminOnly

logger = log.get(__name__)

ADMIN_COMMANDS = ["sh", "shell", "allow", "ban", "cancelall", "ca", "updateyt", "ytupdate"]
KNOWN_COMMANDS = [
    "start", "help", "m", "l", "zm", "zl", "yt", "y", "ytdl", "ytdlleech", "yl",
    "ig", "torrent", "gallery", "clone", "cookie", "settings", "drive", "drivesearch",
    "stats", "clean", "cancel", "cancelall", "ca", "sh", "shell", "allow", "ban",
    "updateyt", "ytupdate",
]

# These only ever run *after* every real command handler above has had its
# shot (Pyrogram tries handlers in registration order within a group and
# stops at the first match) — so reaching one of these means nothing else
# claimed the message. Registered last in core.py for exactly that reason.


@app.on_message(filters.command(ADMIN_COMMANDS) & ~AdminOnly)
@guarded
def deny_admin_only(client, message):
    logger.info(f"chat {message.chat.id} denied admin-only command: {message.text}")
    message.reply_text("🔒 That's an admin-only command.")


@app.on_message(filters.command(KNOWN_COMMANDS) & ~Authorized)
@guarded
def deny_unauthorized(client, message):
    uid = message.from_user.id if message.from_user else "?"
    logger.info(f"chat {message.chat.id} (user {uid}) denied — not authorized")
    message.reply_text("🚫 You're not authorized to use this bot. Ask the admin to run `/allow " + str(uid) + "`.")


@app.on_message(filters.regex(r"^/\w+") & filters.text)
@guarded
def unknown_command(client, message):
    message.reply_text("❓ Unknown command — see /help for the full list.")
