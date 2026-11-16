"""The loop behind `run` and `listen`: one worker thread does every job.

A timer and/or verified webhook deliveries only *kick* the worker; the integration
(and its SQLite state) is built and used on that one thread.

A round runs sync (when due), send and poll; a failing step is logged in one line
and the round goes on with the next step. After a round with a failing step the
integration is closed and the next round builds a new one, so a dropped connection
inside the integration recovers.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from http.server import ThreadingHTTPServer

from makia_integration.integration import Integration
from makia_integration.receiver import PollCoalescer, make_server

log = logging.getLogger("makia_integration")


class Runner:
    def __init__(
        self,
        factory: Callable[[], Integration],
        *,
        interval: float | None,
        listen_port: int | None,
        webhook_secret: str | None,
        wait: Callable[[float], bool] | None = None,
        sync_every: int = 1,
        describe: Callable[[BaseException], str] | None = None,
    ) -> None:
        self._factory = factory
        self._sync_every = max(1, sync_every)
        self._rounds = 0  # rounds that ran; one whose due sync was not attempted is not counted
        self._describe = describe or (lambda err: f"{type(err).__name__}: {err}")
        self._interval = interval
        self._listen_port = listen_port
        self._secret = webhook_secret
        self._stop = threading.Event()
        self._wait = wait or self._stop.wait
        self._integration: Integration | None = None
        self._coalescer: PollCoalescer | None = None
        self._server: ThreadingHTTPServer | None = None
        self._threads: list[threading.Thread] = []
        self.last_error: BaseException | None = None

    @property
    def server_port(self) -> int | None:
        return self._server.server_port if self._server else None

    def start(self) -> None:
        if self._listen_port is not None and not self._secret:
            raise ValueError("listening for webhooks needs a webhook secret")
        self._coalescer = PollCoalescer(self._round)
        self._coalescer.kick()
        if self._interval is not None:
            timer = threading.Thread(target=self._timer, daemon=True)
            timer.start()
            self._threads.append(timer)
        if self._listen_port is not None:
            assert self._secret is not None
            self._server = make_server(self._coalescer, self._secret, self._listen_port)
            log.info("listening on :%d", self._server.server_port)
            srv = threading.Thread(target=self._server.serve_forever, daemon=True)
            srv.start()
            self._threads.append(srv)
            if self._stop.is_set():  # stop() ran before the server existed
                self._shutdown_server()

    def stop(self) -> None:
        """Safe from a signal handler: sets flags only, never joins."""
        self._stop.set()
        if self._coalescer is not None:
            self._coalescer.request_close()
        self._shutdown_server()

    def _shutdown_server(self) -> None:
        server = self._server
        if server is not None:
            # shutdown() blocks until serve_forever exits; run it off-thread
            threading.Thread(target=server.shutdown, daemon=True).start()

    def join(self, timeout: float | None = None) -> None:
        """Wait for the threads in short slices so Ctrl+C can land on Windows."""
        end = None if timeout is None else time.monotonic() + timeout
        while True:
            alive: list[threading.Thread | PollCoalescer] = [
                t for t in self._threads if t.is_alive()
            ]
            if self._coalescer is not None and self._coalescer.is_alive():
                alive.append(self._coalescer)
            if not alive:
                break
            left = 0.5 if end is None else min(0.5, end - time.monotonic())
            if left <= 0:
                return
            alive[0].join(left)
        if self._server is not None and self._stop.is_set():
            self._server.server_close()

    def _timer(self) -> None:
        assert self._interval is not None and self._coalescer is not None
        while not self._wait(self._interval):
            self._coalescer.kick()

    def _fail(self, what: str, err: Exception) -> None:
        self.last_error = err
        log.error(
            "%s failed: %s",
            what,
            self._describe(err),
            exc_info=err if log.isEnabledFor(logging.DEBUG) else None,
        )

    def _round(self) -> None:
        if self._integration is None:
            try:
                self._integration = self._factory()
            except Exception as err:
                self._fail("integration", err)
                return
        integration = self._integration
        sync_due = self._rounds % self._sync_every == 0
        steps: list[tuple[str, Callable[[], object]]] = [
            ("send", integration.send),
            ("poll", integration.poll),
        ]
        if sync_due:
            steps.insert(0, ("sync", integration.sync))
        failed = cut = sync_tried = False
        for name, step in steps:
            if self._stop.is_set():
                cut = True
                break
            sync_tried = sync_tried or name == "sync"
            try:
                step()
            except Exception as err:
                failed = True
                self._fail(name, err)
        if sync_tried or not sync_due:
            self._rounds += 1
        if failed:
            # A failure may have left a connection inside the integration broken: build a new
            # one next round (the factory opens the state file again, so close it first).
            self._integration = None
            close = getattr(integration, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    log.debug("closing the integration failed", exc_info=True)
        elif not cut:
            self.last_error = None
