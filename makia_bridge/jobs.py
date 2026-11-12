"""The bridge's three jobs: sync, send, poll. Each is safe to run again."""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from dataclasses import dataclass
from typing import Any

from .api import ApiError, MakiaClient
from .db import Connection, execute, get_state, rows, set_state, upsert

log = logging.getLogger("makia_bridge")

# What the run account may do in every synced project.
RUN_ACCOUNT_PERMISSIONS = ["cases", "run", "review"]
PROGRESS_READS_PER_POLL = 50
FINAL_RUN_EVENTS = {
    "run.succeeded": "succeeded",
    "run.failed": "failed",
    "run.cancelled": "cancelled",
}
FINAL_STATUSES = frozenset(FINAL_RUN_EVENTS.values())


@dataclass
class Bridge:
    conn: Connection
    sync_api: MakiaClient  # holds the sync token: /sync/* only
    run_api: MakiaClient  # holds the run account's token: everything else
    storage_connection_id: str

    # --- 1. sync projects and members ---------------------------------------

    def sync(self) -> None:
        """Send every source project's full state. A project leaving the source
        must be marked archived there: absence alone archives nothing."""
        run_account = self.run_api.me()["userId"]
        members_by_project: dict[str, list[dict[str, Any]]] = {}
        for project, subject, permissions in rows(
            self.conn,
            "SELECT project_external_id, subject, permissions FROM src_members ORDER BY subject",
        ):
            perms = [p.strip() for p in permissions.split(",") if p.strip()]
            members_by_project.setdefault(project, []).append(
                {"subject": subject, "permissions": perms}
            )

        source = rows(
            self.conn, "SELECT external_id, name, archived FROM src_projects ORDER BY external_id"
        )
        for external_id, name, archived in source:
            members = [
                *members_by_project.get(external_id, []),
                {"service_account": run_account, "permissions": RUN_ACCOUNT_PERMISSIONS},
            ]
            answer = self.sync_api.put_project(
                external_id, name=name, archived=bool(archived), members=members
            )
            m = answer["members"]
            log.info(
                "sync %s: %s (+%d ~%d -%d, pending %d, conflicts %d)",
                external_id,
                answer["outcome"],
                len(m["added"]),
                len(m["changed"]),
                len(m["removed"]),
                len(m["pending"]),
                len(m["conflicts"]),
            )
            for conflict in m["conflicts"]:
                log.warning("sync %s: left alone, resolve in Studio: %s", external_id, conflict)

        known = {external_id for external_id, _, _ in source}
        for project in self.sync_api.synced_projects():
            if project["external_id"] not in known and not project["archived"]:
                log.warning(
                    "sync: %s is no longer in the source but not archived there",
                    project["external_id"],
                )

    # --- 2. send records --------------------------------------------------------

    def send(self) -> None:
        """Start a run for every record not sent yet. A record sent before but
        whose answer was lost is sent again: MAKIA answers 200 with the
        existing case and writes nothing."""
        self._start_feed()
        pending = rows(
            self.conn,
            "SELECT r.external_id, r.project_external_id, r.workflow_slug, r.input_id FROM src_records r "
            "WHERE NOT EXISTS (SELECT 1 FROM bridge_runs b WHERE b.record_external_id = r.external_id) "
            "ORDER BY r.external_id",
        )
        for external_id, project, workflow, input_id in pending:
            paths = [
                p
                for (p,) in rows(
                    self.conn,
                    "SELECT path FROM src_record_files WHERE record_external_id = ? ORDER BY path",
                    external_id,
                )
            ]
            if not paths:
                continue
            sources = [
                {"inputId": input_id, "connectionId": self.storage_connection_id, "paths": paths}
            ]
            try:
                status, answer = self.run_api.send_record(
                    project, workflow, external_id=external_id, sources=sources
                )
            except ApiError as err:
                if err.code in ("project_archived", "project_not_found", "workflow_not_found"):
                    upsert(
                        self.conn,
                        "bridge_runs",
                        {"record_external_id": external_id},
                        {"status": "refused", "error_code": err.code},
                    )
                    self.conn.commit()
                    log.warning("send %s: refused (%s)", external_id, err.code)
                    continue
                raise
            upsert(
                self.conn,
                "bridge_runs",
                {"record_external_id": external_id},
                {
                    "case_id": answer["case_id"],
                    "run_id": answer["run_id"],
                    "review_url": answer["review_url"],
                    "status": "sent",
                    "error_code": None,
                },
            )
            self.conn.commit()
            log.info(
                "send %s: %s run %s",
                external_id,
                "known" if status == 200 else "started",
                answer["run_id"],
            )

    # --- 3. poll the change feed ----------------------------------------------

    def poll(self, *, limit: int = 100) -> None:
        """Read the feed from the stored marker until it is drained, then read
        the progress of runs still open."""
        self._poll_feed(limit=limit)
        self._read_progress()

    def _poll_feed(self, *, limit: int) -> None:
        """Results are re-read per (run, view) once per page, then the marker moves."""
        if self._start_feed():
            return
        after = int(get_state(self.conn, "feed_marker"))
        while True:
            page = self.run_api.changes(after, limit=limit)
            to_read: set[tuple[str, str]] = set()
            for change in page["changes"]:
                to_read |= self._handle(change)
            for run_id, view in sorted(to_read):
                self.read_results(run_id, view)
            after = page["next"]
            set_state(self.conn, "feed_marker", str(after))
            self.conn.commit()
            if not page["more"]:
                return

    def _start_feed(self) -> bool:
        """A new bridge starts at the feed's head instead of replaying history.
        `send` calls this before its first run, so a run that ends before the
        first poll is still after the marker. True if the marker was just set."""
        if get_state(self.conn, "feed_marker") is not None:
            return False
        set_state(self.conn, "feed_marker", str(self.run_api.changes_head()))
        self.conn.commit()
        return True

    def _read_progress(self, *, at_most: int = PROGRESS_READS_PER_POLL) -> None:
        """Store the live progress of runs `sent` or `running`, least recently
        read first (never-read first). A run that is running becomes `running`.
        A run progress shows finished is one whose final event this bridge will
        never read (it ended before the marker): it is caught up from here. The
        UPDATEs touch only open runs, so progress never moves a finished run back."""
        open_runs = rows(
            self.conn,
            "SELECT run_id FROM bridge_runs WHERE status IN ('sent', 'running') "
            "AND run_id IS NOT NULL ORDER BY progress_at, record_external_id",
        )
        for (run_id,) in open_runs[:at_most]:
            try:
                snapshot = self.run_api.run_progress(run_id)
            except ApiError as err:
                log.warning("progress %s: %s", run_id, err)
                # Count it as read, so runs that keep failing rotate to the back
                # instead of filling the window every poll.
                execute(
                    self.conn,
                    "UPDATE bridge_runs SET progress_at = ? WHERE run_id = ?",
                    datetime.now(UTC).isoformat(timespec="seconds"),
                    run_id,
                )
                self.conn.commit()
                continue
            execute(
                self.conn,
                "UPDATE bridge_runs SET progress_json = ?, progress_at = ?, "
                "status = CASE WHEN ? = 'running' THEN 'running' ELSE status END "
                "WHERE run_id = ? AND status IN ('sent', 'running')",
                json.dumps(snapshot, sort_keys=True),
                datetime.now(UTC).isoformat(timespec="seconds"),
                snapshot["run"]["status"],
                run_id,
            )
            self.conn.commit()
            if snapshot["run"]["status"] in FINAL_STATUSES:
                self._catch_up(run_id, snapshot["run"]["status"])

    def _catch_up(self, run_id: str, status: str) -> None:
        """Take a finished run's outcome from its progress and copy the views it
        built. Should its feed events still come (it ended between this poll's
        feed read and now), they set the same status and re-read the same rows."""
        execute(
            self.conn,
            "UPDATE bridge_runs SET status = ? WHERE run_id = ? AND status IN ('sent', 'running')",
            status,
            run_id,
        )
        self.conn.commit()
        log.info("caught up %s: %s, ended before the feed marker", run_id, status)
        if status == "succeeded":
            for view in self.run_api.run_views(run_id):
                if view["built"]:
                    self.read_results(run_id, view["view"])

    def _handle(self, change: dict[str, Any]) -> set[tuple[str, str]]:
        kind, ref = change["type"], change["ref"]
        if kind in FINAL_RUN_EVENTS:
            execute(
                self.conn,
                "UPDATE bridge_runs SET status = ?, error_code = ? WHERE run_id = ?",
                FINAL_RUN_EVENTS[kind],
                ref.get("error_code"),
                ref["run_id"],
            )
            return set()
        if kind == "run.views_built":
            return {(ref["run_id"], view) for view in ref["views"] if self._is_ours(ref["run_id"])}
        if kind == "row.reviewed":
            return {(ref["run_id"], ref["view"])} if self._is_ours(ref["run_id"]) else set()
        if kind == "match.changed":
            # A pair decision stands for the whole case: every run of it may change.
            return {
                (run_id, view)
                for run_id, view in rows(
                    self.conn,
                    "SELECT DISTINCT r.run_id, r.view_key FROM bridge_results r "
                    "JOIN bridge_runs b ON b.run_id = r.run_id WHERE b.case_id = ?",
                    ref["case_id"],
                )
            }
        return set()

    def _is_ours(self, run_id: str) -> bool:
        return bool(rows(self.conn, "SELECT 1 FROM bridge_runs WHERE run_id = ?", run_id))

    def read_results(self, run_id: str, view: str) -> None:
        """Copy one view of a run into bridge_results, keyed by (run, view,
        unit_key). A view rebuilt while reading answers 409
        generation_changed: start again from the top."""
        for _attempt in range(3):
            try:
                count = self._read_all_pages(run_id, view)
            except ApiError as err:
                if err.code == "generation_changed":
                    continue
                if err.code == "views_not_built":
                    return  # run.views_built comes later
                raise
            log.info("results %s/%s: %d rows", run_id, view, count)
            return
        raise RuntimeError(f"results {run_id}/{view}: view kept being rebuilt while reading")

    def _read_all_pages(self, run_id: str, view: str) -> int:
        after, count = None, 0
        while True:
            page = self.run_api.results_page(run_id, view, after=after)
            generation = page["generation"]
            for row in page["rows"]:
                review = row["review"]
                upsert(
                    self.conn,
                    "bridge_results",
                    {"run_id": run_id, "view_key": view, "unit_key": row["unit_key"]},
                    {
                        "generation": generation,
                        "row_key": row.get("row_key"),
                        "data_json": json.dumps(row["data"], sort_keys=True),
                        "findings_open": row["findings"]["open"],
                        "review_state": review["state"],
                        "review_option": review["option"],
                        "review_key": review["key"],
                        "url": row["url"],
                    },
                )
                count += 1
            after = page["next"]
            if after is None:
                # Rows an older build had and this one does not are gone.
                execute(
                    self.conn,
                    "DELETE FROM bridge_results WHERE run_id = ? AND view_key = ? AND generation <> ?",
                    run_id,
                    view,
                    generation,
                )
                self.conn.commit()
                return count
            self.conn.commit()
