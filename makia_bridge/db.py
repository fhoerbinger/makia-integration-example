"""The bridge's database: the customer's tables it reads and its own tables.

Plain DB-API with `?` parameters, which both `sqlite3` and `pyodbc` (MS SQL)
understand. Writes use UPDATE-then-INSERT instead of a vendor upsert, so the
same code runs on both.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Protocol

SCHEMA_DIR = Path(__file__).parent / "schema"


class Connection(Protocol):
    def cursor(self) -> Any: ...
    def commit(self) -> None: ...
    def close(self) -> None: ...


def connect(url: str) -> tuple[Connection, str]:
    """`odbc:<connection string>` opens MS SQL through pyodbc; anything else
    is a SQLite file path. Answers the connection and its dialect."""
    if url.startswith("odbc:"):
        import pyodbc  # optional dependency: pip install .[mssql]

        return pyodbc.connect(url.removeprefix("odbc:")), "mssql"
    # `bridge listen` builds the connection on the main thread but polls on the
    # coalescer's one worker thread. Only that thread touches it afterwards (polls
    # never overlap), so sharing it across threads is safe.
    return sqlite3.connect(url, check_same_thread=False), "sqlite"


def create_schema(conn: Connection, dialect: str) -> None:
    script = (SCHEMA_DIR / f"{dialect}.sql").read_text()
    cur = conn.cursor()
    for statement in script.split(";"):
        if statement.strip():
            cur.execute(statement)
    conn.commit()


def rows(conn: Connection, sql: str, *params: Any) -> list[tuple[Any, ...]]:
    cur = conn.cursor()
    cur.execute(sql, params)
    return [tuple(r) for r in cur.fetchall()]


def execute(conn: Connection, sql: str, *params: Any) -> int:
    cur = conn.cursor()
    cur.execute(sql, params)
    return cur.rowcount


def upsert(conn: Connection, table: str, key: dict[str, Any], values: dict[str, Any]) -> None:
    """Update the row with `key`, or insert it. Table and column names come
    from this code, never from input."""
    sets = ", ".join(f"{c} = ?" for c in values)
    where = " AND ".join(f"{c} = ?" for c in key)
    if values and execute(
        conn, f"UPDATE {table} SET {sets} WHERE {where}", *values.values(), *key.values()
    ):
        return
    cols = [*key, *values]
    marks = ", ".join("?" for _ in cols)
    execute(
        conn,
        f"INSERT INTO {table} ({', '.join(cols)}) VALUES ({marks})",
        *key.values(),
        *values.values(),
    )


def get_state(conn: Connection, name: str) -> str | None:
    found = rows(conn, "SELECT value FROM bridge_state WHERE name = ?", name)
    return found[0][0] if found else None


def set_state(conn: Connection, name: str, value: str) -> None:
    upsert(conn, "bridge_state", {"name": name}, {"value": value})
