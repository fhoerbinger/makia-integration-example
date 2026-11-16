from __future__ import annotations

import sqlite3

import pytest

from makia_integration.state import RunEntry, SqliteStateStore, StateLocked


def entry(ext: str, **kw) -> RunEntry:
    base = {
        "record_external_id": ext,
        "case_id": f"case-{ext}",
        "run_id": f"run-{ext}",
        "review_url": None,
        "status": "sent",
        "error_code": None,
        "sink_pending": False,
        "progress_at": None,
    }
    base.update(kw)
    return RunEntry(**base)


def test_marker_roundtrip(state):
    assert state.marker() is None
    state.set_marker(7)
    assert state.marker() == 7
    state.set_marker(9)
    assert state.marker() == 9


def test_upsert_and_lookups(state):
    state.upsert_run(entry("a", case_id="c1"))
    state.upsert_run(entry("b", case_id="c1"))
    assert state.run_by_record("a").run_id == "run-a"
    assert state.run_by_id("run-b").record_external_id == "b"
    assert state.run_by_record("zz") is None
    assert state.run_by_id("nope") is None
    assert [e.record_external_id for e in state.runs_of_case("c1")] == ["a", "b"]
    state.upsert_run(entry("a", case_id="c1", status="running", review_url="u"))
    got = state.run_by_record("a")
    assert (got.status, got.review_url) == ("running", "u")


def test_pending_sink(state):
    state.upsert_run(entry("a", sink_pending=True))
    state.upsert_run(entry("b"))
    assert [e.record_external_id for e in state.pending_sink()] == ["a"]


def test_open_runs_order_and_limit(state):
    state.upsert_run(entry("a", progress_at="2026-01-02T00:00:00Z"))
    state.upsert_run(entry("b", progress_at="2026-01-01T00:00:00Z", status="running"))
    state.upsert_run(entry("c", progress_at=None))
    state.upsert_run(entry("d", progress_at=None))
    state.upsert_run(entry("e", status="succeeded"))
    state.upsert_run(entry("f", run_id=None))
    got = [e.record_external_id for e in state.open_runs(10)]
    assert got == ["c", "d", "b", "a"]
    assert [e.record_external_id for e in state.open_runs(2)] == ["c", "d"]


def test_set_status_only_open_never_moves_finished_back(state):
    state.upsert_run(entry("a", status="succeeded"))
    state.upsert_run(entry("b", status="sent"))
    state.set_status("run-a", "running", None, only_open=True)
    state.set_status("run-b", "running", None, only_open=True)
    assert state.run_by_id("run-a").status == "succeeded"
    assert state.run_by_id("run-b").status == "running"
    state.set_status("run-a", "failed", "boom", only_open=False)
    got = state.run_by_id("run-a")
    assert (got.status, got.error_code) == ("failed", "boom")


def test_touch_progress_and_views(state):
    state.upsert_run(entry("a"))
    state.touch_progress("run-a", "2026-01-01T00:00:00Z")
    assert state.run_by_id("run-a").progress_at == "2026-01-01T00:00:00Z"
    assert state.views_read("run-a") == []
    state.mark_view_read("run-a", "v1")
    state.mark_view_read("run-a", "v1")
    state.mark_view_read("run-a", "v0")
    assert state.views_read("run-a") == ["v0", "v1"]


def test_clear_refusals_one_and_all(state):
    state.upsert_run(entry("a", status="refused_final"))
    state.upsert_run(entry("b", status="refused_final"))
    state.upsert_run(entry("c", status="refused"))
    assert state.clear_refusals("a") == ["a"]
    assert state.run_by_record("a") is None
    assert state.clear_refusals("c") == []
    assert state.run_by_record("c").status == "refused"
    assert state.clear_refusals(None) == ["b"]
    assert state.run_by_record("b") is None
    assert state.run_by_record("c") is not None


def test_rollback_on_error(state):
    with pytest.raises(ValueError):
        with state.transaction():
            state.upsert_run(entry("a"))
            raise ValueError("x")
    assert state.run_by_record("a") is None


def test_nested_transaction_does_not_commit_early(state, tmp_path):
    with pytest.raises(ValueError):
        with state.transaction():
            with state.transaction():
                state.upsert_run(entry("a"))
            state.upsert_run(entry("b"))
            raise ValueError("x")
    assert state.run_by_record("a") is None
    assert state.run_by_record("b") is None
    with state.transaction():
        with state.transaction():
            state.upsert_run(entry("c"))
    other = sqlite3.connect(tmp_path / "state.db")
    assert other.execute("SELECT count(*) FROM runs").fetchone()[0] == 1
    other.close()


def test_mutations_commit_outside_transaction(state, tmp_path):
    state.set_marker(3)
    other = sqlite3.connect(tmp_path / "state.db")
    assert other.execute("SELECT value FROM meta WHERE key='marker'").fetchone() is not None
    other.close()


def test_second_store_on_same_file_is_locked(state, tmp_path):
    with pytest.raises(StateLocked) as ei:
        SqliteStateStore(tmp_path / "state.db")
    assert "state.db" in str(ei.value)
    assert "another makia-integration process" in str(ei.value)


def test_lock_released_on_close(tmp_path):
    first = SqliteStateStore(tmp_path / "state.db")
    first.close()
    second = SqliteStateStore(tmp_path / "state.db")
    second.close()


def test_schema_version_row(tmp_path):
    path = tmp_path / "state.db"
    SqliteStateStore(path).close()
    db = sqlite3.connect(path)
    assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    db.execute("UPDATE meta SET value='5' WHERE key='schema_version'")
    db.commit()
    db.close()
    with pytest.raises(RuntimeError) as ei:
        SqliteStateStore(path)
    assert "5" in str(ei.value) and "2" in str(ei.value)
    # the failed open must not leave the lock held
    db = sqlite3.connect(path)
    db.execute("UPDATE meta SET value='2' WHERE key='schema_version'")
    db.commit()
    db.close()
    SqliteStateStore(path).close()


def test_path_with_spaces_and_missing_parent(tmp_path):
    store = SqliteStateStore(tmp_path / "a b" / "state.db")
    store.set_marker(1)
    store.close()
    assert (tmp_path / "a b" / "state.db").exists()


def test_clear_refusals_one_clears_gone_all_does_not(state):
    state.upsert_run(entry("a", status="gone", error_code="run_not_found"))
    state.upsert_run(entry("b", status="gone", error_code="run_not_found"))
    state.upsert_run(entry("c", status="succeeded"))
    state.upsert_run(entry("d", status="no_run"))
    assert state.clear_refusals("a") == ["a"]
    assert state.run_by_record("a") is None
    assert state.clear_refusals(None) == ["d"]  # --all-refused: gone stays
    assert state.run_by_record("b").status == "gone"
    assert state.clear_gone() == ["b"]
    assert state.run_by_record("b") is None
    assert state.run_by_record("c").status == "succeeded"


def test_drop_pending_deletes_only_a_pending_entry(state):
    state.upsert_run(entry("a", sink_pending=True))
    state.upsert_run(entry("b"))
    assert state.drop_pending("b") is False and state.run_by_record("b") is not None
    assert state.drop_pending("a") is True and state.run_by_record("a") is None
    assert state.drop_pending("zz") is False


def test_open_runs_skips_sink_pending(state):
    state.upsert_run(entry("a", sink_pending=True))
    state.upsert_run(entry("b"))
    assert [e.record_external_id for e in state.open_runs(1)] == ["b"]


def test_entry_keeps_the_record_fields(state):
    state.upsert_run(entry("a", project_external_id="p", workflow_slug="w", input_id="i"))
    got = state.run_by_record("a")
    assert (got.project_external_id, got.workflow_slug, got.input_id) == ("p", "w", "i")


def test_version_1_file_is_migrated(tmp_path):
    path = tmp_path / "state.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES ('schema_version', '1'), ('marker', '4');
        CREATE TABLE runs (
            record_external_id TEXT PRIMARY KEY, case_id TEXT, run_id TEXT UNIQUE,
            review_url TEXT, status TEXT NOT NULL, error_code TEXT,
            sink_pending INTEGER NOT NULL DEFAULT 0, progress_at TEXT
        );
        CREATE INDEX runs_case ON runs(case_id);
        CREATE TABLE views_read (run_id TEXT NOT NULL, view TEXT NOT NULL,
            PRIMARY KEY (run_id, view));
        INSERT INTO runs VALUES ('a', 'c', 'r', NULL, 'sent', NULL, 1, NULL);
        """
    )
    db.close()
    store = SqliteStateStore(path)
    try:
        got = store.run_by_record("a")
        assert got.run_id == "r" and got.sink_pending and got.project_external_id is None
        assert store.marker() == 4
    finally:
        store.close()
    db = sqlite3.connect(path)
    assert db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
    db.close()
