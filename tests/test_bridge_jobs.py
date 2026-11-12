from __future__ import annotations

import json

import pytest
from fake_makia import RUN_ACCOUNT_ID, progress, row

from makia_bridge.api import ApiError
from makia_bridge.db import get_state, rows


def results(bridge, run_id="run-1", view="mapping"):
    return rows(
        bridge.conn,
        "SELECT unit_key, review_state, review_option, data_json FROM bridge_results "
        "WHERE run_id = ? AND view_key = ? ORDER BY unit_key",
        run_id,
        view,
    )


# --- sync ---------------------------------------------------------------------


def test_sync_sends_full_state_with_the_run_account(bridge, makia):
    bridge.sync()
    p = makia.projects["2026-114"]
    assert p["name"] == "Engagement 2026-114" and p["archived"] is False
    assert {"service_account": RUN_ACCOUNT_ID, "permissions": ["cases", "run", "review"]} in p[
        "members"
    ]
    assert {"subject": "uid=a.huber,ou=people", "permissions": ["review", "cases"]} in p["members"]
    assert makia.projects["2025-087"]["archived"] is True


def test_sync_twice_changes_nothing(bridge, makia):
    bridge.sync()
    before = dict(makia.projects)
    bridge.sync()
    assert makia.projects == before


def test_project_gone_from_source_is_not_archived_by_absence(bridge, makia, caplog):
    bridge.sync()
    bridge.conn.execute("DELETE FROM src_projects WHERE external_id = '2026-114'")
    bridge.sync()
    assert makia.projects["2026-114"]["archived"] is False
    assert "no longer in the source" in caplog.text


# --- send ---------------------------------------------------------------------


def test_send_starts_one_run_per_record_and_stores_the_link(bridge, makia):
    bridge.sync()
    bridge.send()
    sent = rows(
        bridge.conn,
        "SELECT record_external_id, run_id, review_url, status FROM bridge_runs ORDER BY 1",
    )
    assert [r[0] for r in sent] == ["DOC-77812", "DOC-77813"]
    assert all(r[3] == "sent" and r[2].startswith("https://studio.example/") for r in sent)
    bridge.send()  # nothing new
    assert sum(1 for c in makia.calls if c[1].startswith("/inbound/")) == 2


def test_lost_answer_is_recovered_by_sending_again(bridge, makia):
    bridge.sync()
    bridge.send()
    bridge.conn.execute("DELETE FROM bridge_runs WHERE record_external_id = 'DOC-77812'")
    bridge.send()
    run_id = rows(
        bridge.conn, "SELECT run_id FROM bridge_runs WHERE record_external_id = 'DOC-77812'"
    )[0][0]
    assert run_id == "run-1"
    assert len(makia.cases) == 2


def test_archived_project_marks_the_record_refused(bridge, makia):
    bridge.sync()
    makia.projects["2026-114"]["archived"] = True
    bridge.send()
    assert rows(bridge.conn, "SELECT DISTINCT status, error_code FROM bridge_runs") == [
        ("refused", "project_archived")
    ]


def test_sync_token_is_refused_by_inbound(makia):
    from fake_makia import SYNC_SECRET

    from makia_bridge.api import MakiaClient

    client = MakiaClient("https://makia.example", SYNC_SECRET, transport=makia.transport())
    with pytest.raises(ApiError) as err:
        client.send_record("2026-114", "invoice-check", external_id="X", sources=[])
    assert err.value.code == "token_scope"


# --- poll ---------------------------------------------------------------------


def test_first_poll_starts_at_the_head(bridge, makia):
    makia.emit("run.succeeded", run_id="old", status="succeeded")
    bridge.poll()
    assert get_state(bridge.conn, "feed_marker") == "1"


def test_a_run_finished_before_the_first_poll_is_not_missed(bridge, makia):
    makia.emit("run.succeeded", run_id="old", status="succeeded")
    bridge.sync()
    bridge.send()  # a new bridge stores the head before its first send
    makia.emit("run.failed", run_id="run-1", status="failed", error_code=None)
    bridge.poll()
    assert rows(bridge.conn, "SELECT status FROM bridge_runs WHERE run_id = 'run-1'") == [
        ("failed",)
    ]


def started(bridge, makia):
    bridge.sync()
    bridge.send()
    bridge.poll()  # marker at the head


def test_views_built_copies_the_results(bridge, makia):
    started(bridge, makia)
    makia.set_rows("run-1", "mapping", [row("a"), row("b")])
    makia.emit("run.succeeded", run_id="run-1", status="succeeded")
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    bridge.poll()
    assert [r[0] for r in results(bridge)] == ["a", "b"]
    assert rows(bridge.conn, "SELECT status FROM bridge_runs WHERE run_id = 'run-1'") == [
        ("succeeded",)
    ]
    assert get_state(bridge.conn, "feed_marker") == "2"


def test_failed_inbound_run_keeps_its_reason(bridge, makia):
    started(bridge, makia)
    makia.emit("run.failed", run_id="run-2", status="failed", error_code="inbound_import_empty")
    bridge.poll()
    assert rows(
        bridge.conn, "SELECT status, error_code FROM bridge_runs WHERE run_id = 'run-2'"
    ) == [("failed", "inbound_import_empty")]


def test_review_is_read_again(bridge, makia):
    started(bridge, makia)
    makia.set_rows("run-1", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    bridge.poll()
    makia.set_rows("run-1", "mapping", [row("a", state="decided", option="cleared")])
    makia.emit("row.reviewed", run_id="run-1", view="mapping", row_key="a", verdict="cleared")
    bridge.poll()
    assert results(bridge)[0][1:3] == ("decided", "cleared")


def test_match_decision_rereads_every_run_of_the_case(bridge, makia):
    started(bridge, makia)
    makia.set_rows("run-1", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    bridge.poll()
    makia.set_rows("run-1", "mapping", [row("a", konto="9999")])
    makia.emit(
        "match.changed",
        case_id="case-1",
        run_id=None,
        document_id="d",
        booking_id="b",
        verdict="confirm",
    )
    bridge.poll()
    assert json.loads(results(bridge)[0][3]) == {"konto": "9999"}


def test_paging_and_a_rebuild_mid_read(bridge, makia):
    started(bridge, makia)
    makia.set_rows("run-1", "mapping", [row(f"r{i:03}") for i in range(1200)])
    makia.rebuild_once.add(("run-1", "mapping"))
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    bridge.poll()
    assert len(results(bridge)) == 1200


def test_rows_a_rebuild_dropped_are_removed(bridge, makia):
    started(bridge, makia)
    makia.set_rows("run-1", "mapping", [row("a"), row("b")], generation="g1")
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    bridge.poll()
    makia.set_rows("run-1", "mapping", [row("a")], generation="g2")
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=True)
    bridge.poll()
    assert [r[0] for r in results(bridge)] == ["a"]


def test_runs_of_other_systems_are_ignored(bridge, makia):
    started(bridge, makia)
    makia.set_rows("foreign", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="foreign", views=["mapping"], rebuilt=False)
    bridge.poll()
    assert results(bridge, run_id="foreign") == []


def test_expired_access_token_is_swapped_again(bridge, makia):
    started(bridge, makia)
    swaps = makia.swaps
    makia.expire_next = True
    makia.emit("run.cancelled", run_id="run-1", status="cancelled")
    bridge.poll()
    assert makia.swaps == swaps + 1
    assert rows(bridge.conn, "SELECT status FROM bridge_runs WHERE run_id = 'run-1'") == [
        ("cancelled",)
    ]


def test_feed_is_drained_across_pages(bridge, makia):
    started(bridge, makia)
    for _ in range(250):
        makia.emit("finding.changed", run_id="run-1", finding_id="f", status="confirmed")
    bridge.poll()
    assert get_state(bridge.conn, "feed_marker") == "250"


# --- run progress -------------------------------------------------------------


def status_of(bridge, run_id="run-1"):
    return rows(
        bridge.conn,
        "SELECT status, progress_json, progress_at FROM bridge_runs WHERE run_id = ?",
        run_id,
    )[0]


def test_progress_marks_a_sent_run_running(bridge, makia):
    started(bridge, makia)
    makia.progress["run-1"] = progress("run-1", "running", done=3, count=10)
    bridge.poll()
    status, stored, at = status_of(bridge)
    assert status == "running" and at
    assert json.loads(stored)["running"] == [
        {"node_id": "extract", "items_done": 3, "item_count": 10}
    ]
    # run-2 has no snapshot yet (404): left alone.
    assert status_of(bridge, "run-2")[0] == "sent"


def recovered(bridge, makia, status):
    """DOC-77812's case: the run ended before this bridge's marker (a bridge
    whose database was lost sends the record again and gets the known run), so
    the feed never brings its outcome."""
    makia.cases["DOC-77812"] = {
        "case_id": "case-1",
        "run_id": "run-1",
        "status_url": "/runs/run-1",
        "review_url": "https://studio.example/runs/run-1",
    }
    makia.emit("run.succeeded", run_id="run-1", status=status)  # before the marker
    bridge.sync()
    bridge.send()  # 200: the known case and its run
    makia.progress["run-1"] = progress("run-1", status)


def test_a_known_run_that_already_succeeded_is_caught_up(bridge, makia):
    makia.set_rows("run-1", "mapping", [row("a"), row("b")])
    recovered(bridge, makia, "succeeded")
    bridge.poll()
    assert rows(bridge.conn, "SELECT status FROM bridge_runs WHERE run_id = 'run-1'") == [
        ("succeeded",)
    ]
    assert [r[0] for r in results(bridge)] == ["a", "b"]
    bridge.poll()  # a finished run is not read again
    assert makia.calls.count(("GET", "/runs/run-1/progress")) == 1


def test_a_known_run_that_already_failed_is_caught_up(bridge, makia):
    recovered(bridge, makia, "failed")
    bridge.poll()
    assert rows(bridge.conn, "SELECT status FROM bridge_runs WHERE run_id = 'run-1'") == [
        ("failed",)
    ]
    assert ("GET", "/runs/run-1/views") not in makia.calls


def test_final_status_not_overwritten_by_progress(bridge, makia):
    started(bridge, makia)
    makia.progress["run-1"] = progress("run-1", "running")
    makia.emit("run.succeeded", run_id="run-1", status="succeeded")
    bridge.poll()  # feed first: succeeded; the open-run query then skips run-1
    assert status_of(bridge)[0] == "succeeded"
    # A progress read that was already in flight (run-1 looked open) answers "running":
    # the UPDATE's guard keeps the finished run finished.
    bridge.run_api.run_progress = lambda run_id: progress(run_id, "running")
    real_rows = rows

    import makia_bridge.jobs as jobs

    jobs.rows = lambda conn, sql, *p: (
        [("run-1",)] if "progress_at" in sql else real_rows(conn, sql, *p)
    )
    try:
        bridge._read_progress()
    finally:
        jobs.rows = real_rows
    assert status_of(bridge)[0] == "succeeded"


def test_failing_progress_reads_rotate_to_the_back(bridge, makia):
    started(bridge, makia)  # no snapshots in the fake: both reads 404
    bridge.poll()
    assert all(at for (at,) in rows(bridge.conn, "SELECT progress_at FROM bridge_runs"))


def test_at_most_50_progress_reads_per_poll(bridge, makia):
    started(bridge, makia)
    for n in range(60):
        bridge.conn.execute(
            "INSERT INTO bridge_runs (record_external_id, run_id, status) VALUES (?, ?, 'sent')",
            (f"X{n}", f"bulk-{n}"),
        )
        makia.progress[f"bulk-{n}"] = progress(f"bulk-{n}")
    makia.calls.clear()
    bridge.poll()
    reads = [c for c in makia.calls if c[1].endswith("/progress")]
    assert len(reads) == 50
    # The next poll starts with the runs not read yet.
    makia.calls.clear()
    bridge.poll()
    again = {c[1] for c in makia.calls if c[1].endswith("/progress")}
    assert len(again) == 50 and sum(1 for p in again if p.startswith("/runs/bulk-")) >= 12
