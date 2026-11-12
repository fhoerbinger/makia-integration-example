from __future__ import annotations

import json

import pytest
from fake_makia import RUN_ACCOUNT_ID, progress, row

from makia_integration.api import ApiError
from makia_integration.db import get_state, rows


def results(integration, run_id="run-1", view="mapping"):
    return rows(
        integration.conn,
        "SELECT unit_key, review_state, review_option, data_json FROM integration_results "
        "WHERE run_id = ? AND view_key = ? ORDER BY unit_key",
        run_id,
        view,
    )


# --- sync ---------------------------------------------------------------------


def test_sync_sends_full_state_with_the_run_account(integration, makia):
    integration.sync()
    p = makia.projects["2026-114"]
    assert p["name"] == "Engagement 2026-114" and p["archived"] is False
    assert {"service_account": RUN_ACCOUNT_ID, "permissions": ["cases", "run", "review"]} in p[
        "members"
    ]
    assert {"subject": "uid=a.huber,ou=people", "permissions": ["review", "cases"]} in p["members"]
    assert makia.projects["2025-087"]["archived"] is True


def test_sync_twice_changes_nothing(integration, makia):
    integration.sync()
    before = dict(makia.projects)
    integration.sync()
    assert makia.projects == before


def test_project_gone_from_source_is_not_archived_by_absence(integration, makia, caplog):
    integration.sync()
    integration.conn.execute("DELETE FROM src_projects WHERE external_id = '2026-114'")
    integration.sync()
    assert makia.projects["2026-114"]["archived"] is False
    assert "no longer in the source" in caplog.text


# --- send ---------------------------------------------------------------------


def test_send_starts_one_run_per_record_and_stores_the_link(integration, makia):
    integration.sync()
    integration.send()
    sent = rows(
        integration.conn,
        "SELECT record_external_id, run_id, review_url, status FROM integration_runs ORDER BY 1",
    )
    assert [r[0] for r in sent] == ["DOC-77812", "DOC-77813"]
    assert all(r[3] == "sent" and r[2].startswith("https://studio.example/") for r in sent)
    integration.send()  # nothing new
    assert sum(1 for c in makia.calls if c[1].startswith("/inbound/")) == 2


def test_lost_answer_is_recovered_by_sending_again(integration, makia):
    integration.sync()
    integration.send()
    integration.conn.execute("DELETE FROM integration_runs WHERE record_external_id = 'DOC-77812'")
    integration.send()
    run_id = rows(
        integration.conn,
        "SELECT run_id FROM integration_runs WHERE record_external_id = 'DOC-77812'",
    )[0][0]
    assert run_id == "run-1"
    assert len(makia.cases) == 2


def test_archived_project_marks_the_record_refused(integration, makia):
    integration.sync()
    makia.projects["2026-114"]["archived"] = True
    integration.send()
    assert rows(integration.conn, "SELECT DISTINCT status, error_code FROM integration_runs") == [
        ("refused", "project_archived")
    ]


def test_sync_token_is_refused_by_inbound(makia):
    from fake_makia import SYNC_SECRET

    from makia_integration.api import MakiaClient

    client = MakiaClient("https://makia.example", SYNC_SECRET, transport=makia.transport())
    with pytest.raises(ApiError) as err:
        client.send_record("2026-114", "invoice-check", external_id="X", sources=[])
    assert err.value.code == "token_scope"


# --- poll ---------------------------------------------------------------------


def test_first_poll_starts_at_the_head(integration, makia):
    makia.emit("run.succeeded", run_id="old", status="succeeded")
    integration.poll()
    assert get_state(integration.conn, "feed_marker") == "1"


def test_a_run_finished_before_the_first_poll_is_not_missed(integration, makia):
    makia.emit("run.succeeded", run_id="old", status="succeeded")
    integration.sync()
    integration.send()  # a new integration stores the head before its first send
    makia.emit("run.failed", run_id="run-1", status="failed", error_code=None)
    integration.poll()
    assert rows(integration.conn, "SELECT status FROM integration_runs WHERE run_id = 'run-1'") == [
        ("failed",)
    ]


def started(integration, makia):
    integration.sync()
    integration.send()
    integration.poll()  # marker at the head


def test_views_built_copies_the_results(integration, makia):
    started(integration, makia)
    makia.set_rows("run-1", "mapping", [row("a"), row("b")])
    makia.emit("run.succeeded", run_id="run-1", status="succeeded")
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    integration.poll()
    assert [r[0] for r in results(integration)] == ["a", "b"]
    assert rows(integration.conn, "SELECT status FROM integration_runs WHERE run_id = 'run-1'") == [
        ("succeeded",)
    ]
    assert get_state(integration.conn, "feed_marker") == "2"


def test_failed_inbound_run_keeps_its_reason(integration, makia):
    started(integration, makia)
    makia.emit("run.failed", run_id="run-2", status="failed", error_code="inbound_import_empty")
    integration.poll()
    assert rows(
        integration.conn, "SELECT status, error_code FROM integration_runs WHERE run_id = 'run-2'"
    ) == [("failed", "inbound_import_empty")]


def test_review_is_read_again(integration, makia):
    started(integration, makia)
    makia.set_rows("run-1", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    integration.poll()
    makia.set_rows("run-1", "mapping", [row("a", state="decided", option="cleared")])
    makia.emit("row.reviewed", run_id="run-1", view="mapping", row_key="a", verdict="cleared")
    integration.poll()
    assert results(integration)[0][1:3] == ("decided", "cleared")


def test_match_decision_rereads_every_run_of_the_case(integration, makia):
    started(integration, makia)
    makia.set_rows("run-1", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    integration.poll()
    makia.set_rows("run-1", "mapping", [row("a", konto="9999")])
    makia.emit(
        "match.changed",
        case_id="case-1",
        run_id=None,
        document_id="d",
        booking_id="b",
        verdict="confirm",
    )
    integration.poll()
    assert json.loads(results(integration)[0][3]) == {"konto": "9999"}


def test_paging_and_a_rebuild_mid_read(integration, makia):
    started(integration, makia)
    makia.set_rows("run-1", "mapping", [row(f"r{i:03}") for i in range(1200)])
    makia.rebuild_once.add(("run-1", "mapping"))
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    integration.poll()
    assert len(results(integration)) == 1200


def test_rows_a_rebuild_dropped_are_removed(integration, makia):
    started(integration, makia)
    makia.set_rows("run-1", "mapping", [row("a"), row("b")], generation="g1")
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    integration.poll()
    makia.set_rows("run-1", "mapping", [row("a")], generation="g2")
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=True)
    integration.poll()
    assert [r[0] for r in results(integration)] == ["a"]


def test_runs_of_other_systems_are_ignored(integration, makia):
    started(integration, makia)
    makia.set_rows("foreign", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="foreign", views=["mapping"], rebuilt=False)
    integration.poll()
    assert results(integration, run_id="foreign") == []


def test_expired_access_token_is_swapped_again(integration, makia):
    started(integration, makia)
    swaps = makia.swaps
    makia.expire_next = True
    makia.emit("run.cancelled", run_id="run-1", status="cancelled")
    integration.poll()
    assert makia.swaps == swaps + 1
    assert rows(integration.conn, "SELECT status FROM integration_runs WHERE run_id = 'run-1'") == [
        ("cancelled",)
    ]


def test_feed_is_drained_across_pages(integration, makia):
    started(integration, makia)
    for _ in range(250):
        makia.emit("finding.changed", run_id="run-1", finding_id="f", status="confirmed")
    integration.poll()
    assert get_state(integration.conn, "feed_marker") == "250"


# --- run progress -------------------------------------------------------------


def status_of(integration, run_id="run-1"):
    return rows(
        integration.conn,
        "SELECT status, progress_json, progress_at FROM integration_runs WHERE run_id = ?",
        run_id,
    )[0]


def test_progress_marks_a_sent_run_running(integration, makia):
    started(integration, makia)
    makia.progress["run-1"] = progress("run-1", "running", done=3, count=10)
    integration.poll()
    status, stored, at = status_of(integration)
    assert status == "running" and at
    assert json.loads(stored)["running"] == [
        {"node_id": "extract", "items_done": 3, "item_count": 10}
    ]
    # run-2 has no snapshot yet (404): left alone.
    assert status_of(integration, "run-2")[0] == "sent"


def recovered(integration, makia, status):
    """DOC-77812's case: the run ended before this integration's marker (an integration
    whose database was lost sends the record again and gets the known run), so
    the feed never brings its outcome."""
    makia.cases["DOC-77812"] = {
        "case_id": "case-1",
        "run_id": "run-1",
        "status_url": "/runs/run-1",
        "review_url": "https://studio.example/runs/run-1",
    }
    makia.emit("run.succeeded", run_id="run-1", status=status)  # before the marker
    integration.sync()
    integration.send()  # 200: the known case and its run
    makia.progress["run-1"] = progress("run-1", status)


def test_a_known_run_that_already_succeeded_is_caught_up(integration, makia):
    makia.set_rows("run-1", "mapping", [row("a"), row("b")])
    recovered(integration, makia, "succeeded")
    integration.poll()
    assert rows(integration.conn, "SELECT status FROM integration_runs WHERE run_id = 'run-1'") == [
        ("succeeded",)
    ]
    assert [r[0] for r in results(integration)] == ["a", "b"]
    integration.poll()  # a finished run is not read again
    assert makia.calls.count(("GET", "/runs/run-1/progress")) == 1


def test_a_known_run_that_already_failed_is_caught_up(integration, makia):
    recovered(integration, makia, "failed")
    integration.poll()
    assert rows(integration.conn, "SELECT status FROM integration_runs WHERE run_id = 'run-1'") == [
        ("failed",)
    ]
    assert ("GET", "/runs/run-1/views") not in makia.calls


def test_final_status_not_overwritten_by_progress(integration, makia):
    started(integration, makia)
    makia.progress["run-1"] = progress("run-1", "running")
    makia.emit("run.succeeded", run_id="run-1", status="succeeded")
    integration.poll()  # feed first: succeeded; the open-run query then skips run-1
    assert status_of(integration)[0] == "succeeded"
    # A progress read that was already in flight (run-1 looked open) answers "running":
    # the UPDATE's guard keeps the finished run finished.
    integration.run_api.run_progress = lambda run_id: progress(run_id, "running")
    real_rows = rows

    import makia_integration.jobs as jobs

    jobs.rows = lambda conn, sql, *p: (
        [("run-1",)] if "progress_at" in sql else real_rows(conn, sql, *p)
    )
    try:
        integration._read_progress()
    finally:
        jobs.rows = real_rows
    assert status_of(integration)[0] == "succeeded"


def test_failing_progress_reads_rotate_to_the_back(integration, makia):
    started(integration, makia)  # no snapshots in the fake: both reads 404
    integration.poll()
    assert all(at for (at,) in rows(integration.conn, "SELECT progress_at FROM integration_runs"))


def test_at_most_50_progress_reads_per_poll(integration, makia):
    started(integration, makia)
    for n in range(60):
        integration.conn.execute(
            "INSERT INTO integration_runs (record_external_id, run_id, status) VALUES (?, ?, 'sent')",
            (f"X{n}", f"bulk-{n}"),
        )
        makia.progress[f"bulk-{n}"] = progress(f"bulk-{n}")
    makia.calls.clear()
    integration.poll()
    reads = [c for c in makia.calls if c[1].endswith("/progress")]
    assert len(reads) == 50
    # The next poll starts with the runs not read yet.
    makia.calls.clear()
    integration.poll()
    again = {c[1] for c in makia.calls if c[1].endswith("/progress")}
    assert len(again) == 50 and sum(1 for p in again if p.startswith("/runs/bulk-")) >= 12


# --- send from a folder -------------------------------------------------------


@pytest.fixture
def files_dir(tmp_path):
    folder = tmp_path / "belege"
    (folder / "2026" / "114").mkdir(parents=True)
    for n in (1, 2, 3):
        (folder / "2026" / "114" / f"invoice-{n}.pdf").write_bytes(f"%PDF-{n}".encode())
    return folder


def inbound_calls(makia):
    return [c for c in makia.calls if c[1].startswith("/inbound/")]


def test_folder_mode_posts_multipart_with_external_id_first(integration, makia):
    integration.sync()
    integration.send()
    first, second = makia.uploads
    assert first[0] == ("external_id", "", b"DOC-77812")
    assert first[1:] == [
        ("invoices", "invoice-1.pdf", b"%PDF-1"),
        ("invoices", "invoice-2.pdf", b"%PDF-2"),
    ]
    assert second == [
        ("external_id", "", b"DOC-77813"),
        ("invoices", "invoice-3.pdf", b"%PDF-3"),
    ]
    sent = rows(
        integration.conn, "SELECT record_external_id, status FROM integration_runs ORDER BY 1"
    )
    assert sent == [("DOC-77812", "sent"), ("DOC-77813", "sent")]


def test_a_missing_file_skips_the_record_and_the_next_still_goes(
    integration, makia, files_dir, caplog
):
    integration.sync()
    (files_dir / "2026" / "114" / "invoice-2.pdf").unlink()
    integration.send()
    assert len(makia.uploads) == 1 and makia.uploads[0][0][2] == b"DOC-77813"
    assert rows(integration.conn, "SELECT record_external_id FROM integration_runs") == [
        ("DOC-77813",)
    ]
    assert "DOC-77812" in caplog.text and "invoice-2.pdf" in caplog.text


def test_a_file_over_the_cap_is_refused_without_a_request(
    integration, makia, files_dir, monkeypatch
):
    from makia_integration import jobs

    monkeypatch.setattr(jobs, "MAX_FILE_BYTES", 6)
    (files_dir / "2026" / "114" / "invoice-3.pdf").write_bytes(b"%PDF-too-big")
    integration.sync()
    integration.send()
    assert rows(
        integration.conn,
        "SELECT record_external_id, status, error_code FROM integration_runs ORDER BY 1",
    ) == [("DOC-77812", "sent", None), ("DOC-77813", "refused", "file_too_large")]
    assert len(inbound_calls(makia)) == 1


def test_a_413_file_too_large_answer_marks_the_record_refused(integration, makia):
    makia.inbound_status, makia.inbound_code = 413, "file_too_large"
    integration.sync()
    integration.send()
    assert rows(integration.conn, "SELECT status, error_code FROM integration_runs") == [
        ("refused", "file_too_large"),
        ("refused", "file_too_large"),
    ]


def test_a_415_answer_marks_that_record_refused_and_the_next_still_goes(integration, makia):
    from makia_integration.api import ApiError

    real = integration.run_api.send_record_files
    calls = []

    def flaky(*a, **kw):
        calls.append(kw["external_id"])
        if len(calls) == 1:
            raise ApiError(415, "unsupported_media_type")
        return real(*a, **kw)

    integration.run_api.send_record_files = flaky
    integration.sync()
    integration.send()
    assert rows(
        integration.conn,
        "SELECT record_external_id, status, error_code FROM integration_runs ORDER BY 1",
    ) == [("DOC-77812", "refused", "unsupported_media_type"), ("DOC-77813", "sent", None)]


def test_an_unreadable_file_skips_the_record_and_the_next_still_goes(
    integration, makia, files_dir, caplog
):
    import os

    target = files_dir / "2026" / "114" / "invoice-1.pdf"
    target.chmod(0)
    try:
        if os.access(target, os.R_OK):
            pytest.skip("running as a user that ignores file modes")
        integration.sync()
        integration.send()
    finally:
        target.chmod(0o644)
    assert len(makia.uploads) == 1 and makia.uploads[0][0][2] == b"DOC-77813"
    assert rows(integration.conn, "SELECT record_external_id FROM integration_runs") == [
        ("DOC-77813",)
    ]
    assert "DOC-77812" in caplog.text and "not readable" in caplog.text


def test_an_empty_file_is_refused_without_a_request(integration, makia, files_dir):
    (files_dir / "2026" / "114" / "invoice-3.pdf").write_bytes(b"")
    integration.sync()
    integration.send()
    assert rows(
        integration.conn,
        "SELECT record_external_id, status, error_code FROM integration_runs ORDER BY 1",
    ) == [("DOC-77812", "sent", None), ("DOC-77813", "refused", "empty_file")]
    assert len(inbound_calls(makia)) == 1


def test_a_path_leaving_the_folder_is_never_read(integration, makia, files_dir, tmp_path, caplog):
    (tmp_path / "x.pdf").write_bytes(b"secret")
    integration.conn.execute(
        "UPDATE src_record_files SET path = '../x.pdf' WHERE path = '2026/114/invoice-3.pdf'"
    )
    integration.conn.execute(
        "INSERT INTO src_record_files (record_external_id, path) "
        f"VALUES ('DOC-77812', '{tmp_path}/x.pdf')"
    )
    integration.sync()
    integration.send()
    assert makia.uploads == []
    assert b"secret" not in b"".join(p[2] for u in makia.uploads for p in u)
    assert "DOC-77813" in caplog.text


def test_without_a_folder_the_body_is_json_by_reference(integration, makia):
    # the plain `integration` fixture (files_dir None) is exercised by the tests above;
    # this one pins that no multipart is sent in that mode
    integration.files_dir = None
    integration.sync()
    integration.send()
    assert makia.uploads == [] and len(makia.cases) == 2
