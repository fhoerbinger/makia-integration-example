from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).parent
sys.path[:0] = [str(HERE.parent), str(HERE)]

import pytest  # noqa: E402
from integration_doubles import MemorySource, RecordingSink  # noqa: E402

from makia_integration.client import MakiaClient  # noqa: E402
from makia_integration.testing import RUN_SECRET, SYNC_SECRET, FakeMakia  # noqa: E402

URL = "https://makia.example"


@pytest.fixture
def makia() -> FakeMakia:
    return FakeMakia()


@pytest.fixture
def api(makia: FakeMakia) -> tuple[MakiaClient, MakiaClient]:
    return (
        MakiaClient(URL, SYNC_SECRET, transport=makia.transport(), sleep=lambda s: None),
        MakiaClient(URL, RUN_SECRET, transport=makia.transport(), sleep=lambda s: None),
    )


@pytest.fixture
def source() -> MemorySource:
    return MemorySource()


@pytest.fixture
def sink() -> RecordingSink:
    return RecordingSink()


@pytest.fixture
def state(tmp_path):
    from makia_integration.state import SqliteStateStore

    store = SqliteStateStore(tmp_path / "state.db")
    yield store
    store.close()


@pytest.fixture
def integration(source, sink, api, state):
    from makia_integration.integration import Integration

    sync_api, run_api = api
    return Integration(
        source,
        sink,
        sync_api=sync_api,
        run_api=run_api,
        state=state,
        storage_connection_id="conn-1",
    )
