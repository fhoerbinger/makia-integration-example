from __future__ import annotations

import socket
from pathlib import Path

import httpx
import pytest
from integration_doubles import MemorySource, RecordingSink

from makia_integration import Integration, Check, Config, run_checks
from makia_integration.cli import main
from makia_integration.model import Member, Project, Record
from makia_integration.testing import RUN_SECRET, SYNC_SECRET

URL = "https://makia.example"


def make_config(tmp_path, **over) -> Config:
    values = dict(
        api_url=URL,
        sync_token=SYNC_SECRET,
        run_token=RUN_SECRET,
        storage_connection_id="conn-1",
        files_dir=None,
        state_file=tmp_path / "state.db",
        ca_bundle=None,
        integration_ref=None,
        webhook_secret=None,
    )
    values.update(over)
    return Config(**values)


def make_factory(makia, source=None):
    opened = []

    def factory(config: Config) -> Integration:
        sync_api, run_api = config.clients()
        integration = Integration(
            source or MemorySource(projects=[Project("p-1", "Alpha")]),
            RecordingSink(),
            sync_api=sync_api,
            run_api=run_api,
            state=config.open_state(),
            storage_connection_id=config.storage_connection_id,
            files_dir=config.files_dir,
        )
        opened.append(integration)
        return integration

    factory.opened = opened
    return factory


def by_name(checks: list[Check]) -> dict[str, Check]:
    return {c.name: c for c in checks}


def test_all_ok_against_fake(makia, tmp_path):
    checks = run_checks(
        make_config(tmp_path),
        factory=make_factory(makia),
        listen_port=None,
        transport=makia.transport(),
    )
    assert [c.name for c in checks][:3] == ["python", "configuration", "network"]
    assert all(c.ok for c in checks), [c for c in checks if not c.ok]


def test_missing_files_dir_fails_with_fix(makia, tmp_path):
    config = make_config(tmp_path, files_dir=tmp_path / "nope", storage_connection_id="")
    checks = by_name(
        run_checks(config, factory=None, listen_port=None, transport=makia.transport())
    )
    cfg = checks["configuration"]
    assert not cfg.ok and "MAKIA_FILES_DIR" in (cfg.fix or "")


def test_bad_token_fails_token_check(makia, tmp_path):
    config = make_config(tmp_path, sync_token="wrong")
    checks = by_name(
        run_checks(config, factory=None, listen_port=None, transport=makia.transport())
    )
    assert not checks["sync token"].ok
    assert "invalid_token" in checks["sync token"].detail
    assert "MAKIA_SYNC_TOKEN" in (checks["sync token"].fix or "")
    assert checks["run token"].ok


def test_tls_failure_suggests_ca_bundle(tmp_path):
    def boom(request):
        raise httpx.ConnectError("[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer")

    checks = run_checks(
        make_config(tmp_path),
        factory=None,
        listen_port=None,
        transport=httpx.MockTransport(boom),
    )
    bad = [c for c in checks if not c.ok and "token" in c.name]
    assert bad and all("MAKIA_CA_BUNDLE" in (c.fix or "") for c in bad)


def test_source_counts_and_first_items(makia, tmp_path):
    source = MemorySource(
        projects=[Project("p-1", "Alpha"), Project("p-2", "Beta")],
        members=[Member("p-1", "ann@example.com", ("run",))],
        records=[Record("r-1", "p-1", "wf", "in-1")],
    )
    checks = by_name(
        run_checks(
            make_config(tmp_path),
            factory=make_factory(makia, source),
            listen_port=None,
            transport=makia.transport(),
        )
    )
    integration = checks["integration"]
    assert integration.ok
    assert "2 projects" in integration.detail and "1 members" in integration.detail
    assert "1 records" in integration.detail and "Alpha" in integration.detail


def test_multipart_files_checked(makia, tmp_path):
    (tmp_path / "files").mkdir()
    (tmp_path / "files" / "a.pdf").write_bytes(b"x")
    rec = Record("r-1", "p-1", "wf", "in-1")
    ok = MemorySource(records=[rec], files={"r-1": ["a.pdf"]})
    config = make_config(tmp_path, storage_connection_id="", files_dir=tmp_path / "files")
    checks = by_name(
        run_checks(
            config,
            factory=make_factory(makia, ok),
            listen_port=None,
            transport=makia.transport(),
        )
    )
    assert checks["integration"].ok
    missing = MemorySource(records=[rec], files={"r-1": ["gone.pdf"]})
    checks = by_name(
        run_checks(
            config,
            factory=make_factory(makia, missing),
            listen_port=None,
            transport=makia.transport(),
        )
    )
    assert not checks["integration"].ok and "gone.pdf" in checks["integration"].detail


def test_no_integration_and_factory_errors_are_fails(makia, tmp_path):
    checks = by_name(
        run_checks(
            make_config(tmp_path), factory=None, listen_port=None, transport=makia.transport()
        )
    )
    assert (
        not checks["integration"].ok and "no integration configured" in checks["integration"].detail
    )

    def broken(config):
        raise RuntimeError("db down")

    checks = by_name(
        run_checks(
            make_config(tmp_path), factory=broken, listen_port=None, transport=makia.transport()
        )
    )
    assert "RuntimeError: db down" in checks["integration"].detail


def test_state_closed_after_integration_check(makia, tmp_path):
    factory = make_factory(makia)
    config = make_config(tmp_path)
    run_checks(config, factory=factory, listen_port=None, transport=makia.transport())
    config.open_state().close()  # would raise StateLocked if doctor left it open


def test_doctor_never_writes(makia, tmp_path):
    run_checks(
        make_config(tmp_path),
        factory=make_factory(makia),
        listen_port=None,
        transport=makia.transport(),
    )
    assert makia.calls
    assert all(m == "GET" or p == "/auth/token" for m, p in makia.calls)


def test_port_in_use_fails(makia, tmp_path):
    s = socket.socket()
    s.bind(("", 0))
    s.listen()
    try:
        port = s.getsockname()[1]
        checks = by_name(
            run_checks(
                make_config(tmp_path),
                factory=None,
                listen_port=port,
                transport=makia.transport(),
            )
        )
    finally:
        s.close()
    assert not checks["listen port"].ok and str(port) in checks["listen port"].detail


def test_cli_prints_and_exits(makia, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("MAKIA_SYNC_TOKEN", SYNC_SECRET)
    monkeypatch.setenv("MAKIA_RUN_TOKEN", RUN_SECRET)
    monkeypatch.setenv("MAKIA_STATE_FILE", str(tmp_path / "s.db"))
    monkeypatch.setenv("MAKIA_API_URL", "http://127.0.0.1:1")
    code = main(["doctor"])
    out = capsys.readouterr().out
    assert code == 1
    assert out.startswith("OK   python")
    assert "FAIL integration" in out and "     fix: " in out
    assert SYNC_SECRET not in out and RUN_SECRET not in out


def test_cli_unset_variables_are_fail_lines(tmp_path, monkeypatch, capsys):
    for n in ("MAKIA_API_URL", "MAKIA_SYNC_TOKEN", "MAKIA_RUN_TOKEN"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.chdir(tmp_path)
    assert main(["doctor"]) == 1  # unset variables are a FAIL line, not a crash
    assert "FAIL configuration" in capsys.readouterr().out


@pytest.fixture(autouse=True)
def _no_real_env(monkeypatch):
    for n in (
        "MAKIA_INTEGRATION",
        "MAKIA_FILES_DIR",
        "MAKIA_CA_BUNDLE",
        "MAKIA_STORAGE_CONNECTION_ID",
    ):
        monkeypatch.delenv(n, raising=False)


def test_state_locked_is_not_inspected(makia, tmp_path):
    from makia_integration.state import StateLocked

    def locked(config):
        raise StateLocked(config.state_file)

    checks = by_name(
        run_checks(
            make_config(tmp_path), factory=locked, listen_port=None, transport=makia.transport()
        )
    )
    assert checks["integration"].ok and "not inspected" in checks["integration"].detail


def test_missing_api_url_fails_both_tokens(tmp_path):
    config = make_config(tmp_path, api_url="")
    checks = by_name(run_checks(config, factory=None, listen_port=None))
    assert not checks["sync token"].ok and not checks["run token"].ok


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def tls_server():
    import ssl
    import threading

    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(FIXTURES / "localhost.pem", FIXTURES / "localhost.key")
    raw = socket.socket()
    raw.bind(("127.0.0.1", 0))
    raw.listen()
    stop = threading.Event()

    def serve():
        raw.settimeout(0.2)
        while not stop.is_set():
            try:
                conn, _ = raw.accept()
            except OSError:
                continue
            try:
                with ctx.wrap_socket(conn, server_side=True) as tls:
                    tls.recv(4096)
                    body = b"{}"
                    tls.sendall(
                        b"HTTP/1.1 404 Not Found\r\nContent-Length: 2\r\n"
                        b"Connection: close\r\n\r\n" + body
                    )
            except OSError:
                pass

    t = threading.Thread(target=serve, daemon=True)
    t.start()
    yield f"https://localhost:{raw.getsockname()[1]}"
    stop.set()
    t.join(2)
    raw.close()


@pytest.fixture(autouse=True)
def _no_proxy(monkeypatch):
    for n in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy", "HTTP_PROXY", "http_proxy"):
        monkeypatch.delenv(n, raising=False)
    monkeypatch.setenv("NO_PROXY", "*")


def test_untrusted_certificate_is_shown_and_saved(tls_server, tmp_path):
    checks = by_name(
        run_checks(make_config(tmp_path, api_url=tls_server), factory=None, listen_port=None)
    )
    net = checks["network"]
    assert not net.ok
    assert "CERTIFICATE_VERIFY_FAILED" in net.detail or "self" in net.detail.lower()
    assert "SHA-256" in net.detail
    pem = tmp_path / "makia-server-cert.pem"
    assert pem.read_text().startswith("-----BEGIN CERTIFICATE-----")
    assert f"MAKIA_CA_BUNDLE={pem}" in (net.fix or "")


def test_ca_bundle_makes_network_ok(tls_server, tmp_path):
    config = make_config(tmp_path, api_url=tls_server, ca_bundle=str(FIXTURES / "localhost.pem"))
    checks = by_name(run_checks(config, factory=None, listen_port=None))
    assert checks["network"].ok and "404" in checks["network"].detail


def test_both_send_modes_set_is_a_fail(makia, tmp_path):
    config = make_config(tmp_path, files_dir=tmp_path)  # storage_connection_id is conn-1
    checks = by_name(
        run_checks(config, factory=None, listen_port=None, transport=makia.transport())
    )
    st = checks["storage connection"]
    assert not st.ok and "both" in st.detail and "not both" in (st.fix or "")


def test_the_saved_certificate_file_is_a_working_ca_bundle(tls_server, tmp_path):
    config = make_config(tmp_path, api_url=tls_server)
    first = by_name(run_checks(config, factory=None, listen_port=None))
    assert not first["network"].ok
    saved = tmp_path / "makia-server-cert.pem"
    again = make_config(tmp_path, api_url=tls_server, ca_bundle=str(saved))
    checks = run_checks(again, factory=None, listen_port=None)
    assert checks[2].name == "network" and checks[2].ok, checks[2]


def test_ca_bundle_that_is_not_pem_is_a_fail_line(makia, tmp_path):
    bad = tmp_path / "ca.pem"
    bad.write_text("nope", encoding="utf-8")
    checks = by_name(
        run_checks(
            make_config(tmp_path, ca_bundle=str(bad)),
            factory=None,
            listen_port=None,
            transport=makia.transport(),
        )
    )
    assert not checks["sync token"].ok and str(bad) in checks["sync token"].detail
