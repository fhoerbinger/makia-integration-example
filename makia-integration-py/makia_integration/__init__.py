"""Connect a customer system to MAKIA through its stable API."""

from __future__ import annotations

from makia_integration.integration import (
    FINAL_REFUSALS,
    FIXABLE_REFUSALS,
    MAX_FILE_BYTES,
    PROGRESS_READS_PER_POLL,
    Integration,
    ConfigError,
    PollReport,
    SendReport,
    SyncReport,
)
from makia_integration.client import (
    ROUTES_USED,
    ApiError,
    MakiaClient,
    TransportFailed,
    __version__,
)

from makia_integration.config import Config, load_integration, load_config, read_env_file
from makia_integration.doctor import Check, run_checks
from makia_integration.runner import Runner
from makia_integration.state import RunEntry, SqliteStateStore, StateLocked, StateStore
from makia_integration.model import (
    Member,
    Project,
    Record,
    ResultRow,
    RunRef,
    RunState,
    SentRun,
    Sink,
    Source,
)

__all__ = [
    "FINAL_REFUSALS",
    "FIXABLE_REFUSALS",
    "MAX_FILE_BYTES",
    "PROGRESS_READS_PER_POLL",
    "PollReport",
    "Integration",
    "SendReport",
    "Check",
    "Config",
    "ConfigError",
    "SyncReport",
    "ROUTES_USED",
    "ApiError",
    "MakiaClient",
    "Member",
    "Project",
    "Record",
    "ResultRow",
    "RunEntry",
    "Runner",
    "RunRef",
    "RunState",
    "SentRun",
    "Sink",
    "SqliteStateStore",
    "Source",
    "StateLocked",
    "StateStore",
    "TransportFailed",
    "__version__",
    "load_integration",
    "load_config",
    "run_checks",
    "read_env_file",
]
