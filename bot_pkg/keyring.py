"""Shared multi-key rotation for upload hosts.

Both /px (pixeldrain) and /ua (upload.am) accept several credentials as a comma
or newline separated env list and rotate when one account can't take a file.

Per-key state persists in data/<name>_keys.json: `full` keys are skipped for a
cooldown (their quota may free up), `dead` keys never come back (revoked
token). Only a hash of each key is stored — the token itself never lands on disk.

Error codes are per-service policy, so they are passed in rather than hardcoded:
`rotate` means another account may well succeed, `dead` means this credential
is permanently unusable.
"""
import hashlib
import json
import os
import threading
import time

from . import config, log

logger = log.get(__name__)

_lock = threading.Lock()


class Keyring:
    def __init__(self, name, env_var, rotate=(), dead=(), cooldown_s=6 * 3600, extra_env=()):
        self.name = name
        self.env_var = env_var
        self.extra_env = tuple(extra_env)
        self.rotate = set(rotate)
        self.dead = set(dead)
        self.cooldown_s = cooldown_s
        self.file = os.path.join(config.DATA_DIR, f"{name}_keys.json")

    # ── credentials ────────────────────────────────────────────────────────
    def keys(self):
        """Comma/newline separated list from env_var, then any extra vars.

        The singular spellings (`PIXELDRAIN_API_KEY`) are accepted so an older
        single-key config keeps working.
        """
        out = []
        for var in (self.env_var, *self.extra_env):
            raw = os.environ.get(var) or ""
            out += [k.strip() for k in raw.replace("\n", ",").split(",") if k.strip()]
        return list(dict.fromkeys(out))

    def configured(self):
        return bool(self.keys())

    @staticmethod
    def fp(key):
        return hashlib.sha256(key.encode()).hexdigest()[:12]

    # ── persisted state ────────────────────────────────────────────────────
    def load(self):
        try:
            with open(self.file) as f:
                d = json.load(f)
                return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save(self, d):
        try:
            tmp = self.file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(d, f)
            os.replace(tmp, self.file)
        except Exception as e:
            logger.warning(f"{self.name}: couldn't save key state: {e}")

    def _usable(self, keys, st, now):
        out = []
        for k in keys:
            e = st.get(self.fp(k), {})
            if e.get("state") == "dead":
                continue
            if e.get("state") == "full" and e.get("until", 0) > now:
                continue
            out.append(k)
        return out

    def order(self):
        """Keys to try, last-known-good first.

        When every non-dead key is cooling down they are still returned: a
        refilled quota would otherwise sit unused for the whole window.
        """
        keys = self.keys()
        st = self.load()
        usable = self._usable(keys, st, time.time())
        if not usable:
            usable = [k for k in keys if st.get(self.fp(k), {}).get("state") != "dead"]
        last = st.get("_last_good")
        if isinstance(last, str) and last in usable:
            usable.remove(last)
            usable.insert(0, last)
        return usable

    def mark_full(self, key, code):
        with _lock:
            st = self.load()
            st[self.fp(key)] = {"state": "full", "until": time.time() + self.cooldown_s,
                                "code": code}
            self._save(st)
        logger.warning(f"{self.name}: key {self.fp(key)} -> {code}, rotating")

    def mark_dead(self, key, code):
        with _lock:
            st = self.load()
            st[self.fp(key)] = {"state": "dead", "code": code}
            self._save(st)
        logger.error(f"{self.name}: key {self.fp(key)} -> {code}, dropped")

    def mark_good(self, key):
        with _lock:
            st = self.load()
            st.pop(self.fp(key), None)
            st["_last_good"] = key
            self._save(st)

    def status(self):
        """{fingerprint: state} — for /ua status and debugging."""
        return {k: v for k, v in self.load().items() if k != "_last_good"}

    def classify(self, code):
        """'dead' | 'rotate' | None for a service error code."""
        if code in self.dead:
            return "dead"
        if code in self.rotate:
            return "rotate"
        return None
