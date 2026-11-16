from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
EXAMPLE = HERE.parent
# The package from source: the monorepo keeps it under packages/, the public repo beside the example.
for candidate in (
    EXAMPLE / "makia-integration-py",
    EXAMPLE.parents[1] / "packages" / "makia-integration-py",
):
    if candidate.is_dir():
        sys.path.insert(0, str(candidate))
        break
sys.path.insert(0, str(EXAMPLE))

from example_integration.db import create_schema  # noqa: E402
from makia_integration import Config, MakiaClient  # noqa: E402
from makia_integration.testing import RUN_SECRET, SYNC_SECRET, FakeMakia  # noqa: E402

URL = "https://makia.example"


@pytest.fixture
def files_dir() -> Path | None:
    """None = send by reference. Tests of the folder mode override this."""
    return None


@pytest.fixture
def makia() -> FakeMakia:
    return FakeMakia()


@pytest.fixture
def db_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "integration.db"
    conn = sqlite3.connect(path)
    create_schema(conn, "sqlite")
    conn.executescript((EXAMPLE / "sample" / "seed.sql").read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    monkeypatch.setenv("INTEGRATION_DB", str(path))
    return path


@pytest.fixture
def config(tmp_path: Path, files_dir: Path | None, makia: FakeMakia, db_path: Path, monkeypatch):
    def clients(self):
        return (
            MakiaClient(URL, SYNC_SECRET, transport=makia.transport(), sleep=lambda s: None),
            MakiaClient(URL, RUN_SECRET, transport=makia.transport(), sleep=lambda s: None),
        )

    monkeypatch.setattr(Config, "clients", clients)
    return Config(
        api_url=URL,
        sync_token=SYNC_SECRET,
        run_token=RUN_SECRET,
        storage_connection_id="conn-1",
        files_dir=files_dir,
        state_file=tmp_path / "state.db",
        ca_bundle=None,
        integration_ref=None,
        webhook_secret=None,
    )
