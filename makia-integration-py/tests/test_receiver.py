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
        self.ticked = threading.Event()  # set on every poll

    def poll(self) -> None:
        self.polls += 1
        self.started.set()
        self.ticked.set()
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
        assert integration.ticked.wait(5)
        assert post(server.server_port, b"{}", signature="sha256=00") == 401
        assert post(server.server_port, b"{}", ts=str(int(time.time()) - 900)) == 401
        integration.ticked.clear()
        assert not integration.ticked.wait(0.1)  # refused deliveries poll nothing
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
        integration.ticked.clear()
        integration.gate.set()
        assert integration.ticked.wait(5)  # the one more poll
        integration.ticked.clear()
        assert not integration.ticked.wait(0.2)  # and no third
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


def test_an_escaping_error_is_logged_as_a_failed_round_not_a_poll(caplog):
    ran = threading.Event()

    def run():
        ran.set()
        raise RuntimeError("factory bug")

    with caplog.at_level("ERROR", logger="makia_integration"):
        c = PollCoalescer(run)
        c.kick()
        assert ran.wait(2)
        c.close()
    assert "round failed" in caplog.text and "poll failed" not in caplog.text
