# AGENTS.md

Guidance for coding agents (Copilot, Claude Code, Cursor, Codex, ...) working in this repository. People start
with [README.md](README.md).

## What this is

A integration between a customer's database and MAKIA, built on the `makia-integration` package (in
`makia-integration-py/`). The example is meant to be copied and adapted. The usual job is to **fit it to one
customer's system**: replace the source tables with views or queries over their data and point the sink at their
tables. Everything generic lives in the package: read its guide
(`makia-integration-py/makia_integration/docs/guide.md`, or `makia-integration docs`) before changing behaviour.

MAKIA's API is described in [`openapi.json`](openapi.json). Your MAKIA instance serves its own at
`GET /openapi.json`.

## Layout

| File | Holds |
|---|---|
| `example_integration/source.py` | `DbSource`: the `Source` the package reads (projects, members, records, files) |
| `example_integration/sink.py` | `DbSink`: the `Sink` the package writes to (`integration_runs`, `integration_results`) |
| `example_integration/db.py` | DB-API helpers (`rows`, `execute`, `upsert`); SQLite or MS SQL via ODBC |
| `example_integration/integration.py` | `integration(config) -> Integration`, loaded with `makia-integration --integration example_integration.integration:integration` |
| `example_integration/schema/*.sql` | the source tables (`src_*`) and the result tables (`integration_*`) |

## Adapting it to a customer

- **Read their data through the four `src_*` tables.** Make them views over the customer's system (or change the
  `SELECT`s in `source.py`). Keep the column meanings: `external_id` is the customer's own stable id, and
  `src_members.subject` is the person's single sign-on identity.
- **Choose how files reach MAKIA**: a storage connection (`MAKIA_STORAGE_CONNECTION_ID`, paths relative to its
  root) or an upload from a folder (`MAKIA_FILES_DIR`, at most 50 MB per file). See the package guide.
- **Two service accounts, two tokens** (`MAKIA_SYNC_TOKEN`, `MAKIA_RUN_TOKEN`). Never put either in code, logs or the
  database.
- **Results land in `integration_results`.** To write into the customer's own tables, change `DbSink`.

## Rules that keep the integration correct

- **Every sink method must be safe to repeat.** The package calls at least once per event. Writes go
  UPDATE-then-INSERT (`db.upsert`); `view_done` deletes the rows of other generations.
- **Never delete or archive in MAKIA because something is missing from the source.** A project leaves only when
  `archived` is set.
- **The package owns the change feed and the run bookkeeping** (its state file). The sink only stores results.
- **Portable SQL only:** `?` parameters, no `ON CONFLICT` / `MERGE`, no dialect-specific types outside
  `schema/*.sql`.

## Checks

```sh
pip install ./makia-integration-py[dev] .[dev] ruff
ruff check . && ruff format --check .
pytest -q makia-integration-py/tests
pytest -q tests
```

All of them run in CI and must pass. The tests need no MAKIA instance.

## Don't

- Add customer names, hosts, credentials or real documents to this repository. Sample data is invented.
- Edit `makia-integration-py/` here: it is a copy published from the MAKIA repository.
