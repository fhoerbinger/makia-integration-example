"""Configuration: environment variables (optionally from a file) and the integration factory."""

from __future__ import annotations

import importlib
import os
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path

from makia_integration.integration import Integration, ConfigError, send_mode_problem
from makia_integration.client import MakiaClient, tls_context
from makia_integration.state import SqliteStateStore

__all__ = [
    "Config",
    "ConfigError",
    "load_integration",
    "load_config",
    "read_env_file",
    "send_mode_problem",
]

DEFAULT_STATE_FILE = "makia-state.db"
_ALL_TOKENS = ("MAKIA_API_URL", "MAKIA_SYNC_TOKEN", "MAKIA_RUN_TOKEN")


@dataclass(frozen=True, slots=True)
class Config:
    api_url: str
    sync_token: str = field(repr=False)
    run_token: str = field(repr=False)
    storage_connection_id: str
    files_dir: Path | None
    state_file: Path
    ca_bundle: str | None
    integration_ref: str | None
    webhook_secret: str | None = field(repr=False)
    run_account_permissions: tuple[str, ...] = ("cases", "run", "review")

    def _client(self, secret: str) -> MakiaClient:
        return MakiaClient(self.api_url, secret, verify=tls_context(self.ca_bundle))

    def sync_client(self) -> MakiaClient:
        return self._client(self.sync_token)

    def run_client(self) -> MakiaClient:
        return self._client(self.run_token)

    def clients(self) -> tuple[MakiaClient, MakiaClient]:
        """(sync_api, run_api), for a integration factory."""
        return self.sync_client(), self.run_client()

    def open_state(self) -> SqliteStateStore:
        """Open (and lock) the state file. A factory that opens it and then raises
        must close it, or the next attempt finds it locked."""
        return SqliteStateStore(self.state_file)


def read_env_file(path: Path) -> dict[str, str]:
    """`KEY=VALUE` lines; blank lines and `#` comments are ignored."""
    try:
        text = Path(path).read_text(encoding="utf-8-sig")
    except (OSError, UnicodeDecodeError) as err:
        raise ConfigError(f"cannot read env file {path} ({err})") from err
    values: dict[str, str] = {}
    for number, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].lstrip()
        key, eq, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not eq or not key or any(c.isspace() for c in key):
            raise ConfigError(f"{path} line {number} is not KEY=VALUE")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        values[key] = value
    return values


def load_config(
    env: Mapping[str, str],
    *,
    require: tuple[str, ...] = _ALL_TOKENS,
    sending: bool = False,
) -> Config:
    """Build a Config; `require` names the variables the command cannot do without.
    `sending` (send, run, listen): exactly one of MAKIA_STORAGE_CONNECTION_ID and
    MAKIA_FILES_DIR must be set."""

    def get(name: str) -> str:
        return env.get(name, "").strip()

    for name in require:
        if not get(name):
            raise ConfigError(f"set {name} (see the setup guide: makia-integration docs)")
    files_dir = get("MAKIA_FILES_DIR")
    if sending:
        problem = send_mode_problem(
            get("MAKIA_STORAGE_CONNECTION_ID"), Path(files_dir) if files_dir else None
        )
        if problem:
            raise ConfigError(problem)
    ca_bundle = get("MAKIA_CA_BUNDLE")
    if ca_bundle:
        try:
            with Path(ca_bundle).open("rb"):
                pass
        except OSError as err:
            reason = err.strerror or type(err).__name__
            raise ConfigError(
                f"MAKIA_CA_BUNDLE {ca_bundle} cannot be read ({reason}); "
                "point it at the PEM file of the CA"
            ) from err
    return Config(
        api_url=get("MAKIA_API_URL"),
        sync_token=get("MAKIA_SYNC_TOKEN"),
        run_token=get("MAKIA_RUN_TOKEN"),
        storage_connection_id=get("MAKIA_STORAGE_CONNECTION_ID"),
        files_dir=Path(files_dir) if files_dir else None,
        state_file=Path(get("MAKIA_STATE_FILE") or DEFAULT_STATE_FILE),
        ca_bundle=ca_bundle or None,
        integration_ref=get("MAKIA_INTEGRATION") or None,
        webhook_secret=get("MAKIA_WEBHOOK_SECRET") or None,
    )


def load_integration(ref: str) -> Callable[[Config], Integration]:
    """Resolve `"package.module:function"`; the current directory is importable."""
    module_name, colon, func_name = ref.partition(":")
    if not colon or not module_name or not func_name:
        raise ConfigError(f"integration {ref!r} must look like package.module:function")
    cwd = os.getcwd()
    if cwd not in sys.path and "" not in sys.path:
        sys.path.append(cwd)
    try:
        module = importlib.import_module(module_name)
    except Exception as err:  # a integration module can fail in any way while importing
        raise ConfigError(
            f"cannot import {module_name} for the integration ({type(err).__name__}: {err})"
        ) from err
    factory = getattr(module, func_name, None)
    if not callable(factory):
        raise ConfigError(f"{module_name} has no function {func_name}")
    return factory  # type: ignore[no-any-return]
