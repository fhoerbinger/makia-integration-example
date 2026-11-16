"""Where results go: `integration_runs` (one line per record) and `integration_results` (one
row per run, view and unit). The package calls each method at least once per event, so every
write is update-then-insert and safe to repeat."""

from __future__ import annotations

import json
from typing import Any

from makia_integration import Record, ResultRow, RunRef, RunState, SentRun

from example_integration.db import Connection, execute, upsert


class DbSink:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def close(self) -> None:
        """Called by Integration.close() when the integration is done or rebuilt after a failure."""
        self.conn.close()

    def _save(self, table: str, key: dict[str, Any], values: dict[str, Any]) -> None:
        try:
            upsert(self.conn, table, key, values)
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def _save_run(self, record_external_id: str, **values: Any) -> None:
        self._save("integration_runs", {"record_external_id": record_external_id}, values)

    def run_sent(self, record: Record, run: SentRun) -> None:
        """May come again for the same run after its final status (at-least-once): the status
        goes back to "sent" only for a new run of the record, never for this one. A late
        delivery can name a record the source no longer returns: keyed on external_id."""
        try:
            execute(
                self.conn,
                "UPDATE integration_runs SET status = 'sent', error_code = NULL "
                "WHERE record_external_id = ? AND (run_id IS NULL OR run_id <> ?)",
                record.external_id,
                run.run_id,
            )
            values = {"case_id": run.case_id, "run_id": run.run_id, "review_url": run.review_url}
            key = {"record_external_id": record.external_id}
            if not execute(
                self.conn,
                "UPDATE integration_runs SET case_id = ?, run_id = ?, review_url = ? "
                "WHERE record_external_id = ?",
                *values.values(),
                record.external_id,
            ):
                upsert(self.conn, "integration_runs", key, {**values, "status": "sent"})
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def run_refused(self, record: Record, code: str) -> None:
        self._save_run(record.external_id, status="refused", error_code=code)

    def record_skipped(self, record: Record, reason: str) -> None:
        """Not sent this pass (no files, a file missing); tried again next pass."""

    def run_status(self, state: RunState) -> None:
        self._save_run(
            state.ref.record_external_id, status=state.status, error_code=state.error_code
        )

    def result_rows(self, ref: RunRef, view: str, generation: str, rows: list[ResultRow]) -> None:
        try:
            for row in rows:
                upsert(
                    self.conn,
                    "integration_results",
                    {"run_id": ref.run_id, "view_key": view, "unit_key": row.unit_key},
                    {
                        "generation": generation,
                        "row_key": row.row_key,
                        "data_json": json.dumps(row.data, ensure_ascii=False),
                        "findings_open": row.findings_open,
                        "review_state": row.review_state,
                        "review_option": row.review_option,
                        "review_key": row.review_key,
                        "url": row.url,
                    },
                )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def view_done(self, ref: RunRef, view: str, generation: str) -> None:
        """All pages of this generation are in: other generations' rows left the view."""
        try:
            execute(
                self.conn,
                "DELETE FROM integration_results "
                "WHERE run_id = ? AND view_key = ? AND generation <> ?",
                ref.run_id,
                view,
                generation,
            )
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise
