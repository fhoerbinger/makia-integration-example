from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).parent
sys.path[:0] = [str(HERE.parent), str(HERE)]

from fake_makia import RUN_SECRET, SYNC_SECRET, FakeMakia  # noqa: E402
from makia_bridge.api import MakiaClient  # noqa: E402
from makia_bridge.db import create_schema  # noqa: E402
from makia_bridge.jobs import Bridge  # noqa: E402


@pytest.fixture
def makia() -> FakeMakia:
    return FakeMakia()


@pytest.fixture
def bridge(makia: FakeMakia) -> Bridge:
    conn = sqlite3.connect(":memory:")
    create_schema(conn, "sqlite")
    conn.executescript((HERE.parent / "sample" / "seed.sql").read_text())
    url = "https://makia.example"
    return Bridge(
        conn=conn,
        sync_api=MakiaClient(url, SYNC_SECRET, transport=makia.transport()),
        run_api=MakiaClient(url, RUN_SECRET, transport=makia.transport()),
        storage_connection_id="conn-1",
    )
