"""The example end to end through the package: sync, send, views built, poll, review."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from example_integration.integration import integration
from makia_integration import Record, SentRun
from makia_integration.testing import FakeMakia, row


@pytest.fixture
def b(config):
    built = integration(config)
    yield built
    built.close()


def query(path: Path, sql: str, *args):
    conn = sqlite3.connect(path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def test_sync_send_poll_leaves_results_with_the_studio_link(b, makia: FakeMakia, db_path):
    assert b.sync().projects == 2
    assert b.send().sent == 2
    runs = query(db_path, "SELECT record_external_id, status, review_url FROM integration_runs")
    assert sorted(r[:2] for r in runs) == [("DOC-77812", "sent"), ("DOC-77813", "sent")]

    makia.set_rows("run-1", "overview", [row("u1"), row("u2", state="approved")])
    makia.emit("run.views_built", run_id="run-1", views=["overview"])
    makia.emit("run.succeeded", run_id="run-1", status="succeeded")
    assert b.poll().views_read == 1

    results = query(
        db_path, "SELECT unit_key, review_state, url, data_json FROM integration_results"
    )
    assert sorted(r[:2] for r in results) == [("u1", "none"), ("u2", "approved")]
    assert all(r[2].startswith("https://studio.example/") for r in results)
    assert all(isinstance(json.loads(r[3]), dict) for r in results)
    status = query(db_path, "SELECT status FROM integration_runs WHERE run_id = 'run-1'")
    assert status == [("succeeded",)]


def test_a_review_rereads_and_a_rebuild_drops_rows(b, makia: FakeMakia, db_path):
    b.sync()
    b.send()
    makia.set_rows("run-1", "overview", [row("u1"), row("u2")])
    makia.emit("run.views_built", run_id="run-1", views=["overview"])
    b.poll()
    makia.set_rows("run-1", "overview", [row("u1", state="approved")], generation="g2")
    makia.emit("run.views_built", run_id="run-1", views=["overview"])
    b.poll()
    assert query(db_path, "SELECT unit_key, generation, review_state FROM integration_results") == [
        ("u1", "g2", "approved")
    ]


def test_a_refusal_is_final_for_the_record(b, makia: FakeMakia, db_path):
    makia.inbound_status = 422
    makia.inbound_code = "unknown_input"
    b.sync()
    assert b.send().sent == 0
    runs = query(db_path, "SELECT status, error_code FROM integration_runs")
    assert runs == [("refused", "unknown_input")] * 2


def test_the_state_file_is_released_when_building_fails(config, monkeypatch):
    import example_integration.integration as module

    def broken(url):
        raise RuntimeError("no database")

    monkeypatch.setattr(module, "connect", broken)
    with pytest.raises(RuntimeError):
        integration(config)
    config.open_state().close()  # would raise StateLocked if the first attempt kept it


def test_a_repeated_run_sent_keeps_the_final_status(b, makia: FakeMakia, db_path):
    b.sync()
    b.send()
    makia.emit("run.failed", run_id="run-1", status="failed", error_code="x")
    b.poll()
    record = Record("DOC-77812", "2026-114", "invoice-check", "invoices")
    b.sink.run_sent(record, SentRun("case-1", "run-1", "https://studio.example/new", True))
    assert query(
        db_path,
        "SELECT status, review_url FROM integration_runs WHERE record_external_id = 'DOC-77812'",
    ) == [("failed", "https://studio.example/new")]


def test_closing_the_integration_closes_the_database(config):
    built = integration(config)
    built.close()
    with pytest.raises(sqlite3.ProgrammingError):
        built.sink.conn.cursor()
    config.open_state().close()  # the state file was released too
