from __future__ import annotations

from pathlib import Path

import pytest

from makia_integration.integration import (
    FINAL_REFUSALS,
    FIXABLE_REFUSALS,
    MAX_FILE_BYTES,
    Integration,
    SendReport,
)
from makia_integration.client import ApiError
from makia_integration.model import Record

REC = Record("114-0001", "2026-114", "audit", "belege")
REC2 = Record("114-0002", "2026-114", "audit", "belege")


def posts(makia) -> int:
    return sum(1 for m, p in makia.calls if m == "POST" and "/inbound/" in p)


def names(sink) -> list[str]:
    return [c[0] for c in sink.calls]


def test_constants():
    assert "project_not_found" in FIXABLE_REFUSALS and "empty_file" in FINAL_REFUSALS
    assert MAX_FILE_BYTES == 50 * 1024 * 1024


def test_by_reference_sends_sources(integration, source, sink, makia, monkeypatch):
    source._records = [REC]
    source._files = {REC.external_id: ["a.pdf", "b.pdf"]}
    seen = {}
    real = integration.run_api.send_record

    def spy(*a, **kw):
        seen.update(kw)
        return real(*a, **kw)

    monkeypatch.setattr(integration.run_api, "send_record", spy)
    report = integration.send()
    assert seen["sources"] == [
        {"inputId": "belege", "connectionId": "conn-1", "paths": ["a.pdf", "b.pdf"]}
    ]
    assert report == SendReport(1, 0, 0, 0)
    assert names(sink) == ["run_sent"]


def test_multipart_uploads_files(source, sink, api, state, makia, tmp_path):
    (tmp_path / "2026").mkdir()
    (tmp_path / "2026" / "a.pdf").write_bytes(b"AAA")
    source._records = [REC]
    source._files = {REC.external_id: ["2026/a.pdf"]}
    b = Integration(source, sink, sync_api=api[0], run_api=api[1], state=state, files_dir=tmp_path)
    assert b.send().sent == 1
    assert makia.uploads[0][1] == ("belege", "a.pdf", b"AAA")


def test_marker_stored_before_first_send(integration, source, makia, state):
    makia.emit("case_created")
    source._records = [REC]
    source._files = {REC.external_id: ["a.pdf"]}
    integration.send()
    assert state.marker() == 1
    state.set_marker(7)
    integration.send()
    assert state.marker() == 7


def test_known_answer_is_known(integration, source, sink, makia):
    makia.cases[REC.external_id] = {
        "case_id": "c",
        "run_id": "r",
        "status_url": "/runs/r",
        "review_url": "u",
    }
    source._records = [REC]
    source._files = {REC.external_id: ["a.pdf"]}
    report = integration.send()
    assert report.known == 1 and report.sent == 0
    assert sink.calls[0][1][1].known is True


def test_sink_failure_is_redelivered(integration, source, sink, makia, state):
    source._records = [REC]
    source._files = {REC.external_id: ["a.pdf"]}
    sink.fail_once("run_sent")
    try:
        integration.send()
        raise AssertionError("expected failure")
    except RuntimeError:
        pass
    assert state.pending_sink()
    before = posts(makia)
    integration.send()
    assert posts(makia) == before
    assert names(sink) == ["run_sent"] and not state.pending_sink()
    assert sink.calls[0][1][0] == REC


def test_fixable_refusal_retries_next_pass(integration, source, sink, makia, state):
    source._records = [REC]
    source._files = {REC.external_id: ["a.pdf"]}
    makia.inbound_status, makia.inbound_code = 404, "project_not_found"
    r = integration.send()
    assert r.refused == 1 and state.run_by_record(REC.external_id).status == "refused"
    assert sink.calls == [("run_refused", (REC, "project_not_found"))]
    integration.send()
    assert names(sink) == ["run_refused", "run_refused"]
    makia.inbound_status = None
    assert integration.send().sent == 1
    assert names(sink)[-1] == "run_sent"


def test_final_refusal_waits_for_clear(integration, source, sink, makia, state):
    source._records = [REC]
    source._files = {REC.external_id: ["a.pdf"]}
    makia.inbound_status, makia.inbound_code = 415, "unsupported_media_type"
    integration.send()
    assert state.run_by_record(REC.external_id).status == "refused_final"
    makia.inbound_status = None
    before = posts(makia)
    r = integration.send()
    assert posts(makia) == before and r == SendReport(0, 0, 0, 0)
    assert names(sink) == ["run_refused"]
    state.clear_refusals(REC.external_id)
    assert integration.send().sent == 1


def test_no_files_is_skipped(integration, source, sink):
    source._records = [REC]
    r = integration.send()
    assert r.skipped == 1 and sink.calls == [("record_skipped", (REC, "no files"))]


def folder_integration(source, sink, api, state, tmp_path):
    return Integration(
        source, sink, sync_api=api[0], run_api=api[1], state=state, files_dir=tmp_path
    )


def test_missing_file_is_skipped_not_stored(source, sink, api, state, tmp_path):
    source._records = [REC]
    source._files = {REC.external_id: ["nope.pdf"]}
    r = folder_integration(source, sink, api, state, tmp_path).send()
    assert r.skipped == 1 and "nope.pdf" in sink.calls[0][1][1]
    assert state.run_by_record(REC.external_id) is None


def test_too_big_file_is_final_refusal(source, sink, api, state, makia, tmp_path):
    with (tmp_path / "big.pdf").open("wb") as fh:
        fh.truncate(MAX_FILE_BYTES + 1)
    (tmp_path / "e.pdf").write_bytes(b"")
    source._records = [REC, Record("e", "2026-114", "audit", "belege")]
    source._files = {REC.external_id: ["big.pdf"], "e": ["e.pdf"]}
    r = folder_integration(source, sink, api, state, tmp_path).send()
    assert r.refused == 2 and posts(makia) == 0
    assert state.run_by_record(REC.external_id).error_code == "file_too_large"
    assert state.run_by_record("e").status == "refused_final"
    assert sink.calls[1] == ("run_refused", (source._records[1], "empty_file"))


def test_windows_style_paths_resolve_under_files_dir(source, sink, api, state, makia, tmp_path):
    (tmp_path / "2026" / "114").mkdir(parents=True)
    (tmp_path / "2026" / "114" / "a.pdf").write_bytes(b"A")
    (tmp_path.parent / "x.pdf").write_bytes(b"X")
    other = Record("114-0002", "2026-114", "audit", "belege")
    source._records = [REC, other]
    source._files = {REC.external_id: ["2026\\114\\a.pdf"], other.external_id: ["..\\x.pdf"]}
    r = folder_integration(source, sink, api, state, tmp_path).send()
    assert r.sent == 1 and r.skipped == 1
    assert makia.uploads[0][1][1] == "a.pdf"
    skip = [c for c in sink.calls if c[0] == "record_skipped"][0]
    assert skip[1] == (other, "file outside the folder: ..\\x.pdf")


def test_absolute_path_is_outside(source, sink, api, state, tmp_path):
    source._records = [REC]
    source._files = {REC.external_id: ["/etc/hosts"]}
    folder_integration(source, sink, api, state, tmp_path).send()
    assert sink.calls[0][1][1].startswith("file outside the folder")


def test_duplicate_record_in_one_pass_sent_once(integration, source, sink, makia):
    source._records = [REC, REC]
    source._files = {REC.external_id: ["a.pdf"]}
    r = integration.send()
    assert r.sent == 1 and posts(makia) == 1 and names(sink) == ["run_sent"]


def test_unicode_external_id_roundtrips(integration, source, sink, makia):
    rec = Record("Prüfung/ä ö 1", "2026-114", "audit", "belege")
    source._records = [rec]
    source._files = {rec.external_id: ["a.pdf"]}
    integration.send()
    assert sink.calls[0][1][0] == rec and rec.external_id in makia.cases


def test_final_refusal_redelivered_after_sink_failure(integration, source, sink, makia, state):
    source._records = [REC]
    source._files = {REC.external_id: ["a.pdf"]}
    makia.inbound_status, makia.inbound_code = 415, "unsupported_media_type"
    sink.fail_once("run_refused")
    try:
        integration.send()
        raise AssertionError("expected failure")
    except RuntimeError:
        pass
    makia.inbound_status = None
    before = posts(makia)
    integration.send()
    assert posts(makia) == before and names(sink) == ["run_refused"]
    assert not state.pending_sink() and state.run_by_record(REC.external_id).sink_pending is False
    integration.send()
    assert names(sink) == ["run_refused"]


def test_nul_in_path_is_skipped(source, sink, api, state, tmp_path):
    source._records = [REC]
    source._files = {REC.external_id: ["a\x00.pdf"]}
    r = folder_integration(source, sink, api, state, tmp_path).send()
    assert r.skipped == 1 and state.run_by_record(REC.external_id) is None


def test_missing_file_beats_too_big(source, sink, api, state, tmp_path):
    with (tmp_path / "big.pdf").open("wb") as fh:
        fh.truncate(MAX_FILE_BYTES + 1)
    source._records = [REC]
    source._files = {REC.external_id: ["big.pdf", "nope.pdf"]}
    r = folder_integration(source, sink, api, state, tmp_path).send()
    assert r.skipped == 1 and r.refused == 0 and names(sink) == ["record_skipped"]
    assert state.run_by_record(REC.external_id) is None


def test_unknown_4xx_refusal_does_not_block_later_records(integration, source, sink, makia, state):
    source._records = [REC, REC2]
    source._files = {REC.external_id: ["a.pdf"], REC2.external_id: ["b.pdf"]}
    makia.inbound_status, makia.inbound_code = 409, "import_running"
    r = integration.send()
    assert r.refused == 2 and r.sent == 0
    assert state.run_by_record(REC.external_id).status == "refused"
    assert state.run_by_record(REC.external_id).error_code == "import_running"
    assert sink.calls == [
        ("run_refused", (REC, "import_running")),
        ("run_refused", (REC2, "import_running")),
    ]
    makia.inbound_status = None  # fixed: the next pass sends both
    assert integration.send().sent == 2


@pytest.mark.parametrize("status", [401, 403, 500])
def test_auth_error_still_aborts_send(integration, source, sink, makia, state, status):
    source._records = [REC, REC2]
    source._files = {REC.external_id: ["a.pdf"], REC2.external_id: ["b.pdf"]}
    makia.inbound_status, makia.inbound_code = status, "boom"
    with pytest.raises(ApiError):
        integration.send()
    assert sink.calls == [] and state.run_by_record(REC.external_id) is None


def test_known_record_without_a_run_is_no_run_and_does_not_block(
    integration, source, sink, makia, state, caplog
):
    # 200 for a known external_id whose runs were deleted: run_id is null.
    makia.cases[REC.external_id] = {
        "case_id": "c-gone",
        "run_id": None,
        "status_url": None,
        "review_url": None,
    }
    source._records = [REC, REC2]
    source._files = {REC.external_id: ["a.pdf"], REC2.external_id: ["b.pdf"]}
    with caplog.at_level("WARNING", logger="makia_integration"):
        r = integration.send()
    assert (r.sent, r.refused) == (1, 1)
    entry = state.run_by_record(REC.external_id)
    assert (entry.status, entry.error_code, entry.sink_pending) == ("no_run", "no_run", False)
    assert entry.case_id == "c-gone" and entry.run_id is None
    assert sink.calls[0] == ("run_refused", (REC, "no_run"))
    assert names(sink) == ["run_refused", "run_sent"]
    assert "114-0001" in caplog.text and "no run" in caplog.text
    before = posts(makia)
    integration.send()  # final until retry: not sent again, not reported again
    assert posts(makia) == before and names(sink) == ["run_refused", "run_sent"]
    assert state.open_runs(10) == [state.run_by_record(REC2.external_id)]
    assert state.clear_refusals(REC.external_id) == 1
    del makia.cases[REC.external_id]  # someone started it again in MAKIA
    assert integration.send().sent == 1


def test_no_run_redelivered_after_sink_failure(integration, source, sink, makia, state):
    makia.cases[REC.external_id] = {
        "case_id": "c",
        "run_id": None,
        "status_url": None,
        "review_url": None,
    }
    source._records = [REC]
    source._files = {REC.external_id: ["a.pdf"]}
    sink.fail_once("run_refused")
    with pytest.raises(RuntimeError):
        integration.send()
    before = posts(makia)
    integration.send()
    assert posts(makia) == before and names(sink) == ["run_refused"]
    assert state.run_by_record(REC.external_id).sink_pending is False


def test_both_send_modes_refused(source, sink, api, state, tmp_path):
    from makia_integration.integration import ConfigError

    with pytest.raises(ConfigError, match="MAKIA_FILES_DIR.*MAKIA_STORAGE_CONNECTION_ID"):
        Integration(
            source,
            sink,
            sync_api=api[0],
            run_api=api[1],
            state=state,
            storage_connection_id="conn-1",
            files_dir=tmp_path,
        )


def test_send_without_a_mode_is_a_config_error(source, sink, api, state, makia):
    from makia_integration.integration import ConfigError

    source._records = [REC]
    source._files = {REC.external_id: ["a.pdf"]}
    b = Integration(source, sink, sync_api=api[0], run_api=api[1], state=state)
    with pytest.raises(ConfigError, match="set MAKIA_STORAGE_CONNECTION_ID or MAKIA_FILES_DIR"):
        b.send()
    assert posts(makia) == 0 and sink.calls == []


def test_close_closes_state_clients_source_and_sink(source, sink, api, state):
    closed: list[str] = []
    source.close = lambda: closed.append("source")
    sink.close = lambda: closed.append("sink")
    b = Integration(source, sink, sync_api=api[0], run_api=api[1], state=state, files_dir=Path("."))
    b.close()
    assert closed == ["sink", "source"]
    reopened = type(state)(state.path)  # the lock is released
    reopened.close()


def test_close_calls_a_shared_source_and_sink_once(api, state):
    from integration_doubles import MemorySource

    class Both(MemorySource):
        closes = 0

        def close(self):
            Both.closes += 1

        def __getattr__(self, name):
            return lambda *a, **k: None

    both = Both()
    Integration(
        both, both, sync_api=api[0], run_api=api[1], state=state, files_dir=Path(".")
    ).close()
    assert Both.closes == 1
