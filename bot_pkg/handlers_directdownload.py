"""/directdownload — connect an Instagram account to this chat and download
Instagram content by DMing links to the bot's Instagram account.

The pairing exists because Telegram and Instagram have no shared identity:
a DM arrives with an Instagram-scoped ID (IGSID) and nothing else. /directdownload
mints a short code; sending that code as a DM to the bot's Instagram account
tells the bot which Telegram chat the IGSID belongs to. No password is ever
asked for or stored — the Instagram side authenticates through Meta's own
Messaging API, and the only thing kept on disk is IGSID → chat id.

/gdmlinks (or the buttons) shows and revokes what's connected.
"""
import textwrap

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import config, state, log, ig_token
from .core import app
from .ig_dm import FEATURES, LABEL, issue_code, unlink
from .handlers_core import Authorized, AdminOnly
from .utils import esc, guarded, safe_edit

logger = log.get(__name__)


def _status_text(chat_id):
    from .ig_dm import _load
    d = _load()
    mine = [k for k, v in (d.get("links") or {}).items() if v.get("chat_id") == chat_id]
    if not config.IG_WEBHOOK_PORT or not config.IG_PAGE_TOKEN:
        head = ("⚙️ <b>Instagram DM downloads — not configured</b>\n\n"
                "The admin needs to set <code>IG_APP_SECRET</code>, "
                "<code>IG_PAGE_TOKEN</code>, <code>IG_ACCOUNT_ID</code>, "
                "<code>IG_VERIFY_TOKEN</code> and <code>IG_WEBHOOK_PORT</code>. "
                "That requires an Instagram Professional account linked to a "
                "Facebook Page plus Meta app review — see README.")
    else:
        head = ("📥 <b>Instagram DM downloads</b>\n\n"
                "DM any Instagram link to this bot's Instagram account and the "
                "files arrive here. Instagram password is never asked for: the "
                "connection is made by sending a code, and Meta's official "
                "Messaging API delivers the messages.")
    lines = [head, "", "<b>Supported</b>"]
    for _, mark, desc in FEATURES:
        lines.append(f"{mark} {desc}")
    lines += ["", "<b>How to connect</b>",
              "1. Tap <b>🔗 Connect</b> below for a one-time code.",
              "2. DM that code to the bot's Instagram account.",
              "3. Send an Instagram link in the same DM thread."]
    st = ig_token.state()
    if st != "no_token":
        left = ig_token.days_left()
        # The token is a shared server credential, so its state is shown to
        # everyone: a silent expiry would otherwise look like "the bot broke".
        lines.append(f"\n🔑 Token: {ig_token.STATUS[st]}"
                     + (f" — {left:.0f} days left" if left is not None else ""))
        if st in ("expiring", "expired"):
            lines.append("<i>Admin: <code>/igtoken &lt;token&gt;</code>, or let the "
                         "auto-refresh run.</i>")
    if mine:
        lines.append(f"\n✅ Linked: <code>{len(mine)}</code> Instagram account(s).")
    else:
        lines.append("\n⚠️ No Instagram account connected to this chat yet.")
    return "\n".join(lines)


def _kb(chat_id):
    from .ig_dm import _load
    d = _load()
    mine = [k for k, v in (d.get("links") or {}).items() if v.get("chat_id") == chat_id]
    rows = [[InlineKeyboardButton("🔗 Connect Instagram", callback_data="igdm:connect")]]
    if mine:
        rows.append([InlineKeyboardButton("🔌 Disconnect all", callback_data="igdm:unlink")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="igdm:refresh")])
    return InlineKeyboardMarkup(rows)


@app.on_message(filters.command(["directdownload", "ddownload"]) & Authorized)
@guarded
def cmd_directdownload(client, message):
    message.reply_text(_status_text(message.chat.id), reply_markup=_kb(message.chat.id),
                       disable_web_page_preview=True)


@app.on_message(filters.command(["gdmlinks"]) & Authorized)
@guarded
def cmd_gdmlinks(client, message):
    cmd_directdownload(client, message)


@app.on_message(filters.command("igtoken") & AdminOnly)
@guarded
def cmd_igtoken(client, message):
    """/igtoken <token> — store a new Instagram access token.

    Accepted over DM only: the token is a server credential, and the message is
    deleted the moment it's read so it doesn't linger in chat history.
    """
    if message.chat.type != "private":
        return message.reply_text("🔒 Send that in a private chat with me, not in a group.")

    parts = (message.text or "").split(maxsplit=1)
    arg = parts[1].strip() if len(parts) > 1 else ""
    if arg.lower() in ("refresh", "renew"):
        ok, why = ig_token.refresh(force=True)
        icon = "✅" if ok else "❌"
        return message.reply_text(f"{icon} {esc(why)}")
    if arg.lower() in ("status", ""):
        left = ig_token.days_left()
        return message.reply_text(
            f"🔑 {ig_token.STATUS[ig_token.state()]}"
            + (f" — {left:.0f} days left" if left is not None else "")
            + "\n\n<code>/igtoken &lt;token&gt;</code> — store a new token"
            "\n<code>/igtoken refresh</code> — renew now (Instagram Login only)")

    # delete first: the token must not stay in chat history even if the rest fails
    try:
        message.delete()
    except Exception as e:
        logger.warning(f"/igtoken: couldn't delete the message: {e}")

    ok, info = ig_token.validate(arg)
    if not ok:
        return message.reply_text(f"❌ That token didn't work: {esc(info)}\n"
                                  "Nothing was saved.")
    ig_token.save(arg, None, source="tg")

    # A fresh token is worth an immediate DM to the accounts already linked:
    # otherwise the first message a user sends looks like a broken bot.
    ok2, why = ig_token.refresh() if "graph.instagram.com" in config.IG_API_HOST else (False, "")
    tail = f"\n🔄 Auto-refresh available: {esc(why)}" if why else ""
    message.reply_text(f"✅ Token saved and verified ({esc(info)}). "
                       f"Instagram DMs will work again.{tail}\n"
                       f"<i>Your message with the token was deleted.</i>")


@app.on_callback_query(filters.regex(r"^igdm:"))
@guarded
def cb_igdm(client, cq):
    action = cq.data.split(":", 1)[1]
    chat_id = cq.message.chat.id

    if action == "connect":
        if not config.IG_WEBHOOK_PORT or not config.IG_PAGE_TOKEN:
            return cq.answer("Not configured — ask the admin to set the IG_* vars.", show_alert=True)
        code = issue_code(chat_id)
        safe_edit(cq, textwrap.dedent(f"""\
            🔗 <b>Connect your Instagram</b>

            Send this code as a DM to the bot's Instagram account:

            <code>{code}</code>

            It works once and expires in 30 minutes. Once it's accepted, DM any
            Instagram post, reel, story or highlight link and it lands here.

            <i>This bot never asks for your Instagram password. The code only
            tells it which Telegram chat to deliver to.</i>"""),
                  reply_markup=InlineKeyboardMarkup(
                      [[InlineKeyboardButton("⬅️ Back", callback_data="igdm:refresh")]]))
        return cq.answer("Code created")

    if action == "unlink":
        n = unlink(chat_id)
        cq.answer(f"Disconnected {n} account(s).", show_alert=True)
    else:
        cq.answer()

    safe_edit(cq, _status_text(chat_id), reply_markup=_kb(chat_id),
              disable_web_page_preview=True)
