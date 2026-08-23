import threading, time, os, sys

from pyrogram import Client
from pyrogram.enums import ParseMode

from . import config, state, log

# ── Crash guards — must run BEFORE anything imports tgcrypto/pyrogram ───
# 1) faulthandler: if native code segfaults again (exit 139), dump the C
#    and Python stack traces to a file we can actually read afterwards.
try:
    import faulthandler
    os.makedirs(config.LOG_DIR, exist_ok=True)
    _fault_log = open(os.path.join(config.LOG_DIR, "crash.log"), "a", buffering=1)
    faulthandler.enable(file=_fault_log, all_threads=True)
except Exception:
    pass

# 2) TgCrypto guard: self-test the C extension in a subprocess; block it if
#    it's the segfaulting kind (pure-Python fallback keeps the bot alive).
from . import crypto_guard
crypto_guard.install()

log.setup()
logger = log.get(__name__)

def _acquire_single_instance_lock():
    """Refuses to start if another instance of this bot is already running
    on the same data volume — the classic cause of every command getting
    answered twice (a redeploy that didn't cleanly stop the old process,
    two containers sharing one BOT_TOKEN, etc.). An in-process dedup guard
    can't catch this since each process has its own memory; this uses a
    real OS-level file lock on data/, so it also works across containers
    that mount the same volume."""
    lock_path = os.path.join(config.DATA_DIR, "azleechbot.lock")
    try:
        import fcntl
        fh = open(lock_path, "w")
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fh.write(str(os.getpid()))
        fh.flush()
        return fh  # keep a reference for the life of the process — closing/GC releases the lock
    except ImportError:
        logger.warning("fcntl not available (non-POSIX host?) — skipping single-instance lock")
        return None
    except OSError:
        logger.error(
            f"Another instance already holds {lock_path} — refusing to start. "
            "This is almost always why commands get answered twice: two bot "
            "processes polling the same BOT_TOKEN. Stop the other one first."
        )
        print(f"CRITICAL: another instance is already running (lock: {lock_path}). Exiting.", file=sys.stderr)
        sys.exit(1)

_lock_fh = None  # kept alive for the process lifetime — see _acquire_single_instance_lock

app = Client(
    "azleechbot",
    api_id=config.API_ID,
    api_hash=config.API_HASH,
    bot_token=config.BOT_TOKEN,
    workdir=config.DATA_DIR,
    # Global HTML parse mode (the WZML-X approach). Markdown's delimiter
    # auto-balancing produced entities that overrun the message length
    # (Telegram error 400 ENTITY_BOUNDS_INVALID) whenever dynamic content —
    # filenames, log tails, URLs with backticks/brackets — shifted a
    # delimiter. HTML has no auto-balancing: what you escape is what gets
    # parsed, so entity bounds always match the text.
    parse_mode=ParseMode.HTML,
    # Default is 1 — meaning file parts upload/download serially over a
    # single connection. Raising this lets Pyrogram push multiple parts of
    # the same file in parallel, which is the single biggest lever for
    # large-file Telegram upload/download speed.
    max_concurrent_transmissions=8,
)

def _bg_worker(worker_id):
    logger.debug(f"worker-{worker_id} started")
    while True:
        fn, args = state.task_queue.get()
        try:
            logger.debug(f"worker-{worker_id} picked up {fn.__name__}")
            fn(*args)
        except Exception:
            logger.exception(f"worker-{worker_id} job {fn.__name__} raised")
        finally:
            state.task_queue.task_done()

def _start_workers():
    for i in range(config.QUEUE_WORKERS):
        threading.Thread(target=_bg_worker, args=(i,), daemon=True).start()
    logger.info(f"started {config.QUEUE_WORKERS} queue workers")

def _purge_loop():
    while True:
        before = state.disk_free_mb()
        state.purge_stale()
        after = state.disk_free_mb()
        if after != before:
            logger.info(f"purged stale downloads, freed {after - before} MB")
        time.sleep(600)

def _yt_dlp_update_loop():
    from . import engine
    while True:
        time.sleep(6 * 3600)  # every 6 hours
        with state.jobs_lock:
            idle = not state.ACTIVE_JOBS
        # Only restart when nothing's actively running — updating the pip
        # package is always safe, but the restart needed to actually load
        # it isn't something to do mid-job.
        engine.update_yt_dlp(restart_on_update=idle)

def start():
    global _lock_fh
    _lock_fh = _acquire_single_instance_lock()
    from . import engine
    engine.update_yt_dlp(restart_on_update=True)  # safe here — nothing is serving yet
    # Restore EVERYTHING user-configured from the Drive datastore on a fresh
    # deploy: settings, users (access control), cookies, OAuth tokens.
    try:
        from . import datastore
        pulled = datastore.restore_all_local()
        if pulled:
            logger.info(f"datastore: restored {', '.join(pulled)}")
        import importlib
        importlib.reload(state)
    except Exception as e:
        logger.warning(f"datastore settings restore skipped: {e}")
    # Telegram-registered Drive accounts (overrides.json) over env ones.
    try:
        from . import handlers_driveauth
        handlers_driveauth._load_overrides_into_config()
    except Exception as e:
        logger.warning(f"drive overrides load skipped: {e}")
    from . import (handlers_core, handlers_admin, handlers_shell, handlers_subproc,
                   handlers_instagram, handlers_drive, handlers_zip,
                   handlers_driveauth, handlers_fallback)  # noqa: F401
    logger.info("handlers registered: core, admin+shell, subproc, instagram+zip, drive+auth, fallback")
    _start_workers()
    threading.Thread(target=_purge_loop, daemon=True).start()
    threading.Thread(target=_yt_dlp_update_loop, daemon=True).start()
    logger.info("AzLeechBot starting (Pyrogram, native >50MB uploads)…")
    app.run()
