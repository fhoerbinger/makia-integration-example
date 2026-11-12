-- The customer's side. In a real bridge these are views over the source system.
CREATE TABLE IF NOT EXISTS src_projects (
  external_id TEXT PRIMARY KEY,
  name        TEXT NOT NULL,
  archived    INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS src_members (
  project_external_id TEXT NOT NULL,
  subject             TEXT NOT NULL,   -- the person's SSO identity
  permissions         TEXT NOT NULL,   -- comma-separated: review,cases,workflows,run
  PRIMARY KEY (project_external_id, subject)
);
CREATE TABLE IF NOT EXISTS src_records (
  external_id         TEXT PRIMARY KEY,
  project_external_id TEXT NOT NULL,
  workflow_slug       TEXT NOT NULL,
  input_id            TEXT NOT NULL   -- the workflow input the files go to
);
CREATE TABLE IF NOT EXISTS src_record_files (
  record_external_id TEXT NOT NULL,
  path               TEXT NOT NULL,   -- relative to the storage connection's root
  PRIMARY KEY (record_external_id, path)
);

-- The bridge's own tables.
CREATE TABLE IF NOT EXISTS bridge_state (
  name  TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS bridge_runs (
  record_external_id TEXT PRIMARY KEY,
  case_id            TEXT,
  run_id             TEXT,
  review_url         TEXT,
  status             TEXT NOT NULL,   -- sent | running | succeeded | failed | cancelled | refused
  error_code         TEXT,
  progress_json      TEXT,            -- last GET /runs/{id}/progress answer
  progress_at        TEXT             -- when it was read
);
CREATE TABLE IF NOT EXISTS bridge_results (
  run_id         TEXT NOT NULL,
  view_key       TEXT NOT NULL,
  unit_key       TEXT NOT NULL,
  generation     TEXT NOT NULL,   -- the view build the row came from
  row_key        TEXT,
  data_json      TEXT NOT NULL,
  findings_open  INTEGER NOT NULL,
  review_state   TEXT NOT NULL,
  review_option  TEXT,
  review_key     TEXT,
  url            TEXT,
  PRIMARY KEY (run_id, view_key, unit_key)
);
