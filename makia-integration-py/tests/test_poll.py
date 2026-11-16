from __future__ import annotations

import pytest

from makia_integration.integration import PROGRESS_READS_PER_POLL, PollReport
from makia_integration.client import ApiError
from makia_integration.model import Record, RunRef
from makia_integration.state import RunEntry
from makia_integration.testing import progress, row

REC = Record("114-0001", "2026-114", "audit", "belege")
REC2 = Record("114-0002", "2026-114", "audit", "belege")


def _live(call) -> bool:
    """A progress report (run_status with a snapshot), made for every open run each poll."""
    return call[0] == "run_status" and call[1][0].progress is not None


def names(sink) -> list[str]:
    return [c[0] for c in sink.calls if not _live(c)]


def calls(sink, name):
    return [c[1] for c in sink.calls if c[0] == name and not _live(c)]


def live(sink):
    return [c[1][0] for c in sink.calls if _live(c)]


@pytest.fixture
def started(integration, source, makia, sink):
    """Two runs sent (run-1, run-2), marker at the head, sink calls cleared."""
    makia.emit("case_created")
    source._records = [REC, REC2]
    source._files = {REC.external_id: ["a.pdf"], REC2.external_id: ["b.pdf"]}
    integration.sync()
    integration.send()
    integration.poll()
    sink.calls.clear()
    return integration


def test_first_poll_starts_at_the_head(integration, makia, state, sink):
    makia.emit("run.succeeded", run_id="old", status="succeeded")
    assert integration.poll() == PollReport(0, 0, 0)
    assert state.marker() == 1
    assert sink.calls == []


def test_final_event_reports_status(started, makia, state, sink):
    makia.emit("run.failed", run_id="run-2", status="failed", error_code="inbound_import_empty")
    report = started.poll()
    assert report.events == 1
    (st,) = calls(sink, "run_status")
    assert (st[0].ref.run_id, st[0].status, st[0].error_code) == (
        "run-2",
        "failed",
        "inbound_import_empty",
    )
    assert st[0].ref.record_external_id == REC2.external_id
    assert state.run_by_id("run-2").status == "failed"
    assert state.marker() == 2


def test_run_finished_before_first_poll_is_not_missed(integration, source, makia, state, sink):
    makia.emit("run.succeeded", run_id="old", status="succeeded")
    source._records = [REC]
    source._files = {REC.external_id: ["a.pdf"]}
    integration.sync()
    integration.send()
    makia.emit("run.failed", run_id="run-1", status="failed", error_code=None)
    integration.poll()
    assert state.run_by_id("run-1").status == "failed"


def test_views_built_streams_pages_then_done(started, makia, state, sink):
    makia.set_rows("run-1", "mapping", [row(f"r{i:03}") for i in range(1200)])
    makia.emit("run.succeeded", run_id="run-1", status="succeeded")
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    report = started.poll()
    assert report.views_read == 1
    assert names(sink) == ["run_status", "result_rows", "result_rows", "result_rows", "view_done"]
    pages = calls(sink, "result_rows")
    assert [len(p[3]) for p in pages] == [500, 500, 200]
    assert {p[2] for p in pages} == {"g1"}
    assert calls(sink, "view_done")[0][1:] == ("mapping", "g1")
    assert state.views_read("run-1") == ["mapping"]


def test_generation_changed_restarts_from_first_page(started, makia, sink):
    makia.set_rows("run-1", "mapping", [row(f"r{i:03}") for i in range(1200)])
    makia.rebuild_once.add(("run-1", "mapping"))
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    started.poll()
    assert names(sink)[-1] == "view_done"
    assert names(sink).count("view_done") == 1
    # page 1, the 409 on page 2, then a full read from the top
    sizes = [len(c[3]) for c in calls(sink, "result_rows")]
    assert sizes == [500, 500, 500, 200]


def test_review_rereads_view(started, makia, sink):
    makia.set_rows("run-1", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    started.poll()
    sink.calls.clear()
    makia.set_rows("run-1", "mapping", [row("a", state="decided", option="cleared")])
    makia.emit("row.reviewed", run_id="run-1", view="mapping", row_key="a", verdict="cleared")
    started.poll()
    (page,) = calls(sink, "result_rows")
    assert (page[3][0].review_state, page[3][0].review_option) == ("decided", "cleared")


def test_match_changed_rereads_case_views(started, makia, sink):
    makia.set_rows("run-1", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    started.poll()
    sink.calls.clear()
    makia.set_rows("run-1", "mapping", [row("a", konto="9999")])
    makia.emit("match.changed", case_id="case-1", run_id=None, document_id="d", booking_id="b")
    started.poll()
    (page,) = calls(sink, "result_rows")
    assert page[0].run_id == "run-1" and page[3][0].data == {"konto": "9999"}


def test_progress_marks_running(started, makia, state, sink):
    makia.progress["run-1"] = progress("run-1", "running", done=3, count=10)
    started.poll()
    one, two = live(sink)  # run-2 is still queued: it stays `sent`
    assert one.status == "running" and one.ref.run_id == "run-1"
    assert one.progress["running"] == [{"node_id": "extract", "items_done": 3, "item_count": 10}]
    assert (two.ref.run_id, two.status) == ("run-2", "sent")
    entry = state.run_by_id("run-1")
    assert entry.status == "running" and entry.progress_at
    assert state.run_by_id("run-2").status == "sent"


def test_failing_progress_reads_rotate_to_the_back(started, state, makia):
    makia.progress["run-1"] = progress("run-1")
    real = started.run_api.run_progress

    def flaky(run_id):
        if run_id == "run-2":
            raise ApiError(503, "unknown")
        return real(run_id)

    started.run_api.run_progress = flaky
    started.poll()
    assert state.run_by_id("run-1").progress_at and state.run_by_id("run-2").progress_at
    assert state.run_by_id("run-2").status == "sent"  # a failed read leaves the run open


def test_at_most_50_progress_reads_per_poll(started, makia, state):
    for n in range(60):
        state.upsert_run(RunEntry(f"X{n}", f"c{n}", f"bulk-{n}", None, "sent", None, False, None))
        makia.progress[f"bulk-{n}"] = progress(f"bulk-{n}")
    makia.calls.clear()
    started.poll()
    reads = [c for c in makia.calls if c[1].endswith("/progress")]
    assert PROGRESS_READS_PER_POLL == 50 and len(reads) == 50
    makia.calls.clear()
    started.poll()
    again = {c[1] for c in makia.calls if c[1].endswith("/progress")}
    assert len(again) == 50 and sum(1 for p in again if p.startswith("/runs/bulk-")) >= 12


def _recovered(integration, source, makia, status):
    makia.cases["114-0001"] = {
        "case_id": "case-1",
        "run_id": "run-1",
        "status_url": "/runs/run-1",
        "review_url": "https://studio.example/runs/run-1",
    }
    makia.emit("run.succeeded", run_id="run-1", status=status)  # before the marker
    source._records = [REC]
    source._files = {REC.external_id: ["a.pdf"]}
    integration.sync()
    integration.send()
    makia.progress["run-1"] = progress("run-1", status)


def test_run_finished_before_marker_is_caught_up(integration, source, makia, state, sink):
    makia.set_rows("run-1", "mapping", [row("a"), row("b")])
    _recovered(integration, source, makia, "succeeded")
    sink.calls.clear()
    report = integration.poll()
    assert report.caught_up == 1
    assert state.run_by_id("run-1").status == "succeeded"
    assert names(sink) == ["run_status", "result_rows", "view_done"]
    assert calls(sink, "run_status")[0][0].status == "succeeded"
    assert [r.unit_key for r in calls(sink, "result_rows")[0][3]] == ["a", "b"]
    integration.poll()  # a finished run is not read again
    assert makia.calls.count(("GET", "/runs/run-1/progress")) == 1


def test_failed_catch_up_reads_no_views(integration, source, makia, state, sink):
    _recovered(integration, source, makia, "failed")
    assert integration.poll().caught_up == 1
    assert state.run_by_id("run-1").status == "failed"
    assert ("GET", "/runs/run-1/views") not in makia.calls


def test_progress_never_moves_a_finished_run_back(started, makia, state, sink, monkeypatch):
    makia.emit("run.succeeded", run_id="run-1", status="succeeded")
    started.poll()
    monkeypatch.setattr(state, "open_runs", lambda limit: [state.run_by_id("run-1")])
    makia.progress["run-1"] = progress("run-1", "running")
    sink.calls.clear()
    started.poll()
    assert state.run_by_id("run-1").status == "succeeded"
    (st,) = live(sink)
    assert st.status == "succeeded"


def test_sink_failure_keeps_marker(started, makia, state, sink):
    makia.set_rows("run-1", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    sink.fail_once("result_rows")
    with pytest.raises(RuntimeError, match="sink failed"):
        started.poll()
    assert state.marker() == 1
    assert state.views_read("run-1") == []
    started.poll()
    assert state.marker() == 2
    assert names(sink)[-2:] == ["result_rows", "view_done"]
    assert state.views_read("run-1") == ["mapping"]


def test_sink_failure_on_status_rolls_back_the_status(started, makia, state, sink):
    makia.emit("run.failed", run_id="run-1", status="failed", error_code=None)
    sink.fail_once("run_status")
    with pytest.raises(RuntimeError):
        started.poll()
    assert state.marker() == 1 and state.run_by_id("run-1").status == "sent"
    started.poll()
    assert state.marker() == 2 and state.run_by_id("run-1").status == "failed"


def test_unknown_and_foreign_events_skipped(started, makia, state, sink):
    makia.emit("thing.happened", run_id="run-1")
    makia.emit("run.succeeded", run_id="nobody", status="succeeded")
    makia.set_rows("foreign", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="foreign", views=["mapping"], rebuilt=False)
    report = started.poll()
    assert report.events == 3 and report.views_read == 0
    assert names(sink) == []
    assert state.marker() == 4


def test_feed_is_drained_across_pages(started, makia, state):
    for _ in range(250):
        makia.emit("finding.changed", run_id="run-1", finding_id="f", status="confirmed")
    assert started.poll().events == 250
    assert state.marker() == 251


def test_views_not_built_reads_nothing(started, sink):
    ref = RunRef(REC.external_id, "case-1", "run-1")
    assert started.read_results(ref, "mapping") is None
    assert sink.calls == []


def test_catch_up_sink_failure_is_retried(integration, source, makia, state, sink):
    makia.set_rows("run-1", "mapping", [row("a")])
    _recovered(integration, source, makia, "succeeded")
    sink.calls.clear()
    sink.fail_once("result_rows")
    with pytest.raises(RuntimeError, match="sink failed"):
        integration.poll()
    assert state.run_by_id("run-1").status == "sent"
    assert state.views_read("run-1") == []
    sink.calls.clear()  # the first try's run_status is repeated: at-least-once
    report = integration.poll()
    assert report.caught_up == 1 and report.views_read == 1
    assert state.run_by_id("run-1").status == "succeeded"
    assert state.views_read("run-1") == ["mapping"]
    assert names(sink) == ["run_status", "result_rows", "view_done"]


def test_catch_up_run_status_failure_is_retried(integration, source, makia, state, sink):
    makia.set_rows("run-1", "mapping", [row("a")])
    _recovered(integration, source, makia, "succeeded")
    sink.calls.clear()
    sink.fail_once("run_status")
    with pytest.raises(RuntimeError, match="sink failed"):
        integration.poll()
    assert state.run_by_id("run-1").status == "sent"
    integration.poll()
    assert names(sink) == ["run_status", "result_rows", "view_done"]
    assert state.run_by_id("run-1").status == "succeeded"


def test_stuck_feed_is_logged(started, makia, sink, caplog):
    makia.set_rows("run-1", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    sink.fail_once("result_rows")
    with pytest.raises(RuntimeError):
        started.poll()
    assert "after=1" in caplog.text and "run run-1 view mapping" in caplog.text


def _gone(sink, run_id):
    return [
        s[0]
        for s in calls(sink, "run_status")
        if s[0].ref.run_id == run_id and s[0].status == "gone"
    ]


def test_results_of_a_deleted_run_close_it_and_the_feed_moves_on(started, makia, state, sink):
    makia.set_rows("run-1", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    makia.delete_run("run-1")
    report = started.poll()
    assert report.views_read == 0 and state.marker() == 2
    (gone,) = _gone(sink, "run-1")
    assert gone.error_code == "run_not_found"
    assert state.run_by_id("run-1").status == "gone"
    assert state.run_by_id("run-1") not in state.open_runs(10)
    makia.calls.clear()
    started.poll()
    assert ("GET", "/runs/run-1/progress") not in makia.calls


def test_a_view_no_longer_there_is_skipped(started, makia, state, sink, caplog):
    makia.set_rows("run-1", "other", [row("a")])
    makia.delete_view("run-1", "mapping")
    makia.emit("run.views_built", run_id="run-1", views=["mapping", "other"], rebuilt=False)
    with caplog.at_level("WARNING", logger="makia_integration"):
        report = started.poll()
    assert report.views_read == 1 and state.marker() == 2
    assert state.views_read("run-1") == ["other"]
    assert state.run_by_id("run-1").status == "sent"  # the run is still there
    assert "view_not_found" in caplog.text and "mapping" in caplog.text


def test_progress_of_a_deleted_run_closes_it(started, makia, state, sink):
    makia.delete_run("run-2")
    started.poll()
    assert state.run_by_id("run-2").status == "gone"
    (gone,) = _gone(sink, "run-2")
    assert gone.error_code == "run_not_found" and gone.ref.record_external_id == REC2.external_id
    makia.calls.clear()
    sink.calls.clear()
    started.poll()
    assert ("GET", "/runs/run-2/progress") not in makia.calls and _gone(sink, "run-2") == []


def test_catch_up_of_a_deleted_run_closes_it(integration, source, makia, state, sink, monkeypatch):
    makia.set_rows("run-1", "mapping", [row("a")])
    _recovered(integration, source, makia, "succeeded")

    def views(run_id):
        raise ApiError(404, "run_not_found")

    monkeypatch.setattr(integration.run_api, "run_views", views)
    sink.calls.clear()
    integration.poll()
    assert state.run_by_id("run-1").status == "gone"
    assert [s[0].status for s in calls(sink, "run_status")] == ["succeeded", "gone"]


def test_match_changed_for_a_deleted_run_moves_on(started, makia, state, sink):
    makia.set_rows("run-1", "mapping", [row("a")])
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    started.poll()
    makia.delete_run("run-1")
    makia.emit("match.changed", case_id="case-1", run_id=None, document_id="d", booking_id="b")
    started.poll()
    assert state.marker() == 3 and state.run_by_id("run-1").status == "gone"
    makia.emit("match.changed", case_id="case-1", run_id=None, document_id="d", booking_id="b")
    makia.calls.clear()
    started.poll()
    assert state.marker() == 4
    assert not any("/runs/run-1/" in p for _, p in makia.calls)


def test_events_wait_until_run_sent_was_delivered(integration, source, makia, state, sink):
    makia.emit("case_created")
    source._records = [REC]
    source._files = {REC.external_id: ["a.pdf"]}
    sink.fail_once("run_sent")
    with pytest.raises(RuntimeError):
        integration.send()
    makia.set_rows("run-1", "mapping", [row("a")])
    makia.emit("run.succeeded", run_id="run-1", status="succeeded")
    makia.emit("run.views_built", run_id="run-1", views=["mapping"], rebuilt=False)
    makia.progress["run-1"] = progress("run-1", "succeeded")
    integration.poll()
    assert sink.calls == [] and state.marker() == 3  # nothing for a run the sink never heard of
    integration.send()
    integration.poll()  # caught up from its progress
    assert names(sink) == ["run_sent", "run_status", "result_rows", "view_done"]
    assert state.run_by_id("run-1").status == "succeeded"
