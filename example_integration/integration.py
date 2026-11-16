"""The integration the command loads: `makia-integration --integration example_integration.integration:integration run`."""

from __future__ import annotations

import os

from makia_integration import Integration, Config

from example_integration.db import Connection, connect, create_schema
from example_integration.sink import DbSink
from example_integration.source import DbSource


def integration(config: Config) -> Integration:
    sync_api, run_api = config.clients()
    try:
        state = config.open_state()
    except BaseException:
        sync_api.close()
        run_api.close()
        raise
    conn: Connection | None = None
    try:
        conn, dialect = connect(os.environ.get("INTEGRATION_DB", "integration.db"))
        create_schema(conn, dialect)  # idempotent: IF NOT EXISTS
        return Integration(
            DbSource(conn),
            DbSink(conn),
            sync_api=sync_api,
            run_api=run_api,
            state=state,  # Integration.close() also calls DbSink.close(), which closes conn
            storage_connection_id=config.storage_connection_id,
            files_dir=config.files_dir,
            run_account_permissions=config.run_account_permissions,
        )
    except BaseException:
        if conn is not None:
            conn.close()
        state.close()  # or the next attempt finds the state file locked
        sync_api.close()
        run_api.close()
        raise
