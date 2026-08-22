import os, time, shutil, subprocess, threading

from pyrogram import filters

from . import config, state, drive, log
from .core import app
from .utils import fmt_time, guarded, safe_edit, esc
from .handlers_core import Authorized, AdminOnly
from .handlers_shell import register_shell_handlers, SHELL_COMMAND_NAMES


@app.on_message(filters.command("stats") & Authorized)
@guarded
def cmd_stats(client, message):
    free = state.disk_free_mb()
    total = shutil.disk_usage(config.DOWNLOAD_DIR).total // (1024 * 1024)
    with state.jobs_lock:
        jobs = dict(state.ACTIVE_JOBS)
    lines = [
        f"  🔴 <code>{esc(tid)}</code> — {j.get('kind', '?')} ({fmt_time(int(time.time() - j.get('start', time.time())))})"
        for tid, j in jobs.items()
    ]
    message.reply_text(
        f"📊 <b>Stats</b>\n\n"
        f"💾 Disk: <code>{free} MB</code> free / <code>{total} MB</code>\n"
        f"⚙️ Active: <code>{len(jobs)}</code> | Queued: <code>{state.task_queue.qsize()}</code>\n"
        f"☁️ Drive: {'enabled' if drive.enabled() else 'not configured'}\n\n"
        f"<b>Active jobs:</b>\n" + ("\n".join(lines) if lines else "  <i>none</i>"),
    )


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
            if len(out) > 3800:
                out = out[-3800:]
            safe_edit(client, message.chat.id, msg.id, f"<pre>{esc(out)}</pre>")
        except subprocess.TimeoutExpired:
            safe_edit(client, message.chat.id, msg.id, "❌ Timed out.")
        except Exception as e:
            safe_edit(client, message.chat.id, msg.id, f"❌ <code>{esc(e)}</code>")

    threading.Thread(target=_run, daemon=True).start()


# Register the live-shell module's handlers onto this app. It owns bare
# "/sh" and "/shell"; anything after them still reaches _legacy_sh via
# handler ordering inside that module.
register_shell_handlers(app, AdminOnly, guarded, on_one_shot=_legacy_sh)


@app.on_message(filters.command(["updateyt", "ytupdate"]) & AdminOnly)
@guarded
def cmd_update_ytdlp(client, message):
    from . import engine, state as state_mod
    msg = message.reply_text("🔄 Checking for a yt-dlp update…")
    with state_mod.jobs_lock:
        idle = not state_mod.ACTIVE_JOBS
    if not idle:
        safe_edit(client, message.chat.id, msg.id,
                  "⚠️ Jobs are currently running — updating the package now, but the "
                  "restart needed to actually load it will happen automatically once "
                  "everything's idle (or run <code>/updateyt</code> again later).")
    before, after = engine.update_yt_dlp(restart_on_update=idle)
    if before == after:
        safe_edit(client, message.chat.id, msg.id, f"✅ Already up to date — yt-dlp <code>{after}</code>.")
    elif idle:
        # Process re-execs itself on update — this message likely won't
        # even get a chance to send before the restart happens.
        pass
    else:
        safe_edit(client, message.chat.id, msg.id,
                  f"📦 Updated yt-dlp <code>{before}</code> → <code>{after}</code> (installed; restart pending until idle).")
