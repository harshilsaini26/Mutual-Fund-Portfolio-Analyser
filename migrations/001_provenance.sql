-- MODULE_0.md §4.2 — provenance. PLAN.md §7 V0 step 1.
--
-- Zone A is SQLite, not the DuckDB of MODULE_0.md §4.1 (DECISIONS V0-19), so
-- every DECIMAL(p,s) in the spec becomes DECIMAL_TEXT: SQLite gives DECIMAL
-- NUMERIC affinity and would silently rewrite a decimal string as a REAL
-- (SZ-13). Intended precision is kept in a comment beside each column.
--
-- There are no decimal columns in this migration; the convention is stated here
-- because it applies from 002 onward.

CREATE TABLE IF NOT EXISTS raw_file (
  file_id        TEXT PRIMARY KEY,       -- sha256 of the bytes, lowercase hex
  source_id      TEXT NOT NULL,          -- 'S1' | 'S5:hdfc' | ...
  url            TEXT,
  as_of_date     DATE,                   -- inferred at fetch, corrected at parse
  fetched_at     TIMESTAMP NOT NULL,     -- UTC
  content_type   TEXT,
  byte_size      INTEGER NOT NULL,
  http_etag      TEXT,
  http_last_mod  TEXT,
  storage_path   TEXT NOT NULL,
  parse_status   TEXT NOT NULL DEFAULT 'pending',  -- pending|ok|failed|quarantined|superseded
  parser_id      TEXT,
  parser_version TEXT,                   -- load-bearing: replay everything a fixed parser touched
  parse_error    TEXT,
  parsed_at      TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_raw_source_date ON raw_file(source_id, as_of_date);
CREATE INDEX IF NOT EXISTS ix_raw_status ON raw_file(parse_status) WHERE parse_status <> 'ok';

CREATE TABLE IF NOT EXISTS job_run (
  run_id        TEXT PRIMARY KEY,
  job_name      TEXT NOT NULL,
  started_at    TIMESTAMP NOT NULL,
  finished_at   TIMESTAMP,
  status        TEXT,                    -- ok|partial|failed|skipped
  files_fetched INTEGER DEFAULT 0,
  files_parsed  INTEGER DEFAULT 0,
  rows_written  INTEGER DEFAULT 0,
  warnings      INTEGER DEFAULT 0,
  error_text    TEXT,
  git_sha       TEXT,
  params_json   TEXT                     -- e.g. {"as_of":"2026-07-31"}, for replay
);
CREATE INDEX IF NOT EXISTS ix_job_name_time ON job_run(job_name, started_at DESC);
