# AGENTS.md

Guidance for coding agents (Copilot, Claude Code, Cursor, Codex, …) working in this repository. People start
with [README.md](README.md).

## What this is

An example bridge between a customer's database and MAKIA. It is meant to be copied and adapted, not used as a
library. The usual job is to **fit it to one customer's system**: replace the source tables with views or queries
over their data, pick the send mode, and run it on their schedule.

MAKIA's API is described in [`openapi.json`](openapi.json). Your MAKIA instance serves its own at
`GET /openapi.json`, and its user guide is in Studio under Docs → Integrations (`/docs/integration`).

## Layout

| File | Holds |
|---|---|
| `makia_bridge/api.py` | `MakiaClient`: one service account's calls; `ROUTES_USED`, every route the bridge calls |
| `makia_bridge/jobs.py` | `Bridge`: the jobs `sync`, `send`, `poll` and the result copy |
| `makia_bridge/db.py` | DB-API helpers (`rows`, `execute`, `upsert`, state); SQLite or MS SQL via ODBC |
| `makia_bridge/schema/*.sql` | the source tables (`src_*`) and the bridge's own (`bridge_*`) |
| `makia_bridge/receiver.py` | `bridge listen`: webhook receiver, checks the signature, then polls |
| `makia_bridge/cli.py` | the `bridge` command and its environment variables |
| `tests/fake_makia.py` | an in-memory fake of MAKIA's API over `httpx.MockTransport` |

## Adapting it to a customer

- **Read their data through the four `src_*` tables.** Make them views over the customer's system (or change the
  `SELECT`s in `jobs.py`). Keep the column meanings: `external_id` is the customer's own stable id, and
  `src_members.subject` is the person's single sign-on identity.
- **Choose how files reach MAKIA**:
  - Files in S3-compatible storage or on an SFTP server MAKIA can reach: a storage connection. Leave
    `MAKIA_FILES_DIR` unset and set `MAKIA_STORAGE_CONNECTION_ID`; `src_record_files.path` is relative to the
    connection's root.
  - Anything else in their network (SMB share, archive or DMS export, local folder): set `MAKIA_FILES_DIR`. The
    bridge uploads each file itself (multipart, at most 50 MB per file), with paths relative to that folder.
- **Two service accounts, two tokens.** The sync token (`MAKIA_SYNC_TOKEN`) may only call `/sync/*`. The run token
  (`MAKIA_RUN_TOKEN`) does everything else. Don't merge them, and never put either one in code, logs or the
  database.
- **Results land in `bridge_results`**, one row per view row with its data as JSON. To write results into the
  customer's own tables, read from there or change `read_results` in `jobs.py`.

## Rules that keep the bridge correct

- **Every job must be safe to run again.** Everything is keyed by `external_id`, and writes go UPDATE-then-INSERT
  (`db.upsert`). MAKIA answers a repeated `external_id` with the existing case and writes nothing.
- **Never delete or archive in MAKIA because something is missing from the source.** A project leaves only when
  `archived` is set.
- **The change feed (`GET /changes`) is the truth.** Webhook bodies are only a nudge to poll; never read content
  from them. Keep the feed marker (`bridge_state`) in the same database as the results.
- **Treat the known refusals as final for that record** (`REFUSALS` in `jobs.py`): mark the record `refused` with the
  code instead of retrying forever. Retry other errors on the next run.
- **Portable SQL only:** `?` parameters, no `ON CONFLICT` / `MERGE`, no dialect-specific types outside
  `schema/*.sql`. The same code runs on SQLite and MS SQL.
- **One runtime dependency (`httpx`).** Add another only if the customer's system needs it (for example a database
  driver), and as an optional extra.

## Calling a new route

1. Find it in `openapi.json` (or the instance's own). Only use routes described there; they are the stable API.
2. Add a method to `MakiaClient` and the route to `ROUTES_USED`.
3. Teach `tests/fake_makia.py` to answer it.
4. Add a test in `tests/test_bridge_jobs.py`. `tests/test_bridge_contract.py` drives every job against the fake,
   refuses any call outside `ROUTES_USED`, and checks every entry against `openapi.json`
   (`MAKIA_OPENAPI=path/to/openapi.json` checks against your instance's).

## Checks

```sh
pip install .[dev] ruff
ruff check . && ruff format --check .
pytest -q
```

All three run in CI and must pass. The tests need no MAKIA instance.

## Don't

- Add customer names, hosts, credentials or real documents to this repository. Sample data is invented.
- Read a file whose resolved path leaves `MAKIA_FILES_DIR` (`..`, absolute paths, symlinks pointing out).
- Send a record with some of its files missing. Skip it and send it once all files are there.
