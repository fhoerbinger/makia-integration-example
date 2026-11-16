"""The integration's data types and the two seams a customer fills in."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable


class ConfigError(Exception):
    """The integration was set up in a way that cannot work."""


@dataclass(frozen=True, slots=True)
class Project:
    external_id: str
    name: str
    archived: bool = False


@dataclass(frozen=True, slots=True)
class Member:
    project_external_id: str
    subject: str
    permissions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class Record:
    external_id: str
    project_external_id: str
    workflow_slug: str
    input_id: str


@dataclass(frozen=True, slots=True)
class SentRun:
    case_id: str
    run_id: str
    review_url: str
    known: bool


@dataclass(frozen=True, slots=True)
class RunRef:
    record_external_id: str
    case_id: str
    run_id: str


@dataclass(frozen=True, slots=True)
class RunState:
    ref: RunRef
    status: str  # sent, running, succeeded, failed, cancelled, gone
    error_code: str | None
    progress: dict[str, Any] | None


@dataclass(frozen=True, slots=True)
class ResultRow:
    unit_key: str
    row_key: str | None
    data: dict[str, Any]
    findings_open: int
    review_state: str
    review_option: str | None
    review_key: str | None
    url: str

    @classmethod
    def from_api(cls, row: dict[str, Any]) -> ResultRow:
        review = row.get("review") or {}
        return cls(
            unit_key=row["unit_key"],
            row_key=row.get("row_key"),
            data=row.get("data") or {},
            findings_open=(row.get("findings") or {}).get("open", 0),
            review_state=review.get("state", "none"),
            review_option=review.get("option"),
            review_key=review.get("key"),
            url=row.get("url", ""),
        )


@runtime_checkable
class Source(Protocol):
    def projects(self) -> Iterable[Project]: ...

    def members(self) -> Iterable[Member]: ...

    def records(self) -> Iterable[Record]: ...

    def files(self, record: Record) -> list[str]: ...


@runtime_checkable
class Sink(Protocol):
    def run_sent(self, record: Record, run: SentRun) -> None: ...

    def run_refused(self, record: Record, code: str) -> None: ...

    def record_skipped(self, record: Record, reason: str) -> None: ...

    def run_status(self, state: RunState) -> None: ...

    def result_rows(
        self, ref: RunRef, view: str, generation: str, rows: list[ResultRow]
    ) -> None: ...

    def view_done(self, ref: RunRef, view: str, generation: str) -> None: ...
