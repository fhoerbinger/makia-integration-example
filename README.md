# MAKIA integration example

A small integration between a customer database and MAKIA, built on the
[`makia-integration`](makia-integration-py/README.md) package. It keeps MAKIA's projects and members in step with
the database, sends records to MAKIA, and copies the results back with their review state and a link into Studio
per row.

It is an example to read and copy, not a product: a `Source` and a `Sink` over sample tables, about 250 lines.
Everything generic (the commands, setup in MAKIA, webhooks, files from a folder, troubleshooting) is in the
package's guide: [`makia-integration-py/makia_integration/docs/guide.md`](makia-integration-py/makia_integration/docs/guide.md),
or `makia-integration docs` once installed.

## What is here

| File | Holds |
|---|---|
| `example_integration/source.py` | `DbSource`: projects, members, records and files, read from the `src_*` tables |
| `example_integration/sink.py` | `DbSink`: writes `integration_runs` and `integration_results` |
| `example_integration/db.py` | `connect` (SQLite file, or `odbc:` + an ODBC connection string for MS SQL), `create_schema`, small DB-API helpers |
| `example_integration/integration.py` | `integration(config)`: the factory the command loads |
| `example_integration/schema/*.sql` | the source tables and the example's result tables, for SQLite and MS SQL |
| `sample/` | invented sample data and files |

## Tables

The source tables stand in for the customer's system (in a real integration, make them views over it):

- `src_projects` (`external_id`, `name`, `archived`)
- `src_members` (`project_external_id`, `subject` = the person's SSO identity, `permissions` = `review,cases,...`)
- `src_records` (`external_id`, `project_external_id`, `workflow_slug`, `input_id`)
- `src_record_files` (`record_external_id`, `path`)

The example writes two of its own (the package keeps its feed marker and run bookkeeping in its own state file):

- `integration_runs`: record -> case, run, `review_url`, status, error code
- `integration_results`: one row per (run, view, `unit_key`) with the row data as JSON, open findings, review state,
  chosen option, Studio link and the view generation

Schemas: [`example_integration/schema/sqlite.sql`](example_integration/schema/sqlite.sql),
[`example_integration/schema/mssql.sql`](example_integration/schema/mssql.sql).

## Run it

Setup in MAKIA (service accounts, storage connection) is in the package guide.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install ./makia-integration-py .          # or: pip install ./makia-integration-py .[mssql]
export MAKIA_API_URL=https://makia.example.com/api
export MAKIA_SYNC_TOKEN=makia_...             # the sync account's secret
export MAKIA_RUN_TOKEN=makia_...              # the run account's secret
export MAKIA_STORAGE_CONNECTION_ID=...        # or MAKIA_FILES_DIR=sample/files for an upload from a folder
export MAKIA_INTEGRATION=example_integration.integration:integration
export INTEGRATION_DB=integration.db          # a SQLite file; created on first use

makia-integration doctor
sqlite3 integration.db < sample/seed.sql      # invented sample data (the tables exist after the first command)
makia-integration sync
makia-integration send
makia-integration poll
makia-integration run --interval 60           # or keep it running
```

Or pass the integration per command: `makia-integration --integration example_integration.integration:integration sync`.

On MS SQL, point `INTEGRATION_DB` at an ODBC connection string:

```sh
export INTEGRATION_DB='odbc:DRIVER={ODBC Driver 18 for SQL Server};SERVER=...;DATABASE=integration;Trusted_Connection=yes'
```

The code uses plain DB-API with `?` parameters and UPDATE-then-INSERT, so it runs unchanged on both.

## Tests

```sh
pip install ./makia-integration-py[dev] .[dev]
pytest -q
```

The tests drive the integration through the package against its in-memory fake of MAKIA (`makia_integration.testing`).
The package's own tests include the check that every route it calls exists in MAKIA's API description
(`makia-integration-py/tests`).

## License

MIT, see [LICENSE](LICENSE).
