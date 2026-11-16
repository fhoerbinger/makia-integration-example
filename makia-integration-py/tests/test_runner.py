from __future__ import annotations

import hashlib
import hmac
import threading
import time
import urllib.request

from makia_integration.runner import Runner
from makia_integration.state import SqliteStateStore


class StubIntegration:
    def __init__(self, log: list[str] | None = None) -> None:
        self.log = log if log is not None else []
        self.polls = 0
        self.poll_thread: int | None = None
        self.on_step = None

    def _step(self, name: str) -> None:
        self.log.append(name)
        if self.on_step:
            self.on_step(name)

    def sync(self):
        self._step("sync")

    def send(self):
        self._step("send")

    def poll(self):
        self.polls += 1
        self.poll_thread = threading.get_ident()
        self._step("poll")

    def close(self):
        self.log.append("close")


def test_factory_called_on_worker_thread():
    idents: list[int] = []
    integration = StubIntegration()
    done = threading.Event()
    integration.on_step = lambda n: done.set() if n == "poll" else None

    def factory():
        idents.append(threading.get_ident())
        return integration

    runner = Runner(factory, interval=None, listen_port=None, webhook_secret=None)
    runner.start()
    assert done.wait(2)
    runner.stop()
    runner.join(timeout=2)
    assert idents == [integration.poll_thread]
    assert idents[0] != threading.get_ident()


def test_timer_kicks_runs():
    integration = StubIntegration()
    round_done = threading.Event()
    done3 = threading.Event()
    waits: list[float] = []

    def on_step(name):
        if name == "poll":
            round_done.set()
            if integration.polls == 3:
                done3.set()

    integration.on_step = on_step

    def wait(interval):
        waits.append(interval)
        round_done.wait(2)  # the previous round finishes before the next kick
        round_done.clear()
        return len(waits) > 2

    runner = Runner(
        lambda: integration, interval=7, listen_port=None, webhook_secret=None, wait=wait
    )
    runner.start()
    assert done3.wait(2)
    runner.stop()
    runner.join(timeout=2)
    assert integration.polls == 3
    assert waits[0] == 7


def test_stop_ends_after_current_step():
    integration = StubIntegration()
    runner_box: list[Runner] = []
    integration.on_step = lambda n: runner_box[0].stop() if n == "sync" else None
    runner = Runner(lambda: integration, interval=None, listen_port=None, webhook_secret=None)
    runner_box.append(runner)
    runner.start()
    runner.join(timeout=2)
    assert integration.log == ["sync"]


def test_job_exception_logged_loop_continues():
    integration = StubIntegration()
    second = threading.Event()
    failed = threading.Event()
    calls = {"n": 0}

    def sync():
        calls["n"] += 1
        if calls["n"] == 1:
            failed.set()
            raise RuntimeError("boom")
        second.set()

    integration.sync = sync  # type: ignore[method-assign]
    runner = Runner(lambda: integration, interval=None, listen_port=None, webhook_secret=None)
    runner.start()
    assert failed.wait(2)
    assert _until(lambda: isinstance(runner.last_error, RuntimeError))
    runner._coalescer.kick()
    assert second.wait(2)
    assert _until(lambda: runner.last_error is None and integration.polls >= 1)
    runner.stop()
    runner.join(timeout=2)


def test_factory_failure_retried_on_next_kick():
    integration = StubIntegration()
    attempts = []
    ok = threading.Event()
    integration.on_step = lambda n: ok.set() if n == "poll" else None

    def factory():
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("no db")
        return integration

    runner = Runner(factory, interval=None, listen_port=None, webhook_secret=None)
    runner.start()
    assert _until(lambda: len(attempts) == 1 and runner.last_error is not None)
    runner._coalescer.kick()
    assert ok.wait(2)
    runner.stop()
    runner.join(timeout=2)
    assert len(attempts) == 2


def test_listen_requires_secret():
    import pytest

    runner = Runner(StubIntegration, interval=None, listen_port=0, webhook_secret=None)
    with pytest.raises(ValueError):
        runner.start()


def _until(cond, timeout=2.0):
    # bounded event-style poll, no fixed sleeps
    ev = threading.Event()
    end = timeout / 0.005
    n = 0
    while n < end:
        if cond():
            return True
        ev.wait(0.005)
        n += 1
    return False


def test_listen_delivery_runs_round_with_real_state(tmp_path):
    polls = threading.Semaphore(0)
    seen: dict = {"threads": set(), "stores": []}

    class B(StubIntegration):
        def __init__(self) -> None:
            super().__init__()
            self.store = SqliteStateStore(tmp_path / "state.db")
            seen["stores"].append(self.store)

        def poll(self):
            seen["threads"].add(threading.get_ident())
            super().poll()
            self.store.set_marker(self.polls)
            if self.polls == 2:
                self.store.close()  # same thread that opened it
            polls.release()

    runner = Runner(B, interval=None, listen_port=0, webhook_secret="s")
    runner.start()
    assert polls.acquire(timeout=2)  # the immediate first round
    body, ts = b"{}", str(int(time.time()))
    mac = hmac.new(b"s", ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(
        f"http://127.0.0.1:{runner.server_port}/",
        data=body,
        headers={"X-Makia-Timestamp": ts, "X-Makia-Signature": "sha256=" + mac},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        assert resp.status == 204
    assert polls.acquire(timeout=2)  # the delivery's round
    runner.stop()
    runner.join(timeout=2)
    assert not any(t.is_alive() for t in runner._threads)
    assert not runner._coalescer._thread.is_alive()
    assert len(seen["threads"]) == 1 and threading.get_ident() not in seen["threads"]
    reopened = SqliteStateStore(tmp_path / "state.db")
    assert reopened.marker() == 2
    reopened.close()


def test_stop_cut_round_keeps_last_error():
    integration = StubIntegration()
    runner_box: list[Runner] = []
    integration.on_step = lambda n: runner_box[0].stop() if n == "sync" else None
    runner = Runner(lambda: integration, interval=None, listen_port=None, webhook_secret=None)
    runner_box.append(runner)
    runner.last_error = RuntimeError("old")
    runner.start()
    runner.join(timeout=2)
    assert isinstance(runner.last_error, RuntimeError)


def test_sync_every_syncs_first_round_and_every_nth():
    integration = StubIntegration()
    polls = threading.Semaphore(0)
    integration.on_step = lambda n: polls.release() if n == "poll" else None
    runner = Runner(
        lambda: integration, interval=None, listen_port=None, webhook_secret=None, sync_every=3
    )
    runner.start()
    assert polls.acquire(timeout=2)
    for _ in range(3):
        runner._coalescer.kick()
        assert polls.acquire(timeout=2)
    runner.stop()
    runner.join(timeout=2)
    rounds = " ".join(integration.log).split(" poll")
    assert [r.split() for r in rounds[:4]] == [
        ["sync", "send"],
        ["send"],
        ["send"],
        ["sync", "send"],
    ]


def test_a_failing_step_does_not_stop_the_others(caplog):
    integration = StubIntegration()
    polls = threading.Semaphore(0)
    integration.on_step = lambda n: polls.release() if n == "poll" else None

    def sync():
        integration.log.append("sync")
        raise RuntimeError("db down")

    integration.sync = sync  # type: ignore[method-assign]
    runner = Runner(
        lambda: integration, interval=None, listen_port=None, webhook_secret=None, sync_every=2
    )
    with caplog.at_level("ERROR", logger="makia_integration"):
        runner.start()
        assert polls.acquire(timeout=2)
        runner._coalescer.kick()
        assert polls.acquire(timeout=2)
        runner.stop()
        runner.join(timeout=2)
    # sync failed, send and poll still ran; the failed sync counts: round 2 has none
    assert integration.log == ["sync", "send", "poll", "close", "send", "poll"]
    assert "sync failed: RuntimeError: db down" in caplog.text
    assert runner.last_error is None  # the second round was clean


def test_last_error_names_the_failing_step_and_is_kept(caplog):
    integration = StubIntegration()
    done = threading.Event()

    def poll():
        done.set()
        raise ValueError("bad row")

    integration.poll = poll  # type: ignore[method-assign]
    runner = Runner(lambda: integration, interval=None, listen_port=None, webhook_secret=None)
    with caplog.at_level("ERROR", logger="makia_integration"):
        runner.start()
        assert done.wait(2)
        assert _until(lambda: runner.last_error is not None)
        runner.stop()
        runner.join(timeout=2)
    assert isinstance(runner.last_error, ValueError)
    assert "poll failed: ValueError: bad row" in caplog.text
    assert "Traceback" not in caplog.text


def test_integration_rebuilt_after_a_failed_round(tmp_path):
    built: list[object] = []
    second = threading.Event()

    class B(StubIntegration):
        def __init__(self) -> None:
            super().__init__()
            self.state = SqliteStateStore(tmp_path / "state.db")  # StateLocked if not released

        def send(self):
            if len(built) == 1:
                raise OSError("connection dropped")
            second.set()

        def close(self):
            self.state.close()

    def factory():
        b = B()
        built.append(b)
        return b

    runner = Runner(factory, interval=None, listen_port=None, webhook_secret=None)
    runner.start()
    assert _until(lambda: runner.last_error is not None)
    runner._coalescer.kick()
    assert second.wait(2)
    assert _until(lambda: runner.last_error is None)
    runner.stop()
    runner.join(timeout=2)
    assert len(built) == 2


def test_describe_shapes_the_log_line(caplog):
    integration = StubIntegration()
    done = threading.Event()

    def send():
        done.set()
        raise RuntimeError("raw")

    integration.send = send  # type: ignore[method-assign]
    runner = Runner(
        lambda: integration,
        interval=None,
        listen_port=None,
        webhook_secret=None,
        describe=lambda err: "explained",
    )
    with caplog.at_level("ERROR", logger="makia_integration"):
        runner.start()
        assert done.wait(2)
        assert _until(lambda: "send failed: explained" in caplog.text)
        runner.stop()
        runner.join(timeout=2)


def test_a_sync_refusal_is_logged_but_does_not_rebuild(caplog):
    from makia_integration.integration import SyncError, SyncReport

    built: list[StubIntegration] = []
    polls = threading.Semaphore(0)

    def factory():
        integration = StubIntegration()
        integration.on_step = lambda n: polls.release() if n == "poll" else None

        def sync():
            integration.log.append("sync")
            raise SyncError(["p1"], ["p1 (409 slug_taken)"], SyncReport(2, 0))

        integration.sync = sync  # type: ignore[method-assign]
        built.append(integration)
        return integration

    runner = Runner(factory, interval=None, listen_port=None, webhook_secret=None)
    with caplog.at_level("ERROR", logger="makia_integration"):
        runner.start()
        assert polls.acquire(timeout=2)
        runner._coalescer.kick()
        assert polls.acquire(timeout=2)
        assert _until(lambda: len(built[0].log) == 6)
        runner.stop()
        runner.join(timeout=2)
    assert len(built) == 1  # the same integration, never closed
    assert built[0].log == ["sync", "send", "poll", "sync", "send", "poll"]
    assert "sync failed: SyncError: 1 of 2 projects refused" in caplog.text
    assert isinstance(runner.last_error, SyncError)  # kept: the refusal lasts
