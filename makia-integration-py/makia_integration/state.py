"""The integration's own small state, kept in a local SQLite file (never in the customer's DB)."""

from __future__ import annotations

import sqlite3
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Protocol

SCHEMA_VERSION = 1

OPEN_STATUSES = ("sent", "running")


class StateLocked(Exception):
    def __init__(self, path: Path) -> None:
        super().__init__(f"{path} is in use: another makia-integration process is using it")
        self.path = path


@dataclass(frozen=True, slots=True)
class RunEntry:
    record_external_id: str
    case_id: str | None
    run_id: str | None
    review_url: str | None
    status: str  # sent, running, succeeded, failed, cancelled, gone, refused, refused_final, no_run
    error_code: str | None
    sink_pending: bool
    progress_at: str | None


class StateStore(Protocol):
    def marker(self) -> int | None: ...

    def set_marker(self, seq: int) -> None: ...

    def upsert_run(self, entry: RunEntry) -> None: ...

    def run_by_record(self, ext_id: str) -> RunEntry | None: ...

    def run_by_id(self, run_id: str) -> RunEntry | None: ...

    def open_runs(self, limit: int) -> list[RunEntry]: ...

    def pending_sink(self) -> list[RunEntry]: ...

    def runs_of_case(self, case_id: str) -> list[RunEntry]: ...

    def mark_view_read(self, run_id: str, view: str) -> None: ...

    def views_read(self, run_id: str) -> list[str]: ...

    def set_status(
        self, run_id: str, status: str, error_code: str | None, *, only_open: bool
    ) -> None: ...

    def touch_progress(self, run_id: str, at: str) -> None: ...

    def clear_refusals(self, record_external_id: str | None) -> int: ...

    def transaction(self): ...

    def close(self) -> None: ...


def _lock(fh: IO[bytes]) -> bool:
    """Take an exclusive non-blocking lock on the open file; False if held elsewhere."""
    if sys.platform == "win32":
        import msvcrt

        try:
            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True
    import fcntl

    try:
        fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(fh: IO[bytes]) -> None:
    try:
        if sys.platform == "win32":
            import msvcrt

            fh.seek(0)
            msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


_COLUMNS = (
    "record_external_id, case_id, run_id, review_url, status, error_code, sink_pending, progress_at"
)

_MIGRATIONS = [
    """
    CREATE TABLE runs (
        record_external_id TEXT PRIMARY KEY,
        case_id TEXT,
        run_id TEXT UNIQUE,
        review_url TEXT,
        status TEXT NOT NULL,
        error_code TEXT,
        sink_pending INTEGER NOT NULL DEFAULT 0,
        progress_at TEXT
    );
    CREATE INDEX runs_case ON runs(case_id);
    CREATE TABLE views_read (
        run_id TEXT NOT NULL,
        view TEXT NOT NULL,
        PRIMARY KEY (run_id, view)
    );
    """,
]


def _entry(row: tuple) -> RunEntry:
    return RunEntry(*row[:6], bool(row[6]), row[7])


class SqliteStateStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock_file: IO[bytes] | None = open(  # noqa: SIM115
            self.path.with_name(self.path.name + ".lock"), "a+b"
        )
        if not _lock(self._lock_file):
            self._lock_file.close()
            self._lock_file = None
            raise StateLocked(self.path)
        self._depth = 0
        try:
            # autocommit mode; transactions are explicit (BEGIN / SAVEPOINT)
            self._db = sqlite3.connect(self.path, isolation_level=None)
            self._migrate()
        except BaseException:
            self.close()
            raise

    def _migrate(self) -> None:
        db = self._db
        db.execute("CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT)")
        row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        current = int(row[0]) if row else 0
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"{self.path} has schema version {current}, but this makia-integration "
                f"understands up to version {SCHEMA_VERSION}; upgrade makia-integration"
            )
        with self.transaction():
            for n in range(current, SCHEMA_VERSION):
                for stmt in _MIGRATIONS[n].split(";"):
                    if stmt.strip():
                        db.execute(stmt)
            db.execute(
                "INSERT INTO meta(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (str(SCHEMA_VERSION),),
            )

    @contextmanager
    def transaction(self) -> Iterator[None]:
        name = f"sp{self._depth}"
        db = self._db
        db.execute("BEGIN" if self._depth == 0 else f"SAVEPOINT {name}")
        self._depth += 1
        try:
            yield
        except BaseException:
            self._depth -= 1
            if self._depth == 0:
                db.execute("ROLLBACK")
            else:
                db.execute(f"ROLLBACK TO {name}")
                db.execute(f"RELEASE {name}")
            raise
        else:
            self._depth -= 1
            db.execute("COMMIT" if self._depth == 0 else f"RELEASE {name}")

    def close(self) -> None:
        db = getattr(self, "_db", None)
        if db is not None:
            db.close()
            self._db = None
        if self._lock_file is not None:
            _unlock(self._lock_file)
            self._lock_file.close()
            self._lock_file = None

    def marker(self) -> int | None:
        row = self._db.execute("SELECT value FROM meta WHERE key='marker'").fetchone()
        return int(row[0]) if row else None

    def set_marker(self, seq: int) -> None:
        self._db.execute(
            "INSERT INTO meta(key, value) VALUES('marker', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(seq),),
        )

    def upsert_run(self, entry: RunEntry) -> None:
        self._db.execute(
            f"INSERT OR REPLACE INTO runs({_COLUMNS}) VALUES (?,?,?,?,?,?,?,?)",
            (
                entry.record_external_id,
                entry.case_id,
                entry.run_id,
                entry.review_url,
                entry.status,
                entry.error_code,
                int(entry.sink_pending),
                entry.progress_at,
            ),
        )

    def _one(self, where: str, arg: str) -> RunEntry | None:
        row = self._db.execute(f"SELECT {_COLUMNS} FROM runs WHERE {where}=?", (arg,)).fetchone()
        return _entry(row) if row else None

    def run_by_record(self, ext_id: str) -> RunEntry | None:
        return self._one("record_external_id", ext_id)

    def run_by_id(self, run_id: str) -> RunEntry | None:
        return self._one("run_id", run_id)

    def open_runs(self, limit: int) -> list[RunEntry]:
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM runs WHERE status IN ('sent','running') "
            "AND run_id IS NOT NULL "
            "ORDER BY progress_at IS NOT NULL, progress_at, record_external_id LIMIT ?",
            (limit,),
        ).fetchall()
        return [_entry(r) for r in rows]

    def pending_sink(self) -> list[RunEntry]:
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM runs WHERE sink_pending=1 ORDER BY record_external_id"
        ).fetchall()
        return [_entry(r) for r in rows]

    def runs_of_case(self, case_id: str) -> list[RunEntry]:
        rows = self._db.execute(
            f"SELECT {_COLUMNS} FROM runs WHERE case_id=? ORDER BY record_external_id",
            (case_id,),
        ).fetchall()
        return [_entry(r) for r in rows]

    def mark_view_read(self, run_id: str, view: str) -> None:
        self._db.execute(
            "INSERT OR IGNORE INTO views_read(run_id, view) VALUES (?,?)", (run_id, view)
        )

    def views_read(self, run_id: str) -> list[str]:
        rows = self._db.execute(
            "SELECT view FROM views_read WHERE run_id=? ORDER BY view", (run_id,)
        ).fetchall()
        return [r[0] for r in rows]

    def set_status(
        self, run_id: str, status: str, error_code: str | None, *, only_open: bool
    ) -> None:
        sql = "UPDATE runs SET status=?, error_code=? WHERE run_id=?"
        if only_open:
            sql += " AND status IN ('sent','running')"
        self._db.execute(sql, (status, error_code, run_id))

    def touch_progress(self, run_id: str, at: str) -> None:
        self._db.execute("UPDATE runs SET progress_at=? WHERE run_id=?", (at, run_id))

    def clear_refusals(self, record_external_id: str | None) -> int:
        sql = "DELETE FROM runs WHERE status IN ('refused_final', 'no_run')"
        args: tuple = ()
        if record_external_id is not None:
            sql += " AND record_external_id=?"
            args = (record_external_id,)
        return self._db.execute(sql, args).rowcount
