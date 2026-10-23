import os
import time, shutil, subprocess, threading
from collections import deque

from pyrogram import filters
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from . import config, state, build_info, drive
from .core import app
from .utils import fmt_time, fmtsz, guarded, safe_edit, esc, fit_html
from .handlers_core import Authorized, AdminOnly
from .handlers_shell import register_shell_handlers


@app.on_message(filters.command("stats") & Authorized)
@guarded
def cmd_stats(client, message):
    free = state.disk_free_mb()
    total = shutil.disk_usage(config.DOWNLOAD_DIR).total // (1024 * 1024)
    rss = int(_rss_mb())
    with state.jobs_lock:
        jobs = dict(state.ACTIVE_JOBS)
    lines = [
        f"  🔴 <code>{esc(tid)}</code> — {j.get('kind', '?')} ({fmt_time(int(time.time() - j.get('start', time.time())))})"
        for tid, j in jobs.items()
    ]
    message.reply_text(
        f"📊 <b>Stats</b>\n\n"
        f"💾 Disk: <code>{free} MB</code> free / <code>{total} MB</code>\n"
        f"🧠 RAM: <code>{rss} MB</code> used by bot\n"
        f"⚡ Crypto: " + ("<b>native (fast)</b>" if _crypto_ok() else "<i>pure-Python fallback — uploads ~2-3x slower!</i>") + "\n"
        f"⚙️ Active: <code>{len(jobs)}</code> | Queued: <code>{state.task_queue.qsize()}</code>\n"
        f"☁️ Drive accounts: <code>{len(config.DRIVE_ACCOUNTS)}</code>"
        + (f" (this chat: <code>{state.get_drive_account(message.chat.id)}</code>)" if config.DRIVE_ACCOUNTS else "")
        + f"\n🏷 Build: <code>{esc(build_info.build_line())}</code>"
        + "\n\n<b>Active jobs:</b>\n" + ("\n".join(lines) if lines else "  <i>none</i>"),
    )


def _rss_mb():
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0


def _crypto_ok():
    """True when native TgCrypto is actually in use — the single most
    common cause of 'uploads are extremely slow' is this being False."""
    from . import crypto_guard
    return crypto_guard.enabled()


@app.on_message(filters.command("ping") & Authorized)
@guarded
def cmd_ping(client, message):
    """WZML-X's /ping: uptime + latency in one line. The single fastest
    'is it alive and awake' check without reading /stats."""
    t0 = time.time()
    msg = message.reply_text("🏓 Pong…")
    latency_ms = int((time.time() - t0) * 1000)
    with state.jobs_lock:
        n_jobs = len(state.ACTIVE_JOBS)
    uptime = fmt_time(time.time() - state.BOOT_TS)
    safe_edit(client, message.chat.id, msg.id,
              f"🏓 <b>Pong</b> — <code>{latency_ms} ms</code>\n"
              f"⏱ Uptime: <code>{uptime}</code>\n"
              f"⚙️ Jobs: <code>{n_jobs}</code> running • "
              f"<code>{state.task_queue.qsize()}</code> queued")


@app.on_message(filters.command("count") & Authorized)
@guarded
def cmd_count(client, message):
    """Storage tally per destination — what WZML-X's /count gives: how many
    files and how many bytes the bot has actually delivered, from its own
    logs rather than a guess."""
    parts = (message.text or "").split(maxsplit=1)
    target = parts[1].strip().lower() if len(parts) > 1 else ""
    task_dir = config.DOWNLOAD_DIR
    files = 0
    total = 0
    try:
        for root, _dirs, names in os.walk(task_dir):
            for n in names:
                try:
                    total += os.path.getsize(os.path.join(root, n))
                    files += 1
                except OSError:
                    continue
    except OSError as e:
        return message.reply_text(f"❌ Can't read the download dir: <code>{esc(e)}</code>")
    if target == "drive":
        if not config.DRIVE_ACCOUNTS:
            return message.reply_text("☁️ No Drive account configured. See <code>/settings</code>.")
        try:
            items = drive.list_files(page_size=1000)
        except Exception as e:
            return message.reply_text(f"❌ Drive listing failed: <code>{esc(str(e)[:200])}</code>")
        if items is None:
            return message.reply_text("❌ Drive not reachable — check <code>/settings</code> for the account state.")
        dbytes = sum(int(f.get("size") or 0) for f in items)
        return message.reply_text(
            f"☁️ <b>Drive</b> ({len(config.DRIVE_ACCOUNTS)} account(s))\n"
            f"📄 Files at root: <code>{len(items)}</code>\n"
            f"💾 Size: <code>{fmtsz(dbytes)}</code>\n"
            f"<i>Root level only — /drive browses folders.</i>")
    free = state.disk_free_mb()
    return message.reply_text(
        f"📊 <b>Count</b>\n\n"
        f"🗂 Temp files on disk: <code>{files}</code> • <code>{fmtsz(total)}</code>\n"
        f"💾 Free disk: <code>{free} MB</code>\n"
        f"⚙️ Active jobs: <code>{len(state.ACTIVE_JOBS)}</code> • "
        f"queued: <code>{state.task_queue.qsize()}</code>\n\n"
        f"☁️ <code>/count drive</code> — Drive root tally\n"
        f"🧹 <code>/clean</code> — purge temp files")


@app.on_message(filters.command(["users", "allowed"]) & AdminOnly)
@guarded
def cmd_users(client, message):
    """Who can use the bot. /allow and /ban changed the set but nothing
    ever displayed it — you had to remember or read users.json on the box."""
    users = state.allowed_users()
    lines = [f"👥 <b>Authorized users</b> ({len(users)})", ""]
    if not users:
        lines.append("  <i>open to everyone</i> — no ADMIN_ID and no allow-list")
    for uid in sorted(users, key=lambda u: (u != str(config.ADMIN_ID), u)):
        lines.append(f"  <code>{esc(uid)}</code>" +
                     (" — <b>admin</b>" if uid == str(config.ADMIN_ID) else ""))
    lines += ["", "➕ <code>/allow &lt;id&gt;</code> — add",
              "➖ <code>/ban &lt;id&gt;</code> — remove"]
    message.reply_text("\n".join(lines))


@app.on_message(filters.command(["status", "jobs"]) & Authorized)
@guarded
def cmd_status(client, message):
    """Live job list with a 🛑 button per job — /cancel alone needed the task
    id, which users only ever saw inside the 'Queued' line they'd already
    scrolled past. Non-admins see only their own chat's jobs."""
    admin = bool(message.from_user) and str(message.from_user.id) == config.ADMIN_ID
    with state.jobs_lock:
        jobs = dict(state.ACTIVE_JOBS)
    if not admin:
        jobs = {t: j for t, j in jobs.items() if j.get("chat_id") == message.chat.id}
    queued = state.task_queue.qsize()
    if not jobs:
        return message.reply_text(
            f"ℹ️ No active jobs{' — ' + str(queued) + ' queued' if queued else ''}.")
    now = time.time()
    lines = [f"📋 <b>Active jobs</b> ({len(jobs)} running"
             + (f", {queued} queued" if queued else "") + ")"]
    rows = []
    for tid, j in list(jobs.items())[:10]:      # 10 = keyboard row budget
        elapsed = fmt_time(int(now - j.get("start", now)))
        lines.append(f"  🔴 <code>{esc(tid)}</code> — {esc(j.get('kind', '?'))} • {elapsed}")
        rows.append([InlineKeyboardButton(f"🛑 Stop {j.get('kind', '?')} · {tid}",
                                          callback_data=f"cancel:{tid}")])
    if len(jobs) > 10:
        lines.append(f"  <i>…and {len(jobs) - 10} more (see /stats)</i>")
    message.reply_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(rows))


@app.on_message(filters.command("log") & AdminOnly)
@guarded
def cmd_log(client, message):
    """Tail bot.log without SSH — the whole point of a headless deploy is
    that nobody can go read the file, and /stats tells you a job failed
    without saying why."""
    parts = (message.text or "").split()
    try:
        n = max(10, min(500, int(parts[1]))) if len(parts) > 1 else 120
    except ValueError:
        n = 120
    try:
        with open(config.LOG_FILE, encoding="utf-8", errors="replace") as fh:
            tail = "".join(deque(fh, maxlen=n)) or "(log is empty)"
    except OSError as e:
        return message.reply_text(f"❌ Can't read the log: <code>{esc(e)}</code>")
    message.reply_text(f"<pre>{fit_html(tail, limit=3800)}</pre>")


@app.on_message(filters.command("clean") & Authorized)
@guarded
def cmd_clean(client, message):
    before = state.disk_free_mb()
    state.purge_stale(max_age_s=0)
    message.reply_text(
        f"🧹 Freed <code>{state.disk_free_mb() - before} MB</code>. "
        f"Now <code>{state.disk_free_mb()} MB</code> free."
    )


@app.on_message(filters.command("cancel") & Authorized)
@guarded
def cmd_cancel(client, message):
    parts = message.text.split(maxsplit=1)
    with state.jobs_lock:
        ids = list(state.ACTIVE_JOBS.keys())
    if len(parts) < 2:
        if not ids:
            return message.reply_text("ℹ️ No active jobs.")
        lines = ["Usage: <code>/cancel &lt;id&gt;</code>", "", "<b>Active:</b>"]
        with state.jobs_lock:
            for tid in ids:
                j = state.ACTIVE_JOBS.get(tid, {})
                lines.append(f"  <code>{esc(tid)}</code> — {j.get('kind', '?')}")
        return message.reply_text("\n".join(lines))
    tid = parts[1].strip()
    ok = state.cancel_task(tid)
    message.reply_text(f"🛑 Cancelling <code>{esc(tid)}</code>…" if ok else f"⚠️ No active job <code>{esc(tid)}</code>.")


@app.on_message(filters.command(["cancelall", "ca"]) & AdminOnly)
@guarded
def cmd_cancel_all(client, message):
    with state.jobs_lock:
        ids = list(state.ACTIVE_JOBS.keys())
    count = sum(1 for tid in ids if state.cancel_task(tid))
    message.reply_text(f"💥 {count} job(s) signalled to stop.")


# Legacy one-shot /sh kept as an alias — the real terminal is the persistent
# PTY session in handlers_shell.py (arrow keys, history, live output).
SHELL_TIMEOUT_S = config.SHELL_TIMEOUT_S

def _legacy_sh(client, message):
    parts = message.text.split(maxsplit=1)
    if len(parts) < 2:
        return message.reply_text(
            "⚠️ <code>/sh &lt;command&gt;</code> runs a one-off command.\n"
            "Send <code>/sh</code> alone to open the <b>live terminal session</b> instead "
            "(persistent shell, arrow keys, history).")
    shell_cmd = parts[1]
    msg = message.reply_text(f"⚙️ Running…\n<code>{esc(shell_cmd[:100])}</code>")

    def _run():
        try:
            result = subprocess.run(shell_cmd, shell=True, capture_output=True,
                                     text=True, timeout=SHELL_TIMEOUT_S)
            out = ((result.stdout or "") + (result.stderr or "")).strip() or "(no output)"
            safe_edit(client, message.chat.id, msg.id, f"<pre>{fit_html(out)}</pre>")
        except subprocess.TimeoutExpired:
            safe_edit(client, message.chat.id, msg.id, "❌ Timed out.")
        except Exception as e:
            safe_edit(client, message.chat.id, msg.id, f"❌ <code>{esc(e)}</code>")

    threading.Thread(target=_run, daemon=True).start()


# Register the live-shell module's handlers onto this app. It owns bare
# "/sh" and "/shell"; anything after them still reaches _legacy_sh via
# handler ordering inside that module.
register_shell_handlers(app, AdminOnly, guarded, on_one_shot=_legacy_sh)

# /updateyt removed (user request): yt-dlp is no longer auto-updated or
# hot-swapped — a restart picks up whatever version the deploy installed.

