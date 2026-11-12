"""A webhook receiver: verify the signature, answer 204 at once, then poll.

MAKIA's delivery only says "something happened up to `seq`"; the feed
(`GET /changes`) stays the source of truth, so the receiver never reads the
body. Deliveries may repeat and arrive in any order, and any number of them
during one poll cost exactly one more poll, never a second poll beside it
(both would read and store the same feed marker).
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

log = logging.getLogger("makia_integration")

MAX_AGE_S = 300  # a timestamp older than five minutes is refused (replay)
MAX_BODY = 1_000_000


def verify(
    secret: str, timestamp: str, body: bytes, signature: str, *, now: float | None = None
) -> bool:
    """`X-Makia-Signature` is `sha256=<hex HMAC-SHA256(secret, "<timestamp>.<body>")>`."""
    try:
        age = (time.time() if now is None else now) - int(timestamp)
    except ValueError:
        return False
    if abs(age) > MAX_AGE_S:
        return False
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return hmac.compare_digest(
        ("sha256=" + mac.hexdigest()).encode(), signature.encode("utf-8", "replace")
    )


class PollCoalescer:
    """One worker thread runs `run`. `kick()` while it runs sets a dirty flag:
    however many kicks, exactly one more run follows."""

    def __init__(self, run: Callable[[], None]) -> None:
        self._run = run
        self._wake = threading.Event()
        self._closed = False
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def kick(self) -> None:
        self._wake.set()

    def close(self) -> None:
        self._closed = True
        self._wake.set()
        self._thread.join(timeout=5)

    def _loop(self) -> None:
        while True:
            self._wake.wait()
            if self._closed:
                return
            self._wake.clear()  # kicks from here on mean "once more"
            try:
                self._run()
            except Exception:
                log.exception("poll failed")


def make_server(coalescer: PollCoalescer, secret: str, port: int) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            try:
                length = int(self.headers.get("content-length") or 0)
            except ValueError:
                return self._answer(400)
            if length < 0:
                return self._answer(400)
            if length > MAX_BODY:
                return self._answer(413)
            body = self.rfile.read(length)
            ok = verify(
                secret,
                self.headers.get("x-makia-timestamp", ""),
                body,
                self.headers.get("x-makia-signature", ""),
            )
            if not ok:
                return self._answer(401)
            # Dedupe on X-Makia-Delivery is unnecessary here: a repeat only
            # costs a poll that finds nothing new.
            self._answer(204)
            coalescer.kick()

        def _answer(self, status: int) -> None:
            self.send_response(status)
            self.send_header("content-length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: Any) -> None:
            log.debug(format, *args)

    return ThreadingHTTPServer(("0.0.0.0", port), Handler)


def serve(integration: Any, secret: str, port: int) -> None:
    """Listen until interrupted. Polls once at start (deliveries missed while
    the receiver was down are in the feed)."""
    coalescer = PollCoalescer(integration.poll)
    coalescer.kick()
    server = make_server(coalescer, secret, port)
    log.info("listening on :%d", server.server_port)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        coalescer.close()
