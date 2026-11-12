# MAKIA integration example

A small, readable integration between a customer database and MAKIA. It keeps MAKIA's projects
and members in step with the database, sends records to MAKIA, and copies the results back, with their review
state and a link into Studio per row.

It is an example to read and copy, not a product. About 600 lines of Python, one dependency (`httpx`).

## What it does

| Command | Does | API |
|---|---|---|
| `makia-integration sync` | Sends every project's full state (name, archived, members). Adds the run account to each. | `PUT /sync/projects/{id}` |
| `makia-integration send` | Starts a run for every record not sent yet, with its files by reference. | `POST /inbound/projects/{id}/workflows/{slug}/runs` |
| `makia-integration poll` | Reads the change feed from its marker, updates run status, copies results after `run.views_built`, re-reads them after a review. | `GET /changes`, `GET /runs/{id}/results/{view}`, `GET /runs/{id}/progress`, `GET /runs/{id}/views` |
| `makia-integration listen --port 8080` | Receives MAKIA webhooks and polls on each one (see below). | |
| `makia-integration webhook create --name N --connection <uuid> --path /hook [--project ID]… [--workflow ID]… [--blueprint ID]… [--type T]…` | Creates a webhook with the run token; prints its id and the signing secret once. | `POST /webhooks` |
| `makia-integration webhook list` | Id, name, enabled, switched_off_reason, filters and last_seq of each webhook. | `GET /webhooks` |
| `makia-integration webhook enable\|disable <id>` | Switches a webhook on or off; it keeps its place. | `PATCH /webhooks/{id}` |
| `makia-integration webhook rotate-secret <id>` | Replaces the signing secret; prints the new one once. | `POST /webhooks/{id}/rotate-secret` |
| `makia-integration webhook delete <id>` | Deletes a webhook. | `DELETE /webhooks/{id}` |
| `makia-integration webhook deliveries <id> [--limit N]` | The newest deliveries and their status. | `GET /webhooks/{id}/deliveries` |
| `makia-integration run` | All three in a loop. | |

`makia-integration poll` also reads `GET /runs/{id}/progress` for runs still `sent` or `running` (at most 50 per poll),
sets the status `running` and stores the answer in `integration_runs.progress_json` / `progress_at`. The answer has item
counts per running node and a `stale` flag, but no total or percentage. A final status normally comes from the feed.
A run that progress already shows finished ended before the integration's feed marker, so its events will never be read
(for example a record sent again after the integration lost its database, answered with its known run). The integration then
takes the status from progress and copies every built view (`GET /runs/{id}/views`).

Every step is safe to repeat. The integration keys everything by the customer's own ids (`external_id`) and never
deletes anything in MAKIA.

## Tables

The integration reads four **source tables**, which stand in for the customer's system (in a real integration, make them
views over it):

- `src_projects` (`external_id`, `name`, `archived`)
- `src_members` (`project_external_id`, `subject` = the person's SSO identity, `permissions` = `review,cases,…`)
- `src_records` (`external_id`, `project_external_id`, `workflow_slug`, `input_id`)
- `src_record_files` (`record_external_id`, `path` relative to the storage connection's root)

It writes three of its own:

- `integration_state`: the feed marker
- `integration_runs`: record → case, run, `review_url`, status, error code, last run progress
- `integration_results`: one row per (run, view, `unit_key`) with the row data as JSON, open findings, review state,
  chosen option and Studio link

Schemas: [`makia_integration/schema/sqlite.sql`](makia_integration/schema/sqlite.sql),
[`makia_integration/schema/mssql.sql`](makia_integration/schema/mssql.sql).

## What you need

- Python 3.12 or newer.
- A MAKIA organization in which you manage the settings (for the service accounts and the storage connection).
- To only read the code and run the tests, nothing else: `pytest` runs against a fake MAKIA (see Tests).

## Setup in MAKIA

Once, by an organization admin:

1. Set up single sign-on (members are named by their SSO identity).
2. Organization settings → Service accounts: add two accounts, a **sync account** and a **run account**. Mint a
   token for each; for the sync account tick **Mint as sync token**. Each secret is shown once.
3. Organization settings → Connector Connections: add the storage connection that holds the files. Note its id.
4. Make sure the workflows the integration starts exist in the projects under the slugs `src_records` names.

The full guide is in Studio under Docs → Integrations (`/docs/integration`).

## Webhooks instead of waiting

`makia-integration listen` replaces the sleep in `makia-integration run` with a push. MAKIA tells the integration when the feed moved; the
integration answers `204` at once and polls. The delivery body is never trusted for content: `/changes` stays the
truth, so a repeated, late or reordered delivery only costs a poll that finds nothing new. Deliveries arriving
during a poll are merged into exactly one more poll after it.

1. An organization admin adds the address MAKIA delivers to, once: Organization settings → External services →
   Add external service, with the base URL where `makia-integration listen` is reachable (for example
   `https://integration.example.com`) and that host in its allowed hosts. MAKIA posts to the base URL plus the
   webhook's `--path`, with the service's auth header (for a proxy in front to check; `makia-integration listen` itself
   checks the signature, step 4). Note its id.
2. Create the webhook with the **run account's** token (`MAKIA_RUN_TOKEN`):
   `makia-integration webhook create --name integration --connection <connection-uuid> --path /hook [--type run.views_built]`.
   It prints the webhook id and the signing secret once. `--project`, `--workflow`, `--blueprint` and `--type`
   (each repeatable) narrow what it sends; without them it sends every change the run account may see. Run events
   come once the run has ended. Manage it later with `makia-integration webhook list|enable|disable|rotate-secret|delete|deliveries`;
   `deliveries` shows each attempt and the answer code.
3. `export MAKIA_WEBHOOK_SECRET=…` and run `makia-integration listen --port 8080`. Run `makia-integration send` on its own schedule.
4. `makia-integration listen` binds `0.0.0.0` and speaks plain HTTP: put it behind a TLS-terminating proxy. Every request is checked: `X-Makia-Signature` is `sha256=` plus the hex HMAC-SHA256 of `<X-Makia-Timestamp>.<body>`
   under the secret; timestamps older than five minutes are refused (`makia_integration/receiver.py`).

## Run it

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install .            # or: pip install .[mssql] for MS SQL
export MAKIA_API_URL=https://makia.example.com/api
export MAKIA_SYNC_TOKEN=makia_…            # the sync account's secret
export MAKIA_RUN_TOKEN=makia_…             # the run account's secret
export MAKIA_STORAGE_CONNECTION_ID=…       # the storage connection's id
export INTEGRATION_DB=integration.db      # a SQLite file

makia-integration init-db
sqlite3 integration.db < sample/seed.sql  # invented sample data
makia-integration sync
makia-integration send                   # a new integration first stores the feed's head
makia-integration poll
makia-integration run --interval 60      # or keep it running
```

### Files from a folder (SMB share, SFTP drop, local)

Set `MAKIA_FILES_DIR` to the folder that holds the files, and the integration uploads each record's files to MAKIA itself (multipart) instead of naming paths in a storage connection. `MAKIA_STORAGE_CONNECTION_ID` is then not needed. The paths in `src_record_files` are read relative to the folder. `sample/files` matches the sample data and works out of the box:

```sh
export MAKIA_FILES_DIR=sample/files
```

Mount a network share read-only. On Linux:

```sh
sudo mount -t cifs //server/share /mnt/belege -o credentials=/etc/makia-smb.cred,ro
export MAKIA_FILES_DIR=/mnt/belege
```

On Windows use a UNC path (`\\server\share\belege`) or a mapped drive. A record with a missing file, or a path that leaves the folder, is skipped with a warning and sent on the next run once fixed. MAKIA takes up to 50 MB per file; a record with a bigger file is marked `refused` (`file_too_large`), as is one with an empty file (`empty_file`) or one MAKIA refuses (`unsupported_media_type`, `too_many_files`, `unknown_input`). An unreadable file (no permission) skips the record like a missing one. Send bigger files through a storage connection.

On MS SQL, point `INTEGRATION_DB` at an ODBC connection string:

```sh
export INTEGRATION_DB='odbc:DRIVER={ODBC Driver 18 for SQL Server};SERVER=…;DATABASE=integration;Trusted_Connection=yes'
```

The code uses plain DB-API with `?` parameters and UPDATE-then-INSERT, so it runs unchanged on both.

## How it handles the awkward cases

- **Access tokens.** Each secret is swapped at `POST /auth/token` for a 15-minute access token; a `401` swaps again
  once. The inbound route takes the run account's secret directly.
- **A lost answer.** If `send` crashed after MAKIA answered, the record has no `integration_runs` row and is sent again.
  MAKIA knows the `external_id`, answers `200` with the existing case, and writes nothing.
- **A project leaving the source.** Mark it `archived` in `src_projects`. A project that just disappears is only
  logged: absence never archives.
- **A view rebuilt while reading.** MAKIA answers `409 generation_changed`; the integration reads again from the top.
  Rows a rebuild dropped are deleted afterwards.
- **Reviews.** `row.reviewed` re-reads that run's view; `match.changed` re-reads every view of every run of the case,
  because a pair decision stands for the whole case.
- **Failed runs.** `run.failed` keeps `error_code` (`inbound_import_empty`: the paths matched no file;
  `inbound_project_archived`: stop sending there).

## Tests

```sh
pip install .[dev]
pytest
```

The tests drive the integration against an in-memory fake of MAKIA's API (`tests/fake_makia.py`). One test checks that
every route the integration calls exists in MAKIA's API description: the copy in `openapi.json` here, or your
instance's own (`MAKIA_OPENAPI=path/to/openapi.json`, served by MAKIA at `GET /openapi.json`).

## License

MIT, see [LICENSE](LICENSE).
