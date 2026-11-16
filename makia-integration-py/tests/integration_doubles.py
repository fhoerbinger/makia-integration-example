"""Test doubles: an in-memory Source and a Sink that records its calls."""

from __future__ import annotations

from typing import Any

from makia_integration.model import Member, Project, Record


class MemorySource:
    def __init__(
        self,
        projects: list[Project] | None = None,
        members: list[Member] | None = None,
        records: list[Record] | None = None,
        files: dict[str, list[str]] | None = None,
    ) -> None:
        self._projects = projects or []
        self._members = members or []
        self._records = records or []
        self._files = files or {}

    def projects(self):
        return list(self._projects)

    def members(self):
        return list(self._members)

    def records(self):
        return list(self._records)

    def files(self, record: Record) -> list[str]:
        return list(self._files.get(record.external_id, []))


class RecordingSink:
    """Records each successful call as (method_name, args)."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self._fail: set[str] = set()

    def fail_once(self, name: str) -> None:
        self._fail.add(name)

    def _do(self, name: str, *args: Any) -> None:
        if name in self._fail:
            self._fail.discard(name)
            raise RuntimeError(f"sink failed: {name}")
        self.calls.append((name, args))

    def run_sent(self, record, run) -> None:
        self._do("run_sent", record, run)

    def run_refused(self, record, code) -> None:
        self._do("run_refused", record, code)

    def record_skipped(self, record, reason) -> None:
        self._do("record_skipped", record, reason)

    def run_status(self, state) -> None:
        self._do("run_status", state)

    def result_rows(self, ref, view, generation, rows) -> None:
        self._do("result_rows", ref, view, generation, rows)

    def view_done(self, ref, view, generation) -> None:
        self._do("view_done", ref, view, generation)
