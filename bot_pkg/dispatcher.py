import os, time, threading
from concurrent.futures import ThreadPoolExecutor, as_completed

from . import state, uploader, drive, log
from .utils import throttled_edit, fix_unknown_ext, cancel_kb

logger = log.get(__name__)

SKIP_SUFFIXES = (".part", ".aria2", ".tmp", ".crdownload", ".ytdl", ".!qB")

class LiveDispatcher:
    """Watches task_dir for finished files and uploads each one as soon as
    it stops changing, instead of waiting for the whole job to finish."""

    def __init__(self, client, chat_id, msg_id, task_dir, dest, reply_to=None, task_id=None,
                 max_workers=2, rename=None, folder=None):
        self.client = client
        self.chat_id = chat_id
        self.msg_id = msg_id
        self.task_dir = task_dir
        self.dest = dest
        self.reply_to = reply_to
        self.task_id = task_id
        self.rename = rename
        self.folder = folder
        self.seen = set()
        self.sent = 0
        self.errors = []
        self._executor = ThreadPoolExecutor(max_workers=max_workers)
        self._futures = []
        self._stop = threading.Event()
        self._watcher = threading.Thread(target=self._watch, daemon=True)
        self._watcher.start()

    def _watch(self):
        while not self._stop.is_set():
            if self.task_id and state.is_cancelled(self.task_id):
                break
            self._scan()
            self._stop.wait(2)

    def _scan(self, force=False):
        now = time.time()
        if not os.path.isdir(self.task_dir):
            return
        for root, _, files in os.walk(self.task_dir):
            for f in files:
                if f.endswith(SKIP_SUFFIXES):
                    continue
                fp = os.path.join(root, f)
                if fp in self.seen:
                    continue
                try:
                    st = os.stat(fp)
                except FileNotFoundError:
                    continue
                # The 2s-untouched check exists to avoid grabbing a file
                # mid-write during periodic scans while the subprocess is
                # still running. finalize()'s last-chance scan runs *after*
                # the subprocess has already exited, so every file left on
                # disk is complete by definition — skipping the age check
                # there matters a lot for fast/small downloads (a single
                # photo, say) that finish in well under 2 seconds and would
                # otherwise be missed entirely, reported as "nothing
                # downloaded" despite the file sitting right there.
                if st.st_size > 0 and (force or now - st.st_mtime > 2):
                    self.seen.add(fp)
                    self.submit(fp)

    def submit(self, fpath):
        self._futures.append(self._executor.submit(self._upload_one, fpath))

    def _upload_one(self, fpath):
        try:
            if self.task_id and state.is_cancelled(self.task_id):
                return
            fpath = fix_unknown_ext(fpath)
            if self.dest in ("telegram", "both"):
                uploader.upload_to_telegram(self.client, self.chat_id, fpath, self.msg_id,
                                             reply_to=self.reply_to, task_id=self.task_id)
            if self.dest in ("drive", "both") and drive.enabled():
                # Drive uploads show their own progress on the task message
                from .utils import progress_line
                import time as _t
                _t0 = _t.time()
                last = [0.0]

                def _dprog(done, total, _n=[0]):
                    now = _t.time()
                    if now - last[0] < 1.5 and done != total:
                        return
                    last[0] = now
                    throttled_edit(self.client, self.chat_id, self.msg_id,
                                   f"☁️ <b>Uploading to Drive</b>\n"
                                   f"{progress_line('☁️', done, total, elapsed=now - _t0)}",
                                   markup=cancel_kb(self.task_id) if self.task_id else None,
                                   force=(done == total))
                link = drive.upload_file(fpath, task_id=self.task_id,
                                         rename=self.rename, folder=self.folder,
                                         chat_id=self.chat_id, progress_cb=_dprog)
                logger.info(f"uploaded to Drive: {fpath} -> {link}")
            self.sent += 1
        except state.CancelledError:
            logger.info(f"[{self.task_id}] dispatch upload cancelled")
        except Exception as e:
            logger.warning(f"dispatch upload failed for {fpath}: {e}")
            self.errors.append(f"{os.path.basename(fpath)}: {e}")
        finally:
            try:
                os.remove(fpath)
            except OSError:
                pass

    def stop(self):
        """Signal the watcher to stop without waiting for outstanding
        uploads — used on cancellation so we don't leak a background thread."""
        self._stop.set()

    def finalize(self, timeout=None):
        self._stop.set()
        self._watcher.join(timeout=5)
        self._scan(force=True)  # subprocess has already exited — every remaining file is complete
        for f in as_completed(self._futures, timeout=timeout):
            pass
        self._executor.shutdown(wait=True)
        return self.sent, self.errors
