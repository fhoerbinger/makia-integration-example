from __future__ import annotations

import hashlib
import hmac
import threading
import time
import urllib.error
import urllib.request

from makia_integration.receiver import PollCoalescer, make_server, verify

SECRET = "whsec_test"


def sign(timestamp: str, body: bytes, secret: str = SECRET) -> str:
    mac = hmac.new(secret.encode(), timestamp.encode() + b"." + body, hashlib.sha256)
    return "sha256=" + mac.hexdigest()


def test_good_signature_accepted():
    ts, body = str(int(time.time())), b'{"seq":1}'
    assert verify(SECRET, ts, body, sign(ts, body))


def test_tampered_body_refused():
    ts, body = str(int(time.time())), b'{"seq":1}'
    assert not verify(SECRET, ts, b'{"seq":2}', sign(ts, body))
    assert not verify("other", ts, body, sign(ts, body))
    assert not verify(SECRET, ts, body, "sha256=nothex")


def test_old_timestamp_refused():
    ts, body = str(int(time.time()) - 600), b"{}"
    assert not verify(SECRET, ts, body, sign(ts, body))
    assert not verify(SECRET, "not-a-number", body, sign("not-a-number", body))


class FakeIntegration:
    def __init__(self) -> None:
        self.polls = 0
        self.gate: threading.Event | None = None
        self.started = threading.Event()

    def poll(self) -> None:
        self.polls += 1
        self.started.set()
        if self.gate is not None and self.polls == 1:
            self.gate.wait(30)  # held until the test opens it, also on a slow machine


def post(port: int, body: bytes, *, signature: str | None = None, ts: str | None = None) -> int:
    ts = ts or str(int(time.time()))
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/",
        data=body,
        headers={
            "X-Makia-Timestamp": ts,
            "X-Makia-Signature": signature or sign(ts, body),
            "X-Makia-Delivery": "d1",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as err:
        return err.code


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.01)
    return False


def start(integration: FakeIntegration):
    coalescer = PollCoalescer(integration.poll)
    server = make_server(coalescer, SECRET, 0)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, coalescer


def test_delivery_triggers_poll():
    integration = FakeIntegration()
    server, coalescer = start(integration)
    try:
        assert post(server.server_port, b'{"seq":1}') == 204
        assert wait_for(lambda: integration.polls == 1)
        assert post(server.server_port, b"{}", signature="sha256=00") == 401
        assert post(server.server_port, b"{}", ts=str(int(time.time()) - 900)) == 401
        time.sleep(0.1)
        assert integration.polls == 1
    finally:
        server.shutdown()
        coalescer.close()


def test_three_deliveries_during_a_poll_run_one_more_poll():
    integration = FakeIntegration()
    integration.gate = threading.Event()
    server, coalescer = start(integration)
    try:
        assert post(server.server_port, b"{}") == 204
        assert integration.started.wait(5)  # first poll is running, held open
        for _ in range(3):
            assert post(server.server_port, b"{}") == 204  # answered at once
        integration.gate.set()
        assert wait_for(lambda: integration.polls >= 2), integration.polls
        time.sleep(0.2)
        assert integration.polls == 2
    finally:
        server.shutdown()
        coalescer.close()


def test_far_future_and_non_ascii_signature_refused():
    body = b"{}"
    future = str(int(time.time()) + 3600)
    assert not verify(SECRET, future, body, sign(future, body))
    ts = str(int(time.time()))
    assert not verify(SECRET, ts, body, "sha256=caf\u00e9")  # no TypeError


def test_bad_content_length_is_400():
    import socket

    integration = FakeIntegration()
    server, coalescer = start(integration)
    try:
        for value in ("-5", "abc"):
            with socket.create_connection(("127.0.0.1", server.server_port), timeout=5) as sock:
                sock.sendall(
                    f"POST / HTTP/1.1\r\nHost: x\r\nContent-Length: {value}\r\n\r\n".encode()
                )
                assert sock.recv(100).startswith(b"HTTP/1.0 400")
    finally:
        server.shutdown()
        coalescer.close()


def test_a_real_sqlite_integration_polls_on_the_coalescer_thread(tmp_path):
    """The connection is built on this thread, the poll runs on the coalescer's."""
    from fake_makia import RUN_SECRET, SYNC_SECRET, FakeMakia

    from makia_integration.api import MakiaClient
    from makia_integration.db import connect, create_schema, get_state
    from makia_integration.jobs import Integration

    makia = FakeMakia()
    conn, dialect = connect(str(tmp_path / "integration.db"))
    create_schema(conn, dialect)
    url = "https://makia.example"
    integration = Integration(
        conn=conn,
        sync_api=MakiaClient(url, SYNC_SECRET, transport=makia.transport()),
        run_api=MakiaClient(url, RUN_SECRET, transport=makia.transport()),
        storage_connection_id="c",
    )
    server, coalescer = start(integration)
    try:
        assert post(server.server_port, b"{}") == 204
        assert wait_for(lambda: ("GET", "/changes/head") in makia.calls)
        assert wait_for(lambda: get_state(conn, "feed_marker") == "0")
    finally:
        server.shutdown()
        coalescer.close()
