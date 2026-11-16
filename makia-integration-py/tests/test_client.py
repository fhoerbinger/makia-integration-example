from __future__ import annotations

from unittest import mock

import httpx
import pytest

from makia_integration.client import ApiError, MakiaClient, TransportFailed

URL = "https://makia.example"


def _client(handler, sleeps=None, **kw):
    sleeps = [] if sleeps is None else sleeps
    return MakiaClient(
        URL, "secret", transport=httpx.MockTransport(handler), sleep=sleeps.append, **kw
    )


def _scripted(steps, seen):
    """Answers each request with the next step: a status int or an exception."""
    it = iter(steps)

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "a", "expires_in": 900})
        step = next(it)
        if isinstance(step, Exception):
            raise step
        if step == 200:
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(step, json={"error": "boom"})

    return handler


def test_retries_5xx_then_succeeds():
    sleeps, seen = [], []
    client = _client(_scripted([503, 503, 200], seen), sleeps)
    assert client.me() == {"ok": True}
    assert len(sleeps) == 2
    assert sleeps[1] > sleeps[0]


def test_retries_429_and_transport_errors():
    sleeps, seen = [], []
    client = _client(_scripted([429, httpx.ConnectError("down"), 200], seen), sleeps)
    assert client.me() == {"ok": True}
    assert len(sleeps) == 2


def test_retry_after_is_honoured():
    sleeps = []
    calls = iter([httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, json={})])

    def handler(request):
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "a", "expires_in": 900})
        return next(calls)

    _client(handler, sleeps).me()
    assert sleeps == [7.0]


@pytest.mark.parametrize(("header", "expected"), [("999", 30.0), ("-5", 0.5), ("soon", 0.5)])
def test_retry_after_is_capped_and_clamped(header, expected):
    sleeps = []
    calls = iter(
        [httpx.Response(503, headers={"Retry-After": header}), httpx.Response(200, json={})]
    )

    def handler(request):
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "a", "expires_in": 900})
        return next(calls)

    _client(handler, sleeps).me()
    assert sleeps == [expected]


def test_no_retry_on_4xx():
    seen, sleeps = [], []

    def handler(request):
        seen.append(request)
        if request.url.path == "/auth/token":
            return httpx.Response(200, json={"access_token": "a", "expires_in": 900})
        return httpx.Response(404, json={"error": "project_not_found"})

    with pytest.raises(ApiError) as exc:
        _client(handler, sleeps).me()
    assert exc.value.code == "project_not_found"
    assert [r.url.path for r in seen].count("/me") == 1
    assert sleeps == []


def test_gives_up_after_retries():
    sleeps = []

    def handler(request):
        raise httpx.ConnectError("down")

    with pytest.raises(TransportFailed):
        _client(handler, sleeps).me()
    assert len(sleeps) == 4


def test_user_agent():
    seen = []
    client = _client(_scripted([200], seen))
    client.me()
    assert seen
    assert all(r.headers["User-Agent"] == "makia-integration/0.1.0" for r in seen)


def test_external_id_is_quoted():
    seen = []
    client = _client(_scripted([200], seen))
    client.put_project("2026/114 Ä", name="n", archived=False, members=[])
    put = [r for r in seen if r.method == "PUT"][0]
    assert put.url.raw_path.decode().split("?")[0] == "/sync/projects/2026%2F114%20%C3%84"


def test_ca_bundle_passed():
    with mock.patch("makia_integration.client.httpx.Client") as cls:
        MakiaClient(URL, "s", verify="/x/ca.pem")
    assert cls.call_args.kwargs["verify"] == "/x/ca.pem"


def test_default_verify_uses_system_store():
    import ssl

    with mock.patch("makia_integration.client.httpx.Client") as cls:
        MakiaClient(URL, "s")
    ctx = cls.call_args.kwargs["verify"]
    assert isinstance(ctx, ssl.SSLContext) and ctx.verify_mode == ssl.CERT_REQUIRED


def test_create_webhook_is_never_retried():
    sleeps, seen = [], []
    client = _client(_scripted([503, 201], seen), sleeps)
    with pytest.raises(ApiError) as err:
        client.create_webhook(name="b", connection_id="c", path="/h")
    assert err.value.status == 503
    assert sum(1 for r in seen if r.url.path == "/webhooks") == 1 and sleeps == []


def test_create_webhook_transport_error_is_not_retried():
    seen = []
    client = _client(_scripted([httpx.ConnectError("down"), 201], seen))
    with pytest.raises(TransportFailed):
        client.create_webhook(name="b", connection_id="c", path="/h")
    assert sum(1 for r in seen if r.url.path == "/webhooks") == 1


FIXTURES = __import__("pathlib").Path(__file__).parent / "fixtures"


def test_tls_context_accepts_a_partial_chain_and_is_not_strict():
    import ssl

    from makia_integration.client import tls_context

    ctx = tls_context(str(FIXTURES / "localhost.pem"))
    assert ctx.verify_flags & ssl.VERIFY_X509_PARTIAL_CHAIN
    assert not ctx.verify_flags & getattr(ssl, "VERIFY_X509_STRICT", 0)
    assert not tls_context(None).verify_flags & getattr(ssl, "VERIFY_X509_STRICT", 0)


def test_tls_context_names_a_file_that_is_not_pem(tmp_path):
    from makia_integration.client import tls_context
    from makia_integration.model import ConfigError

    bad = tmp_path / "ca.pem"
    bad.write_text("not a certificate\n", encoding="utf-8")
    with pytest.raises(ConfigError, match=str(bad).replace("\\", "\\\\")):
        tls_context(str(bad))
