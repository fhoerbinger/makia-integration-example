"""The integration: sync projects and members, send records, poll results."""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from dataclasses import dataclass, field
from dataclasses import replace
from pathlib import Path, PurePath

from makia_integration.client import ApiError, MakiaClient
from makia_integration.model import (
    ConfigError,
    Record,
    ResultRow,
    RunRef,
    RunState,
    SentRun,
    Sink,
    Source,
)
from makia_integration.state import RunEntry, StateStore

log = logging.getLogger("makia_integration")


FIXABLE_REFUSALS = (
    "project_archived",
    "project_not_found",
    "workflow_not_found",
    "workflow_not_published",
    "workflow_archived",
    "connection_not_found",
    "missing_file",
)
FINAL_REFUSALS = (
    "file_too_large",
    "empty_file",
    "unsupported_media_type",
    "too_many_files",
    "unknown_input",
    "missing_required_input",
    "too_many_files_for_input",
    "invalid_input",
)
MAX_FILE_BYTES = 50 * 1024 * 1024
PROGRESS_READS_PER_POLL = 50
FINAL_RUN_EVENTS = {
    "run.succeeded": "succeeded",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}
FINAL_STATUSES = frozenset(FINAL_RUN_EVENTS.values())
NO_RUN = "no_run"  # MAKIA knows the record, but its case has no run any more
GONE = "gone"  # the run was deleted in MAKIA (404 run_not_found)
_DONE = ("sent", "running", "succeeded", "failed", "cancelled", "refused_final", NO_RUN, GONE)
_GONE_CODES = ("run_not_found", "view_not_found")


class SyncError(Exception):
    """Some projects could not be synced; the others were. `failed` holds their ids."""

    def __init__(self, failed: list[str], reasons: list[str], report: SyncReport) -> None:
        super().__init__(
            f"{len(failed)} of {report.projects} projects refused: " + "; ".join(reasons)
        )
        self.failed = failed
        self.report = report


@dataclass(frozen=True, slots=True)
class SyncReport:
    projects: int
    conflicts: int
    not_archived_absent: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class SendReport:
    sent: int
    known: int
    refused: int
    skipped: int


@dataclass(frozen=True, slots=True)
class PollReport:
    events: int  # feed events read, including skipped ones
    views_read: int  # (run, view) pairs read through to view_done
    caught_up: int  # runs that had finished before the marker


class _Skip(Exception):
    """The record cannot go as it is; nothing is stored."""


class _Refuse(Exception):
    """A local check refused the record for good."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class Integration:
    def __init__(
        self,
        source: Source,
        sink: Sink,
        *,
        sync_api: MakiaClient,
        run_api: MakiaClient,
        state: StateStore,
        storage_connection_id: str = "",
        files_dir: Path | None = None,
        run_account_permissions: tuple[str, ...] = ("cases", "run", "review"),
    ) -> None:
        self.source = source
        self.sink = sink
        self.sync_api = sync_api  # sync token: /sync/* only
        self.run_api = run_api  # the run account's token: everything else
        self.state = state
        if storage_connection_id and files_dir is not None:
            raise ConfigError(send_mode_problem(storage_connection_id, files_dir))
        self.storage_connection_id = storage_connection_id
        self.files_dir = files_dir
        self.run_account_permissions = run_account_permissions

    def close(self) -> None:
        """Close the sink, the source (if they have a `close()`), the state and both
        clients. Best effort: one failing close does not skip the others."""
        owners: list[object] = []
        for owner in (self.sink, self.source, self.state, self.sync_api, self.run_api):
            if not any(owner is seen for seen in owners):
                owners.append(owner)
        for owner in owners:
            close = getattr(owner, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception:
                log.debug("close failed", exc_info=True)

    def sync(self) -> SyncReport:
        """Send every source project's full state. A project leaving the source
        must be marked archived there: absence alone archives nothing."""
        run_account = self.run_api.me()["userId"]
        members_by_project: dict[str, list[dict[str, object]]] = {}
        for member in self.source.members():
            if member.subject == run_account:
                raise ConfigError(
                    f"member {member.subject!r} of project {member.project_external_id!r} "
                    "is the run account; it is added to every project automatically"
                )
            members_by_project.setdefault(member.project_external_id, []).append(
                {"subject": member.subject, "permissions": list(member.permissions)}
            )

        projects = sorted(self.source.projects(), key=lambda p: p.external_id)
        conflicts = 0
        failed: list[str] = []
        reasons: list[str] = []
        for project in projects:
            listed = sorted(
                members_by_project.get(project.external_id, []), key=lambda m: m["subject"]
            )
            members = [
                *listed,
                {
                    "service_account": run_account,
                    "permissions": list(self.run_account_permissions),
                },
            ]
            try:
                answer = self.sync_api.put_project(
                    project.external_id,
                    name=project.name,
                    archived=project.archived,
                    members=members,
                )
            except ApiError as err:
                # A refused project (bad member, slug taken) must not stop the others;
                # auth, 5xx and transport errors end the job.
                if err.status in (401, 403) or not 400 <= err.status < 500:
                    raise
                failed.append(project.external_id)
                reasons.append(f"{project.external_id} ({err})")
                if err.code == "foreign_service_account":
                    subjects = ", ".join(str(m["subject"]) for m in listed) or "none"
                    log.warning(
                        "sync %s: refused (%s): a member is a service account, or the run "
                        "account is not of this organization; members: %s. A Source member "
                        "must name a person; check MAKIA_RUN_TOKEN",
                        project.external_id,
                        err,
                        subjects,
                    )
                else:
                    log.warning("sync %s: refused (%s)", project.external_id, err)
                continue
            m = answer["members"]
            log.info(
                "sync %s: %s (+%d ~%d -%d, pending %d, conflicts %d)",
                project.external_id,
                answer["outcome"],
                len(m["added"]),
                len(m["changed"]),
                len(m["removed"]),
                len(m["pending"]),
                len(m["conflicts"]),
            )
            for conflict in m["conflicts"]:
                log.warning(
                    "sync %s: left alone, resolve in Studio: %s", project.external_id, conflict
                )
            conflicts += len(m["conflicts"])

        known = {p.external_id for p in projects}
        absent = sorted(
            p["external_id"]
            for p in self.sync_api.synced_projects()
            if p["external_id"] not in known and not p["archived"]
        )
        for external_id in absent:
            log.warning("sync: %s is no longer in the source but not archived there", external_id)
        report = SyncReport(len(projects), conflicts, absent)
        if failed:
            raise SyncError(failed, reasons, report)
        return report

    # --- send ---------------------------------------------------------------

    def send(self) -> SendReport:
        """Start a run for every source record not sent yet; tell the sink.

        Delivery to the sink is at-least-once: the run is stored with
        `sink_pending` before `run_sent` and cleared after it returns."""
        problem = send_mode_problem(self.storage_connection_id, self.files_dir)
        if problem:
            raise ConfigError(problem)
        if self.state.marker() is None:
            self.state.set_marker(self.run_api.changes_head())
        sent = known = refused = skipped = 0
        seen: set[str] = set()
        for record in self.source.records():
            if record.external_id in seen:
                continue
            seen.add(record.external_id)
            entry = self.state.run_by_record(record.external_id)
            if entry is not None and entry.sink_pending:
                if entry.status in ("refused_final", NO_RUN):
                    assert entry.error_code is not None
                    self.sink.run_refused(record, entry.error_code)
                    self.state.upsert_run(replace(entry, sink_pending=False))
                else:
                    self._deliver(record, entry, known=False)
                continue
            if entry is not None and entry.status in _DONE:
                continue
            paths = self.source.files(record)
            if not paths:
                self.sink.record_skipped(record, "no files")
                skipped += 1
                continue
            try:
                status, answer = self._post(record, paths)
            except _Skip as skip:
                self.sink.record_skipped(record, str(skip))
                skipped += 1
                continue
            except (_Refuse, ApiError) as err:
                code = err.code
                if isinstance(err, ApiError) and code not in FIXABLE_REFUSALS + FINAL_REFUSALS:
                    # Any other 4xx (not 401/403) is treated as fixable so one odd record
                    # cannot block the rest; auth, 5xx and transport errors end the pass.
                    if err.status in (401, 403) or not 400 <= err.status < 500:
                        raise
                    log.warning(
                        "send %s: MAKIA refused with an unlisted code %s (%d); "
                        "tried again next pass",
                        record.external_id,
                        code,
                        err.status,
                    )
                final = code in FINAL_REFUSALS
                self.state.upsert_run(
                    RunEntry(
                        record.external_id,
                        None,
                        None,
                        None,
                        "refused_final" if final else "refused",
                        code,
                        final,
                        None,
                    )
                )
                log.warning("send %s: refused (%s)", record.external_id, code)
                self.sink.run_refused(record, code)
                if final:
                    self.state.upsert_run(
                        RunEntry(
                            record.external_id, None, None, None, "refused_final", code, False, None
                        )
                    )
                refused += 1
                continue
            if answer.get("run_id") is None:
                # 200 for a known record whose case has no run any more: final until
                # `retry`, so one deleted run never blocks the records after it.
                entry = RunEntry(
                    record.external_id, answer["case_id"], None, None, NO_RUN, NO_RUN, True, None
                )
                self.state.upsert_run(entry)
                log.warning(
                    "send %s: MAKIA knows it, but case %s has no run any more (deleted); "
                    "not sent again until `retry %s`",
                    record.external_id,
                    answer["case_id"],
                    record.external_id,
                )
                self.sink.run_refused(record, NO_RUN)
                self.state.upsert_run(replace(entry, sink_pending=False))
                refused += 1
                continue
            entry = RunEntry(
                record.external_id,
                answer["case_id"],
                answer["run_id"],
                answer["review_url"],
                "sent",
                None,
                True,
                None,
            )
            self.state.upsert_run(entry)
            log.info(
                "send %s: %s run %s",
                record.external_id,
                "known" if status == 200 else "started",
                answer["run_id"],
            )
            self._deliver(record, entry, known=status == 200)
            if status == 200:
                known += 1
            else:
                sent += 1
        return SendReport(sent, known, refused, skipped)

    def _deliver(self, record: Record, entry: RunEntry, *, known: bool) -> None:
        if not (entry.case_id and entry.run_id):
            raise RuntimeError(f"state entry for {record.external_id!r} has no run to deliver")
        self.sink.run_sent(
            record, SentRun(entry.case_id, entry.run_id, entry.review_url or "", known)
        )
        self.state.upsert_run(replace(entry, sink_pending=False))

    def _post(self, record: Record, paths: list[str]) -> tuple[int, dict]:
        if self.files_dir is None:
            sources = [
                {
                    "inputId": record.input_id,
                    "connectionId": self.storage_connection_id,
                    "paths": paths,
                }
            ]
            return self.run_api.send_record(
                record.project_external_id,
                record.workflow_slug,
                external_id=record.external_id,
                sources=sources,
            )
        files = self._folder_files(record, paths)
        return self.run_api.send_record_files(
            record.project_external_id,
            record.workflow_slug,
            external_id=record.external_id,
            files=files,
        )

    def _folder_files(self, record: Record, paths: list[str]) -> list[tuple[str, Path]]:
        """The record's files under `files_dir`. Raises _Skip when one is missing,
        unreadable or outside the folder, _Refuse when one is too big or empty."""
        assert self.files_dir is not None
        root = self.files_dir.resolve()
        found: list[tuple[str, Path, int]] = []
        for rel in paths:
            try:
                norm = PurePath(rel.replace("\\", "/"))
                file = (root / norm).resolve()
                if norm.is_absolute() or not file.is_relative_to(root):
                    raise _Skip(f"file outside the folder: {rel}")
                if not file.is_file():
                    raise _Skip(f"file not found in the folder: {rel}")
                size = file.stat().st_size
                with file.open("rb"):
                    pass
            except (OSError, ValueError) as err:
                raise _Skip(f"file not readable: {rel} ({err})") from err
            found.append((record.input_id, file, size))
        # Skips win over refusals: a record that is not whole is not refused for good.
        for _, _, size in found:
            if size > MAX_FILE_BYTES:
                raise _Refuse("file_too_large")
            if size == 0:
                raise _Refuse("empty_file")
        return [(input_id, file) for input_id, file, _ in found]

    # --- poll ---------------------------------------------------------------

    def poll(self, *, limit: int = 100) -> PollReport:
        """Read the feed from the stored marker until it is drained, then read
        the progress of runs still open. The marker of a page moves only after
        every sink call for that page returned; a raising sink leaves it, and
        the next poll repeats the page (at-least-once)."""
        events, views = self._poll_feed(limit=limit)
        caught_up, more_views = self._read_progress()
        return PollReport(events, views + more_views, caught_up)

    def _poll_feed(self, *, limit: int) -> tuple[int, int]:
        after = self.state.marker()
        if after is None:
            # A new integration starts at the feed's head instead of replaying history.
            self.state.set_marker(self.run_api.changes_head())
            return 0, 0
        events = views = 0
        while True:
            page = self.run_api.changes(after, limit=limit)
            reading = "events"
            try:
                with self.state.transaction():
                    to_read: set[tuple[str, str]] = set()
                    for change in page["changes"]:
                        to_read |= self._handle(change)
                    for run_id, view in sorted(to_read):
                        entry = self.state.run_by_id(run_id)
                        if _readable(entry):
                            assert entry is not None
                            reading = f"run {run_id} view {view}"
                            if self.read_results(_ref(entry), view) is not None:
                                views += 1
                    self.state.set_marker(page["next"])
            except Exception:
                log.warning("poll: feed stuck at after=%s while handling %s", after, reading)
                raise
            events += len(page["changes"])
            after = page["next"]
            if not page["more"]:
                return events, views

    def _handle(self, change: dict) -> set[tuple[str, str]]:
        """One feed event: update state, call the sink, name the (run, view) pairs to
        read. Events of a run whose `run_sent` is not delivered yet are skipped: the
        sink has not heard of the run. It stays open, so once `send` delivers
        `run_sent`, its progress catches it up (status and views)."""
        kind, ref = change["type"], change["ref"]
        if kind in FINAL_RUN_EVENTS:
            entry = self.state.run_by_id(ref["run_id"])
            if _readable(entry):
                assert entry is not None
                status, code = FINAL_RUN_EVENTS[kind], ref.get("error_code")
                self.state.set_status(ref["run_id"], status, code, only_open=False)
                self.sink.run_status(RunState(_ref(entry), status, code, None))
            return set()
        if kind == "run.views_built":
            if not _readable(self.state.run_by_id(ref["run_id"])):
                return set()
            return {(ref["run_id"], view) for view in ref["views"]}
        if kind == "row.reviewed":
            if not _readable(self.state.run_by_id(ref["run_id"])):
                return set()
            return {(ref["run_id"], ref["view"])}
        if kind == "match.changed":
            # A pair decision stands for the whole case: every run of it may change.
            return {
                (run.run_id, view)
                for run in self.state.runs_of_case(ref["case_id"])
                if run.run_id and _readable(run)
                for view in self.state.views_read(run.run_id)
            }
        return set()

    def _read_progress(self, *, at_most: int = PROGRESS_READS_PER_POLL) -> tuple[int, int]:
        """Store the live progress of runs `sent` or `running`, least recently
        read first. A run progress shows finished is one whose final event this
        integration will never read (it ended before the marker): it is caught up
        from here. Status updates touch only open runs, so progress never moves
        a finished run back."""
        caught_up = views = 0
        for entry in self.state.open_runs(at_most):
            run_id = entry.run_id
            assert run_id is not None
            if entry.sink_pending:
                continue  # `send` delivers run_sent first
            try:
                snapshot = self.run_api.run_progress(run_id)
            except ApiError as err:
                if err.status == 404 and err.code == "run_not_found":
                    with self.state.transaction():
                        self.state.touch_progress(run_id, _now())
                        self._gone(_ref(entry))
                    continue
                log.warning("progress %s: %s", run_id, err)
                # Count it as read, so runs that keep failing rotate to the back.
                self.state.touch_progress(run_id, _now())
                continue
            status = snapshot["run"]["status"]
            # One transaction per run: a raise anywhere leaves the run open, so
            # the next poll reads its progress and catches it up again.
            with self.state.transaction():
                self.state.touch_progress(run_id, _now())
                if status in FINAL_STATUSES:
                    views += self._catch_up(entry, status)
                    caught_up += 1
                    continue
                if status == "running":
                    self.state.set_status(run_id, "running", None, only_open=True)
                current = self.state.run_by_id(run_id) or entry
                self.sink.run_status(RunState(_ref(current), current.status, None, snapshot))
        return caught_up, views

    def _gone(self, ref: RunRef) -> None:
        """The run was deleted in MAKIA: close it (status `gone`, never read again) and
        tell the sink. Inside the caller's transaction."""
        log.warning(
            "run %s (record %s) is gone from MAKIA: closed", ref.run_id, ref.record_external_id
        )
        self.state.set_status(ref.run_id, GONE, "run_not_found", only_open=False)
        self.sink.run_status(RunState(ref, GONE, "run_not_found", None))

    def _catch_up(self, entry: RunEntry, status: str) -> int:
        """Take a finished run's outcome from its progress and read the views it
        built. Should its feed events still come, they set the same status and
        re-read the same rows. Runs inside the caller's transaction. A run caught
        up from progress carries no error_code (progress has none)."""
        run_id = entry.run_id
        assert run_id is not None
        self.state.set_status(run_id, status, None, only_open=True)
        current = self.state.run_by_id(run_id) or entry
        log.info("caught up %s: %s, ended before the feed marker", run_id, status)
        self.sink.run_status(RunState(_ref(current), current.status, current.error_code, None))
        views = 0
        if status == "succeeded":
            try:
                built = self.run_api.run_views(run_id)
            except ApiError as err:
                if err.status == 404 and err.code == "run_not_found":
                    self._gone(_ref(current))
                    return 0
                raise
            for view in built:
                if view["built"]:
                    if self.read_results(_ref(current), view["view"]) is not None:
                        views += 1
        return views

    def read_results(self, ref: RunRef, view: str) -> int | None:
        """Hand one view of a run to the sink, page by page, then `view_done`.
        A view rebuilt while reading answers 409 generation_changed: start again
        from the top. Returns the row count, or None when the view is not built yet
        or is gone (404 view_not_found; 404 run_not_found also closes the run)."""
        for _attempt in range(3):
            try:
                count = self._read_all_pages(ref, view)
            except ApiError as err:
                if err.code == "generation_changed":
                    continue
                if err.code == "views_not_built":
                    return None  # run.views_built comes later
                if err.status == 404 and err.code in _GONE_CODES:
                    log.warning("results %s/%s: %s, skipped", ref.run_id, view, err)
                    if err.code == "run_not_found":
                        self._gone(ref)
                    return None
                raise
            log.info("results %s/%s: %d rows", ref.run_id, view, count)
            return count
        raise RuntimeError(f"results {ref.run_id}/{view}: view kept being rebuilt while reading")

    def _read_all_pages(self, ref: RunRef, view: str) -> int:
        after, count = None, 0
        while True:
            page = self.run_api.results_page(ref.run_id, view, after=after)
            generation = page["generation"]
            rows = [ResultRow.from_api(r) for r in page["rows"]]
            self.sink.result_rows(ref, view, generation, rows)
            count += len(rows)
            after = page["next"]
            if after is None:
                self.sink.view_done(ref, view, generation)
                self.state.mark_view_read(ref.run_id, view)
                return count


def send_mode_problem(storage_connection_id: str, files_dir: Path | None) -> str | None:
    """None when exactly one way of sending files is set, else what to fix."""
    if storage_connection_id and files_dir is not None:
        return (
            "set MAKIA_FILES_DIR (files uploaded from a folder) or "
            "MAKIA_STORAGE_CONNECTION_ID (files by reference), not both"
        )
    if not storage_connection_id and files_dir is None:
        return (
            "set MAKIA_STORAGE_CONNECTION_ID or MAKIA_FILES_DIR: send needs to know "
            "how files go to MAKIA"
        )
    return None


def _readable(entry: RunEntry | None) -> bool:
    """A run this integration started, whose run_sent was delivered and that is not gone."""
    return entry is not None and not entry.sink_pending and entry.status != GONE


def _ref(entry: RunEntry) -> RunRef:
    assert entry.case_id and entry.run_id
    return RunRef(entry.record_external_id, entry.case_id, entry.run_id)


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
