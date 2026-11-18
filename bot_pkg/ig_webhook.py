"""Webhook receiver for Instagram Messaging — stdlib only, no new deps.

Meta POSTs DM events here; each one is handed to ig_dm.handle_event. Two
things matter and nothing else does:

  * verification (GET with hub.mode/hub.verify_token/hub.challenge) must echo
    the challenge back or the App Dashboard refuses to save the callback URL;
  * the POST must be answered with 200 in under 5 seconds or Meta retries
    (and duplicate deliveries would download the same reel twice), so the
    work is pushed to a thread and the response goes out immediately.

Payloads are verified with X-Hub-Signature-256 (HMAC-SHA256 of the raw body
keyed by the app secret). Without that check anyone who guesses the URL could
make the bot fetch arbitrary links, so it is mandatory — a request with a
missing or wrong signature is rejected, not processed.

The listener is a plain ThreadingHTTPServer on its own port; TLS and the
public hostname are the reverse proxy's job (Caddy/nginx/Cloudflare Tunnel).
Meta requires HTTPS with a valid certificate, self-signed is refused.
"""
import hashlib
import hmac
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config, log

logger = log.get(__name__)

_server = None


def _valid_signature(raw, header):
    if not header or not header.startswith("sha256="):
        return False
    want = hmac.new(config.IG_APP_SECRET.encode(), raw, hashlib.sha256).hexdigest()
    return hmac.compare_digest(want, header.split("=", 1)[1].strip())


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, format, *args):   # noqa: A002 — signature is stdlib's
        logger.debug("ig webhook: " + format % args)

    def _plain(self, code, body=b"", ctype="text/plain"):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def do_GET(self):
        if self.path.split("?")[0] != config.IG_WEBHOOK_PATH:
            return self._plain(404)
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(self.path).query)
        if (q.get("hub.mode", [""])[0] == "subscribe"
                and q.get("hub.verify_token", [""])[0] == config.IG_VERIFY_TOKEN):
            logger.info("ig webhook: verified by Meta")
            return self._plain(200, q.get("hub.challenge", [""])[0].encode())
        logger.warning("ig webhook: verification rejected (token mismatch)")
        self._plain(403)

    def do_POST(self):
        if self.path.split("?")[0] != config.IG_WEBHOOK_PATH:
            return self._plain(404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return self._plain(400)
        raw = self.rfile.read(n) if n > 0 else b""

        if not _valid_signature(raw, self.headers.get("X-Hub-Signature-256")):
            logger.warning("ig webhook: bad or missing signature — rejected")
            return self._plain(403)

        try:
            body = json.loads(raw or b"{}")
        except Exception:
            return self._plain(400)

        # 200 first, work later: Meta gives 5 seconds and retries on timeout.
        self._plain(200, b"EVENT_RECEIVED")
        if body.get("object") != "instagram":
            return
        for entry in body.get("entry") or []:
            for ev in entry.get("messaging") or []:
                threading.Thread(target=_process, args=(ev,), daemon=True).start()

    def do_HEAD(self):
        self._plain(200)


def _process(ev):
    from . import ig_dm
    try:
        logger.info(f"ig webhook: {ig_dm.handle_event(ev)}")
    except Exception:
        logger.exception("ig webhook: handler crashed")


def start():
    """Start the listener when configured. Returns None when disabled."""
    global _server
    if _server is not None:
        return _server
    if not config.IG_WEBHOOK_PORT:
        return None
    missing = [n for n, v in (("IG_APP_SECRET", config.IG_APP_SECRET),
                              ("IG_PAGE_TOKEN", config.IG_PAGE_TOKEN),
                              ("IG_ACCOUNT_ID", config.IG_ACCOUNT_ID),
                              ("IG_VERIFY_TOKEN", config.IG_VERIFY_TOKEN)) if not v]
    if missing:
        logger.warning(f"ig webhook: not started, missing {', '.join(missing)}")
        return None
    _server = ThreadingHTTPServer(("0.0.0.0", config.IG_WEBHOOK_PORT), Handler)
    _server.daemon_threads = True
    threading.Thread(target=_server.serve_forever, daemon=True, name="ig-webhook").start()
    logger.info(f"ig webhook: listening on 0.0.0.0:{config.IG_WEBHOOK_PORT}"
                f"{config.IG_WEBHOOK_PATH} — put HTTPS in front of it")
    return _server
