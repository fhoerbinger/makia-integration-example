-- The customer's side. In a real integration these are views over the source system.
IF OBJECT_ID('src_projects') IS NULL CREATE TABLE src_projects (
  external_id NVARCHAR(200) PRIMARY KEY,
  name        NVARCHAR(200) NOT NULL,
  archived    BIT NOT NULL DEFAULT 0
);
IF OBJECT_ID('src_members') IS NULL CREATE TABLE src_members (
  project_external_id NVARCHAR(200) NOT NULL,
  subject             NVARCHAR(512) NOT NULL,
  permissions         NVARCHAR(100) NOT NULL,
  PRIMARY KEY (project_external_id, subject)
);
IF OBJECT_ID('src_records') IS NULL CREATE TABLE src_records (
  external_id         NVARCHAR(255) PRIMARY KEY,
  project_external_id NVARCHAR(200) NOT NULL,
  workflow_slug       NVARCHAR(200) NOT NULL,
  input_id            NVARCHAR(200) NOT NULL
);
IF OBJECT_ID('src_record_files') IS NULL CREATE TABLE src_record_files (
  record_external_id NVARCHAR(255) NOT NULL,
  path               NVARCHAR(400) NOT NULL,
  PRIMARY KEY (record_external_id, path)
);

-- The integration's own tables.
IF OBJECT_ID('integration_state') IS NULL CREATE TABLE integration_state (
  name  NVARCHAR(100) PRIMARY KEY,
  value NVARCHAR(400) NOT NULL
);
IF OBJECT_ID('integration_runs') IS NULL CREATE TABLE integration_runs (
  record_external_id NVARCHAR(255) PRIMARY KEY,
  case_id            UNIQUEIDENTIFIER NULL,
  run_id             UNIQUEIDENTIFIER NULL,
  review_url         NVARCHAR(1000) NULL,
  status             NVARCHAR(20) NOT NULL,
  error_code         NVARCHAR(100) NULL,
  progress_json      NVARCHAR(MAX) NULL,   -- last GET /runs/{id}/progress answer
  progress_at        NVARCHAR(40) NULL     -- when it was read
);
IF OBJECT_ID('integration_results') IS NULL CREATE TABLE integration_results (
  run_id         UNIQUEIDENTIFIER NOT NULL,
  view_key       NVARCHAR(100) NOT NULL,
  unit_key       NVARCHAR(300) NOT NULL,   -- key under 900 bytes
  generation     NVARCHAR(100) NOT NULL,
  row_key        NVARCHAR(400) NULL,
  data_json      NVARCHAR(MAX) NOT NULL,
  findings_open  INT NOT NULL,
  review_state   NVARCHAR(20) NOT NULL,
  review_option  NVARCHAR(200) NULL,
  review_key     NVARCHAR(400) NULL,
  url            NVARCHAR(1000) NULL,
  PRIMARY KEY (run_id, view_key, unit_key)
);
