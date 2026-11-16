# makia-integration: setup guide

A integration connects a customer system to MAKIA. It reads projects, members and
records (with files) from your system, sends the records to MAKIA, and writes the
results back into your tables, each with a link into Studio.

You write two small classes, a **source** (what to send) and a **sink** (where
results go). The package does the rest: the loop, retries, the change feed, the
state file, webhooks, the command. This guide needs no internet.

Words used here: **integration** = your source + sink + this package. **Run** = one
MAKIA run for one record. **View** = a table of results a run builds. **Result
row** = one row of a view. **State file** = the integration's own small database file.

## Install

Needs Python 3.12 or newer. The package depends on `httpx` only.

Online:

    python -m venv venv
    venv/bin/pip install makia_integration-0.1.0-py3-none-any.whl

Offline (no internet on the server): on a machine with internet and the same
operating system and Python version, collect the wheels, copy the folder over:

    pip download makia_integration-0.1.0-py3-none-any.whl -d wheels

On the server:

    python -m venv venv
    venv/bin/pip install --no-index --find-links wheels makia-integration

On Windows use `venv\Scripts\pip` and `venv\Scripts\makia-integration`. Add your
database driver (pyodbc, pymssql, ...) the same way; the package has none.

Check: `makia-integration version`. This guide's folder: `makia-integration docs`.
It also holds `openapi.json` (MAKIA's API description, when it was built with the
repository) and `template/` (the starter integration).

## The five-minute path

1. In Studio, create two service accounts and note their tokens: the **sync
   account** (creates projects and members) and the **run account** (sends
   records, reads results). Note the id of the storage connection if files are
   sent by reference.
2. Write the starter: `makia-integration new my-integration`. It refuses a folder that
   is not empty. In it: `integration.py`, `test_integration.py`, `.env.example`,
   `makia-integration.service`, `README.md`.
3. In `my-integration`, run `pytest`. It passes before you change anything.
4. Copy `.env.example` to `.env`; fill in `MAKIA_API_URL`, `MAKIA_SYNC_TOKEN`,
   `MAKIA_RUN_TOKEN`, and `MAKIA_STORAGE_CONNECTION_ID` (files by reference) or
   `MAKIA_FILES_DIR` (files uploaded from a folder). Set `MAKIA_INTEGRATION=integration:integration`.
5. In `integration.py`, replace `connect()` and each `# TODO` query with your own.
6. **Replace `connect()` first**: as shipped the starter sends its sample rows to the
   MAKIA in your env file. Then `makia-integration --env-file .env doctor`. Fix every FAIL line (each has a fix
   line under it). `doctor` never writes to MAKIA. `--listen 8080` also checks the
   port can be bound.
7. Run the steps once by hand, in this order:

       makia-integration --env-file .env sync
       makia-integration --env-file .env send
       makia-integration --env-file .env poll

   `sync` sends projects and members. `send` starts a run for each new record.
   `poll` reads what happened since the last poll and writes results through the
   sink.
8. Keep it running: `makia-integration --env-file .env run` (see "Running as a service").
   Webhooks (`--listen`) are a later step, described there.

Options before the command: `--env-file PATH`, `--integration module:function`
(or `MAKIA_INTEGRATION`), `--log-file PATH` (rotating file, 6 files of 5 MB), `-v`
(debug log and tracebacks). Settings in the real environment win over the env file.

Settings (environment variables or env file):

| Name | Meaning |
|---|---|
| `MAKIA_API_URL` | MAKIA's address |
| `MAKIA_SYNC_TOKEN` | the sync account's token |
| `MAKIA_RUN_TOKEN` | the run account's token |
| `MAKIA_STORAGE_CONNECTION_ID` | files by reference: the storage connection (this or `MAKIA_FILES_DIR`) |
| `MAKIA_FILES_DIR` | files by upload: the folder (50 MB per file) (this or `MAKIA_STORAGE_CONNECTION_ID`) |
| `MAKIA_STATE_FILE` | the state file (default `makia-state.db`) |
| `MAKIA_CA_BUNDLE` | PEM file with a private CA (exit 2 if it cannot be read) |
| `MAKIA_INTEGRATION` | `module:function` of the integration |
| `MAKIA_WEBHOOK_SECRET` | the webhook's secret; required for `--listen` (exit 2 without it) |

Set exactly one of `MAKIA_STORAGE_CONNECTION_ID` and `MAKIA_FILES_DIR`. `send`, `run`
and `listen` stop with exit code 2 when both or neither are set (`doctor` shows a FAIL
line). If `MAKIA_FILES_DIR` is set, files are uploaded and `files()` returns paths relative to
that folder (an absolute path, or one that leaves the folder, is skipped). Otherwise `files()` returns paths in the storage connection.

Commands: `sync`, `send`, `poll`, `run`, `listen`, `retry`, `doctor`, `new`,
`docs`, `version`, `webhook`.

Exit codes: **0** done, **1** a job failed (MAKIA or your database could not be
reached, an error in the integration, a project `sync` could not send), **2** configuration
error (a missing setting, both or neither of the two file settings, an unreadable
`MAKIA_CA_BUNDLE`, the integration cannot load, the state file is in use). A failure prints
one line to stderr; add `-v` for the traceback. `run` and `listen` stopped by `Ctrl+C`
or SIGTERM exit 0, even if a round failed before (that failure is in the log).

## Source and Sink

You write one class for each. Both are plain classes; no base class is needed.

### Source

    def projects(self) -> Iterable[Project]     # external_id, name, archived
    def members(self)  -> Iterable[Member]      # project_external_id, subject, permissions
    def records(self)  -> Iterable[Record]      # external_id, project_external_id,
                                                #   workflow_slug, input_id
    def files(self, record) -> list[str]        # the record's file paths

- `external_id` is your own id. It must never change: MAKIA keys everything by it.
- `projects()` returns every project every time. A project that is closed must be
  returned with `archived=True`. A project that is simply no longer returned is
  **not** archived; `sync` only logs a warning about it.
- `members()` returns every member of every project. `subject` is the person's
  identity as MAKIA knows it from the login. `permissions` is a tuple of names.
  Do not list the run account: it is added to every project automatically, with
  the permissions `cases, run, review`. Listing it stops `sync` with an error.
- `records()` may return every record every time. The package skips those it
  already sent, and sending one again is harmless anyway (MAKIA answers 200 and
  writes nothing). `workflow_slug` names the MAKIA workflow; `input_id` names its
  input.
- `files()` returns the paths of the record's files. No files: the record is not
  sent this pass and the sink hears `record_skipped(record, "no files")`.
  With `MAKIA_FILES_DIR` a path that is missing, not readable, or outside the
  folder is skipped the same way and tried again next pass.
- The source reads only. It is called on every pass, so keep the queries cheap.

### Sink

    def run_sent(self, record, run)                          # SentRun: case_id, run_id,
                                                             #   review_url, known
    def run_refused(self, record, code)                      # see "Refusals"
    def record_skipped(self, record, reason)                 # not sent this pass
    def run_status(self, state)                              # RunState: ref, status,
                                                             #   error_code, progress
    def result_rows(self, ref, view, generation, rows)       # one page of a view
    def view_done(self, ref, view, generation)               # all pages are in

`ref` (RunRef) has `record_external_id`, `case_id`, `run_id`. A result row
(ResultRow) has `unit_key`, `row_key`, `data` (a dict), `findings_open`,
`review_state`, `review_option`, `review_key` and `url`, the link into Studio.
Status is one of `sent`, `running`, `succeeded`, `failed`, `cancelled`, or `gone`
(the run was deleted in MAKIA; `error_code` is `run_not_found`, and the run is not
read again).

**Every sink method must be safe to call twice with the same arguments.** The
package delivers at least once and never loses a call, so after a crash or an
error in your sink the call is made again. In SQL: update the row, and insert it
if nothing was updated. Do not append.

What the package promises:

- `send`: MAKIA answers, then the run is stored in the state file marked as not
  yet delivered, then `run_sent` is called, then the mark clears. If `run_sent`
  raises, it is called again on the next pass.
- `poll`: for each page of the change feed the package updates its state and calls
  the sink, then moves the marker. If the sink raises, the marker stays, the poll
  stops, and the next poll repeats that page.
- Results come page by page (views can be large). Key rows by (run, view,
  `unit_key`) and store the `generation` with each row. When MAKIA rebuilds a
  view while it is read, delivery starts again from the first page with a new
  generation; some old-generation rows may still be there for a moment. Only
  `view_done(ref, view, generation)` means the view is complete: delete the rows
  of that (run, view) whose generation is a different one.
- `run_refused` and `record_skipped` can be called again on each pass for the same
  record until it is fixed or sent.
- A run's status and results come only after its `run_sent` was delivered. Events
  that arrive before it are skipped; once `run_sent` went through, the run's status and
  views are caught up from its progress.
- `run_sent` can come again for a run that already has a final status: keep that
  status (the starter sets `sent` only for a new run of the record).

The package never writes to your database except through the sink. It never
creates tables there.

### The factory

The command loads `def integration(config: Config) -> Integration`. Build the source, the
sink and the integration in it:

    sync_api, run_api = config.clients()
    state = config.open_state()        # opens and locks the state file
    ...
    return Integration(source, sink, sync_api=sync_api, run_api=run_api, state=state,
                  storage_connection_id=config.storage_connection_id,
                  files_dir=config.files_dir,
                  run_account_permissions=config.run_account_permissions)

If anything after `open_state()` can raise, close the state in that case (the
starter does), or the next attempt finds the state file locked.

`Integration.close()` closes the state, both clients, and the source and sink if they have a
`close()` method: put the closing of your database connection there (the starter's
`DbSink.close()`). The command calls it when a job ends, and `run` calls it after a
round in which a step failed: the next round calls the factory again, so a dropped
database connection or a broken network session recovers by itself.

All jobs run on **one worker thread**, and the factory is called on it. A
database connection that is not thread-safe is therefore fine, as long as the
factory creates it.

Parameter style: sqlite3 and pyodbc use `?`, pymssql uses `%s`. The starter has
one constant for it (`PARAM`).

### Testing

`makia_integration.testing.FakeMakia` is an in-memory MAKIA. `test_integration.py` in
the starter shows how to use it: sync, send, `makia.set_rows(run_id, view, rows)`
plus `makia.emit("run.views_built", run_id=..., views=[...])`, then poll. The
helper `row("unit-key")` builds a result row.

## The state file

The integration keeps a small SQLite file (`MAKIA_STATE_FILE`, default
`makia-state.db` in the folder where it runs). It holds the **feed marker**
(how far the change feed was read), the **runs this integration started** (record,
case, run, status), and which views were read. It never touches your database.

- **One process per state file.** The integration locks it through a second file
  beside it (`<name>.lock`). A second process (a cron overlap, a second
  operator) stops with `... is in use: another makia-integration process is
  using it` and exit code 2. Stop the service before running a command by hand.
- Keep it on a disk that is backed up and writable by the service user. Do not
  put it on a network share.
- **Never delete it while the integration runs.**
- Deleting it (integration stopped) means: the marker starts again at the current
  head of the feed, so events from the time in between are not read; runs
  already sent are found again by their external id (MAKIA answers 200, nothing
  is started twice, the sink hears `run_sent` again); every run found again is caught
  up from its progress, and the views of the ones that finished are delivered again
  (the sink must cope, see above). Records refused for good are forgotten and tried
  again.
- The package upgrades the file's format itself when you install a newer version.

The marker starts at the feed's head the first time, before the first send, so a
new integration does not replay history. A run that already ended before the marker
is caught up from its progress, not from the feed.

## Refusals

MAKIA can refuse a record when it is sent. The sink hears it as
`run_refused(record, code)`.

**Fixable**: the cause is outside the record. The package tries the record again
on every later pass and reports it each pass until it works.

| Code | What to do |
|---|---|
| `project_not_found` | The project is not in MAKIA. Run `sync`; check the record's `project_external_id` matches a source project. |
| `project_archived` | The project is archived. Un-archive it in your source (`archived=False`) and `sync`, or stop returning its records. |
| `workflow_not_found` | The `workflow_slug` does not exist in the project. Check the slug; make sure the workflow is imported in that project in Studio. |
| `workflow_not_published` | The workflow has no published version. Publish it in Studio. |
| `workflow_archived` | The workflow is archived in Studio. Restore it, or send to another workflow. |
| `connection_not_found` | `MAKIA_STORAGE_CONNECTION_ID` is not a storage connection of the organization. Fix the id (see `doctor`). |
| `missing_file` | MAKIA did not find a file by reference. Check the paths `files()` returns exist in the storage connection. |

**Final**: the record itself is wrong. The package does not try it again and
tells the sink once. Fix the file or the record, then clear the refusal:

    makia-integration retry DOC-77812        # one record, by external id
    makia-integration retry --all-refused    # every record refused for good

`retry` edits the state file only; the next `send` sends the record again
(refusals for good and `no_run` records). It
needs `MAKIA_STATE_FILE` (or the default file) and, like every command that uses
the state file, the service must not be running at that moment.

| Code | What to do |
|---|---|
| `file_too_large` | A file is over 50 MB (upload mode). Send it by reference through a storage connection, or split it. |
| `empty_file` | A file has no bytes. Replace it. |
| `unsupported_media_type` | The file does not match the input's `accepts` (the types the workflow's input takes). Convert it, for example to PDF, or send it to an input that accepts it. |
| `too_many_files` | The record has more files than one run accepts. Split the record. |
| `unknown_input` | `input_id` is not an input of the workflow. Fix the record's `input_id`. |
| `missing_required_input` | The workflow has a required input the record did not fill. Send files for it (the source can only name one `input_id` per record: use a workflow whose other inputs are optional). |
| `too_many_files_for_input` | The record has more files for the input than the input allows. Split the record. |
| `invalid_input` | The input's settings in the workflow do not accept this send. Check the input in Studio. |
| `no_run` | Not from MAKIA's answer list: MAKIA knows the record's external id, but its case has no run any more (the runs were deleted in Studio). Start a run for the case in Studio, or delete the case there, then `retry` the record. |

**Any other 4xx answer** (except 401 and 403) is treated like a fixable one: the
record is stored as refused with that code, the sink hears `run_refused(record,
code)`, the log has a warning naming the code, and the next pass tries again. The
pass goes on with the next record. Examples: `import_running` (another import
holds the case: wait), `idempotency_key_in_use`, `case_not_found`,
`expected_multipart`. If one stays, read the code in `openapi.json`.

**401, 403, 5xx and network errors** stop the job with exit code 1 and one line
saying what MAKIA answered; nothing is marked, the next pass starts again.
`401 invalid_token` or `403 token_scope` / `service_account_required` mean the wrong
token (see Troubleshooting); a 403 for a project means the run account lacks
`cases` or `run` there.

## TLS and proxy

- By default the integration trusts the **system certificate store** (on Windows,
  its certificate store). A company CA installed there is trusted without
  settings.
- If MAKIA uses a certificate from a CA the system does not know, set
  `MAKIA_CA_BUNDLE` to a PEM file holding that CA (the whole chain, if you have
  it). That file replaces the default store for the integration. Every certificate in it
  is trusted as it is, so an intermediate CA alone is enough. A path that does not
  exist, or a file that is not PEM, stops the command with exit code 2 and the path.
- `doctor` on a certificate failure prints the server's certificate fingerprint
  (SHA-256) and saves the certificate chain it saw as `makia-server-cert.pem`
  next to the state file. Check the fingerprint with whoever runs MAKIA before
  you trust it, then set `MAKIA_CA_BUNDLE` to exactly that file (no root CA needs
  to be added).
- A proxy is read from `HTTPS_PROXY` / `HTTP_PROXY` / `NO_PROXY`. `SSL_CERT_FILE`
  is honoured too.
- Calls that fail on the network, or answer 429 or 5xx, are retried with a
  growing wait (up to 30 s). Other 4xx answers are not retried. If all retries
  fail: `cannot reach MAKIA at <host> (check the network, the proxy, and
  MAKIA_CA_BUNDLE for a private CA)`.

## Running as a service

Start with plain **`run`**:

    makia-integration --env-file /etc/makia-integration.env \
        --log-file /var/log/makia-integration/integration.log run

It does `sync`, `send` and `poll` once per round, every `--interval` seconds
(default 60). `sync` runs every `--sync-every` rounds (default 60, so about
hourly; `--sync-every 1` syncs every round). A failing step is logged in one line
(`sync failed: ...`) and the round goes on with the next step; a failed `sync` waits
for its next turn. After a round with a failure the integration is built again. `Ctrl+C` or
SIGTERM stops it between steps (exit code 0). This is enough: with no webhook, results
arrive within one interval.

### Webhooks (later, to get results sooner)

A webhook makes MAKIA tell the integration when something happened, so a round starts
at once. Rounds never overlap: any number of deliveries during a round cost one
more round. The feed stays the truth, so a missed delivery loses nothing.

1. In Studio, an organization admin makes an **external service** (a connection)
   whose base URL reaches this host and the port, for example
   `http://integration.company.example:8080`. Note its id.
2. Create the webhook with the run token:

       makia-integration --env-file /etc/makia-integration.env webhook create \
           --name integration --connection <connection id> --path /hooks/makia

   It prints the secret **once**. Put it in the env file as
   `MAKIA_WEBHOOK_SECRET=...`. **`--listen` stops with exit code 2 and `set
   MAKIA_WEBHOOK_SECRET to receive webhooks` while it is missing.**
3. Add `--listen 8080` after `run` (in the unit's `ExecStart`) and restart.

The receiver is plain HTTP and answers on any path. It checks the signature and a
timestamp no older than 5 minutes, and answers 401 otherwise. If the connection
must be HTTPS, put the customer's reverse proxy (with TLS) in front of the port.

Other webhook commands (they need `MAKIA_API_URL` and `MAKIA_RUN_TOKEN`):

    makia-integration webhook list
    makia-integration webhook deliveries <id> [--limit N]
    makia-integration webhook disable <id>        # also: enable, delete
    makia-integration webhook rotate-secret <id>  # prints the new secret once

`listen --port PORT` is `run --listen` without the timer: it runs only on
deliveries and syncs only on every Nth one (`--sync-every`). Use it for tests, not
as the service.

### systemd (Linux)

`makia-integration.service` from the starter hardcodes `/opt/makia-integration` (the working
folder, with `integration.py` and the venv in `venv/`) and the user `makia-integration`.
Edit it if yours differ. Then:

    sudo useradd --system --home /opt/makia-integration makia-integration
    # put integration.py in /opt/makia-integration and create the venv there (see "Install")
    sudo cp makia-integration.service /etc/systemd/system/
    sudo cp .env /etc/makia-integration.env
    sudo chown root:makia-integration /etc/makia-integration.env && sudo chmod 640 /etc/makia-integration.env
    sudo mkdir -p /var/log/makia-integration && sudo chown makia-integration /var/log/makia-integration
    sudo chown makia-integration /opt/makia-integration     # the state file is written there
    sudo systemctl daemon-reload && sudo systemctl enable --now makia-integration
    journalctl -u makia-integration -f

The unit sets `MAKIA_INTEGRATION=integration:integration`. `Restart=on-failure` restarts it after
exit code 1 and 2, so read the log if it restarts in a loop. The state file is
`makia-state.db` in `/opt/makia-integration` unless `MAKIA_STATE_FILE` says otherwise;
the service user must be able to write there.

### Windows

No fork or signals are used, so the same command runs. Use a service wrapper such
as NSSM, or Task Scheduler "at start-up", with the full path of
`venv\Scripts\makia-integration.exe` and the same arguments.

## Troubleshooting

Start with `makia-integration --env-file .env doctor`. Lines you may see in the log:

| Log line or message | Meaning and what to do |
|---|---|
| `set MAKIA_API_URL (see the setup guide ...)` | A required setting is missing. Set it in the env file. |
| `... is in use: another makia-integration process is using it` | Another process holds the state file. Stop it (`systemctl stop makia-integration`), then retry. |
| `cannot reach MAKIA at <host> ...` | Network, proxy or certificate. Run `doctor`; see "TLS and proxy". |
| `MAKIA answered 401 ...` / `403 token_scope` | The token is wrong or belongs to the other account. Sync token for `sync`, run token for everything else. |
| `sync <id>: ... conflicts N` / `left alone, resolve in Studio` | A member differs from what a person set in Studio. Resolve it in Studio; the integration does not overwrite it. |
| `sync <id>: refused (<status> <code>)` | MAKIA refused that project (for example `400 invalid_member`, `409 slug_taken`); the other projects were sent. `sync` exits 1 with `sync failed: N of M projects refused: ...`. Read the code in `openapi.json`, fix the project or its members. |
| `sync <id>: refused (400 foreign_service_account): ...; members: ...` | A member of that project is a service account, or the run token belongs to another organization. A Source member must name a person; check `MAKIA_RUN_TOKEN`. |
| `sync: <id> is no longer in the source but not archived there` | A project disappeared from `projects()`. Return it with `archived=True`. |
| `set MAKIA_WEBHOOK_SECRET to receive webhooks` | `--listen` or `listen` needs the webhook's secret (exit 2). Create the webhook, set `MAKIA_WEBHOOK_SECRET`, see "Webhooks". Or drop `--listen`. |
| `member '<x>' of project '<id>' is the run account` | Remove the run account from `members()`; it is added automatically. |
| `send <id>: refused (<code>)` | See "Refusals". |
| `send <id>: MAKIA refused with an unlisted code <code> (N)` | A 4xx not in the lists; it is tried again every pass. See "Refusals". |
| `send <id>: MAKIA knows it, but case <case> has no run any more (deleted) ...` | The sink heard `run_refused(record, "no_run")`. See `no_run` under "Refusals". |
| `sync failed: ...` / `send failed: ...` / `poll failed: ...` | That step of a `run` round failed (the reason follows, in the same words as a one-shot command). The other steps still ran; the integration is built again for the next round. |
| `integration failed: ...` | The factory raised. It is called again next round; a configuration error (exit 2) or a state file in use ends `run`. |
| `round failed` (with a traceback) | An error outside the steps, a bug: report it. |
| `run <run> (record <id>) is gone from MAKIA: closed` | The run was deleted in MAKIA. The sink heard `run_status` with status `gone`; the run is not read again. |
| `results <run>/<view>: 404 view_not_found, skipped` | The view is no longer in the run's workflow. Nothing to do; the feed moves on. |
| `poll: feed stuck at after=N while handling ...` | Your sink (or the integration) raised, often a `RuntimeError` or a database error on the next line. The poll stops, the marker stays, and the next poll repeats that page. Fix the cause. |
| `progress <run>: ...` | MAKIA could not give the run's progress. Harmless; it is read again later. |
| `caught up <run>: succeeded, ended before the feed marker` | Normal after a restart or a first start. |
| `results <run>/<view>: N rows` | A view was fully delivered. |
| `view kept being rebuilt while reading` | The view changed during three reads in a row. The poll stops, the marker stays, and the next poll repeats the page. |
| `MAKIA_CA_BUNDLE <path> cannot be read ...` / `... is not a readable PEM certificate file` | Fix the path, or point it at the PEM file (`-----BEGIN CERTIFICATE-----`). Exit 2. |
| `set MAKIA_STORAGE_CONNECTION_ID or MAKIA_FILES_DIR ...` / `... not both` | Set exactly one of them. Exit 2. |
| `cannot listen on port N` | Another program uses the port, or it is below 1024 without rights. Pick another with `--listen PORT` (`listen`: `--port PORT`). |
| `integration ... must look like package.module:function` | Fix `MAKIA_INTEGRATION` / `--integration`. The current folder is importable. |

Nothing arrives in your tables: `poll` once with `-v`; check `webhook deliveries`;
check the view name in your workflow (the sink receives every view that is built).

A run ended but results are missing: the views are built after the run succeeds.
They arrive with the next poll. Failed runs have no views; `run_status` carries
the error code.
