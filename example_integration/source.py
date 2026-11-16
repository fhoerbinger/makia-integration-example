"""What to send: the customer's projects, members, records and files, read from the `src_*`
tables. In a real integration make those views over the source system."""

from __future__ import annotations

from typing import Any

from makia_integration import Member, Project, Record

from example_integration.db import Connection, rows


class DbSource:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def projects(self) -> list[Project]:
        found = rows(self.conn, "SELECT external_id, name, archived FROM src_projects")
        return [Project(external_id=r[0], name=r[1], archived=bool(r[2])) for r in found]

    def members(self) -> list[Member]:
        found = rows(self.conn, "SELECT project_external_id, subject, permissions FROM src_members")
        return [Member(r[0], r[1], _permissions(r[2])) for r in found]

    def records(self) -> list[Record]:
        found = rows(
            self.conn,
            "SELECT external_id, project_external_id, workflow_slug, input_id FROM src_records",
        )
        return [Record(*r) for r in found]

    def files(self, record: Record) -> list[str]:
        found = rows(
            self.conn,
            "SELECT path FROM src_record_files WHERE record_external_id = ? ORDER BY path",
            record.external_id,
        )
        return [r[0] for r in found]


def _permissions(value: Any) -> tuple[str, ...]:
    return tuple(p.strip() for p in str(value).split(",") if p.strip())
