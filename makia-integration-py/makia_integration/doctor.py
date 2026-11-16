"""The checks behind `makia-integration doctor`: run them first on a new server.

Each check is OK or FAIL with a one-line fix. Nothing here writes to MAKIA: the only
routes called are POST /auth/token, GET /me and GET /sync/projects.
"""

from __future__ import annotations

import hashlib
import os
import platform
import socket
import ssl
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import ParseResult, urlparse

import httpx

from makia_integration.integration import (
    MAX_FILE_BYTES,
    Integration,
    ConfigError,
    send_mode_problem,
)
from makia_integration.client import (
    ApiError,
    MakiaClient,
    TransportFailed,
    __version__,
    tls_context,
)
from makia_integration.config import Config
from makia_integration.state import StateLocked

__all__ = ["Check", "run_checks", "render"]

_SHOWN = 120


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str | None = None


def _ok(name: str, detail: str) -> Check:
    return Check(name, True, detail)


def _fail(name: str, detail: str, fix: str) -> Check:
    return Check(name, False, detail, fix)


def _crash(name: str, err: BaseException, fix: str) -> Check:
    return _fail(name, f"{type(err).__name__}: {err}", fix)


def _short(value: object) -> str:
    text = repr(value)
    return text if len(text) <= _SHOWN else text[: _SHOWN - 3] + "..."


def run_checks(
    config: Config,
    *,
    factory: Callable[[Config], Integration] | None,
    listen_port: int | None,
    transport: httpx.BaseTransport | None = None,
) -> list[Check]:
    checks = [_python(), _configuration(config), _network(config, transport)]
    checks += _tokens(config, transport)
    checks.append(_integration(config, factory))
    checks.append(_storage(config))
    if listen_port is not None:
        checks.append(_port(listen_port))
    return checks


def render(checks: list[Check]) -> str:
    lines: list[str] = []
    for c in checks:
        lines.append(f"{'OK  ' if c.ok else 'FAIL'} {c.name}  {c.detail}")
        if not c.ok and c.fix:
            lines.append(f"     fix: {c.fix}")
    return "\n".join(lines)


def _python() -> Check:
    v = sys.version_info
    detail = f"python {v.major}.{v.minor}.{v.micro}, makia-integration {__version__}, "
    detail += f"{platform.system()} {platform.release()}"
    if v < (3, 12):
        return _fail("python", detail, "install Python 3.12 or newer")
    return _ok("python", detail)


def _writable_dir(directory: Path) -> str | None:
    """None when a file can be made in `directory` (created if absent), else the reason."""
    try:
        directory.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=directory, prefix=".doctor-", delete=True):
            pass
    except OSError as err:
        return str(err)
    return None


def _configuration(config: Config) -> Check:
    missing = [
        name
        for name, value in (
            ("MAKIA_API_URL", config.api_url),
            ("MAKIA_SYNC_TOKEN", config.sync_token),
            ("MAKIA_RUN_TOKEN", config.run_token),
        )
        if not value
    ]
    if missing:
        return _fail(
            "configuration",
            "not set: " + ", ".join(missing),
            f"set {', '.join(missing)} (in the environment or an --env-file)",
        )
    if config.files_dir is not None and not config.files_dir.is_dir():
        return _fail(
            "configuration",
            f"MAKIA_FILES_DIR {config.files_dir} is not a directory",
            "create the folder or point MAKIA_FILES_DIR at the folder that holds the files",
        )
    parent = config.state_file.resolve().parent
    reason = _writable_dir(parent)
    if reason is not None:
        return _fail(
            "configuration",
            f"cannot write next to the state file {config.state_file} ({reason})",
            "set MAKIA_STATE_FILE to a path in a folder this user can write",
        )
    return _ok("configuration", f"variables set, state file {config.state_file}")


def _tls_fix(message: str) -> str:
    if "CERTIFICATE_VERIFY" in message or "certificate" in message.lower():
        return (
            "the server's certificate is not trusted here: set MAKIA_CA_BUNDLE to the "
            "PEM file of your company's CA (or fix the proxy that re-signs traffic)"
        )
    return (
        "check the MAKIA_API_URL host name, the network or proxy "
        "(HTTPS_PROXY), and MAKIA_CA_BUNDLE if a private CA signs the certificate"
    )


def _cause_chain(err: BaseException | None) -> list[BaseException]:
    seen: list[BaseException] = []
    while err is not None and err not in seen:
        seen.append(err)
        err = err.__cause__ or err.__context__
    return seen


def _proxy_host() -> str | None:
    for key in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
        value = os.environ.get(key, "").strip()
        if value:
            return urlparse(value if "//" in value else "//" + value).hostname or "set"
    return None


def _network(config: Config, transport: httpx.BaseTransport | None) -> Check:
    if transport is not None:
        return _ok("network", "skipped (test transport)")
    if not config.api_url:
        return _fail("network", "MAKIA_API_URL is not set", "set MAKIA_API_URL")
    url = urlparse(config.api_url)
    if not url.hostname or url.scheme not in ("http", "https"):
        return _fail(
            "network",
            f"{config.api_url!r} is not an http(s) URL",
            "set MAKIA_API_URL like https://makia.example.com",
        )
    store = config.ca_bundle or "system store"
    proxy = _proxy_host()
    how = f"trust: {store}" + (f", proxy {proxy}" if proxy else "")
    try:
        with httpx.Client(
            timeout=10.0, verify=tls_context(config.ca_bundle), trust_env=True
        ) as http:
            resp = http.get(config.api_url.rstrip("/") + "/openapi.json")
    except Exception as err:
        verify_err = next(
            (e for e in _cause_chain(err) if isinstance(e, ssl.SSLCertVerificationError)), None
        )
        if verify_err is None:
            return _fail("network", f"{type(err).__name__}: {err} ({how})", _tls_fix(str(err)))
        return _certificate_failure(config, url, verify_err, how)
    return _ok("network", f"{config.api_url} answered {resp.status_code} ({how})")


def _certificate_failure(
    config: Config, url: ParseResult, err: ssl.SSLCertVerificationError, how: str
) -> Check:
    detail = f"certificate not trusted: {err.verify_message or err} ({how})"
    host = url.hostname or ""
    port = url.port or 443
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        with socket.create_connection((host, port), timeout=5) as sock:
            with context.wrap_socket(sock, server_hostname=host) as tls:
                leaf = tls.getpeercert(binary_form=True)
                chain_fn = getattr(tls, "get_unverified_chain", None)
                chain = (
                    [bytes(c) if not isinstance(c, bytes) else c for c in chain_fn()]
                    if chain_fn
                    else []
                )
        if leaf:
            digest = hashlib.sha256(leaf).hexdigest().upper()
            fingerprint = ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))
            detail += f"; server certificate SHA-256 {fingerprint}"
            der_list = chain or [leaf]
            pem = "".join(ssl.DER_cert_to_PEM_cert(d) for d in der_list)
            path = config.state_file.resolve().parent / "makia-server-cert.pem"
            path.write_text(pem, encoding="ascii")  # best effort: an OSError lands below
            detail += f", saved to {path}"
            return _fail(
                "network",
                detail,
                f"check this certificate is your organization's CA, then set MAKIA_CA_BUNDLE={path}",
            )
    except Exception:  # a probe is best effort; `detail` keeps the fingerprint if it got that far
        pass
    return _fail("network", detail, _tls_fix("certificate"))


def _client(config: Config, secret: str, transport: httpx.BaseTransport | None) -> MakiaClient:
    return MakiaClient(
        config.api_url, secret, transport=transport, verify=tls_context(config.ca_bundle), retries=0
    )


def _api_failure(name: str, err: Exception, env_name: str) -> Check:
    if isinstance(err, TransportFailed):
        return _fail(name, str(err), _tls_fix(str(err)))
    if isinstance(err, ApiError):
        return _fail(
            name,
            f"MAKIA answered {err}",
            f"check {env_name}: it must be the token as minted in Studio, with its permissions",
        )
    return _crash(name, err, f"check {env_name} and MAKIA_API_URL")


def _tokens(config: Config, transport: httpx.BaseTransport | None) -> list[Check]:
    out: list[Check] = []
    if not config.api_url:
        return [
            _fail(name, "MAKIA_API_URL is not set", "set MAKIA_API_URL")
            for name in ("sync token", "run token")
        ]
    for name, secret, env_name in (
        ("sync token", config.sync_token, "MAKIA_SYNC_TOKEN"),
        ("run token", config.run_token, "MAKIA_RUN_TOKEN"),
    ):
        if not secret:
            out.append(_fail(name, f"{env_name} is not set", f"set {env_name}"))
            continue
        try:
            client = _client(config, secret, transport)
        except ConfigError as err:
            out.append(_fail(name, str(err), "set MAKIA_CA_BUNDLE to a PEM file, or unset it"))
            continue
        try:
            if name == "sync token":
                projects = client.synced_projects()
                out.append(_ok(name, f"swapped at /auth/token; {len(projects)} synced projects"))
            else:
                me = client.me()
                who = me.get("email") or me.get("id") or me.get("name") or "account"
                out.append(_ok(name, f"swapped at /auth/token; GET /me answered for {who}"))
        except Exception as err:
            out.append(_api_failure(name, err, env_name))
        finally:
            client.close()
    return out


def _integration(config: Config, factory: Callable[[Config], Integration] | None) -> Check:
    if factory is None:
        return _fail(
            "integration",
            "no integration configured",
            "set MAKIA_INTEGRATION=package.module:function or pass --integration",
        )
    try:
        integration = factory(config)
    except StateLocked:
        return _ok(
            "integration",
            f"not inspected: state file {config.state_file} is in use by a running integration",
        )
    except Exception as err:
        return _crash(
            "integration", err, "fix the integration factory; run with -v for the traceback"
        )
    try:
        return _inspect(integration)
    except Exception as err:
        return _crash("integration", err, "fix the Source; run with -v for the traceback")
    finally:
        integration.close()  # best effort, closes the source and sink too


def _inspect(integration: Integration) -> Check:
    source = integration.source
    projects = list(source.projects())
    members = list(source.members())
    records = list(source.records())
    detail = f"{len(projects)} projects, {len(members)} members, {len(records)} records"
    first = next((x for x in (projects, members, records) if x), None)
    if first:
        detail += f"; first: {_short(first[0])}"
    if not records:
        return _ok("integration", detail)
    paths = source.files(records[0])
    detail += f"; {len(paths)} files for the first record"
    if integration.storage_connection_id:
        return _ok("integration", detail)
    if integration.files_dir is None:
        return _fail(
            "integration",
            detail,
            "set MAKIA_FILES_DIR (files are sent from it) or MAKIA_STORAGE_CONNECTION_ID",
        )
    root = integration.files_dir.resolve()
    for rel in paths:
        file = (root / rel.replace("\\", "/")).resolve()
        if not file.is_relative_to(root) or not file.is_file():
            return _fail(
                "integration",
                f"{detail}; file not found in the folder: {rel}",
                "make Source.files return paths relative to MAKIA_FILES_DIR that exist",
            )
        size = file.stat().st_size
        if size > MAX_FILE_BYTES:
            return _fail(
                "integration",
                f"{detail}; {rel} is {size} bytes",
                f"MAKIA takes files up to {MAX_FILE_BYTES // (1024 * 1024)} MB: leave it out",
            )
    return _ok("integration", detail)


def _storage(config: Config) -> Check:
    problem = send_mode_problem(config.storage_connection_id, config.files_dir)
    if problem and config.storage_connection_id:
        return _fail(
            "storage connection",
            "both MAKIA_STORAGE_CONNECTION_ID and MAKIA_FILES_DIR are set",
            problem,
        )
    if config.storage_connection_id:
        return _ok(
            "storage connection", f"MAKIA_STORAGE_CONNECTION_ID={config.storage_connection_id}"
        )
    if config.files_dir is not None:
        return _ok("storage connection", "not set: files are sent from MAKIA_FILES_DIR")
    return _fail(
        "storage connection",
        "neither MAKIA_STORAGE_CONNECTION_ID nor MAKIA_FILES_DIR is set",
        "set MAKIA_STORAGE_CONNECTION_ID (files stay in a bucket) or MAKIA_FILES_DIR (uploaded)",
    )


def _port(port: int) -> Check:
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if sys.platform != "win32":
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(("", port))
    except OSError as err:
        return _fail(
            "listen port",
            f"cannot bind port {port} ({err})",
            "stop the program using it or pick another --listen port",
        )
    finally:
        sock.close()
    return _ok("listen port", f"port {port} can be bound")
