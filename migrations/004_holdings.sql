-- MODULE_0.md §4.6 — disclosed holdings. PLAN.md §7 V1 steps 3 and 4.
--
-- This is what the entity master built in 003 exists to be pointed at, and
-- what the look-through engine reads. DECIMAL_TEXT throughout: SZ-13, V0-19.

CREATE TABLE IF NOT EXISTS holding (
  scheme_id           TEXT NOT NULL REFERENCES scheme(scheme_id),
  as_of_date          DATE NOT NULL,
  revision            INTEGER NOT NULL DEFAULT 1,
  row_number          INTEGER NOT NULL,
  isin                TEXT,
  -- NOT NULL by construction. An unresolvable row carries __UNRESOLVED__ and
  -- stays visible in every aggregate; PLAN.md §4.10 forbids dropping it.
  issuer_id           TEXT NOT NULL REFERENCES issuer(issuer_id),
  instrument_raw_name TEXT NOT NULL,
  quantity            DECIMAL_TEXT,           -- DECIMAL(24,4)
  -- Rupees ABSOLUTE, already converted from whatever unit the header stated.
  -- §7.2 calls the conversion the 100x error path.
  market_value        DECIMAL_TEXT NOT NULL,  -- DECIMAL(24,4)
  pct_to_nav          DECIMAL_TEXT,           -- DECIMAL(9,6), as reported
  -- Sums to EXACTLY 100 per scheme-date. Every aggregation uses this, never
  -- pct_to_nav — the reported figure sums to 98-102% and the error differs per
  -- scheme, so look-through would not equal portfolio value (§7.3).
  pct_normalised      DECIMAL_TEXT NOT NULL,  -- DECIMAL(9,6)
  instrument_class    TEXT NOT NULL,          -- equity|debt|cash|derivative|mfunit|other
  credit_rating       TEXT,
  reported_sector     TEXT,
  yield_pct           DECIMAL_TEXT,           -- DECIMAL(9,6)
  resolution_method   TEXT NOT NULL,          -- isin|alias|fuzzy|rule|provisional|unresolved
  resolution_conf     DECIMAL_TEXT,           -- DECIMAL(4,3)
  source_file_id      TEXT NOT NULL REFERENCES raw_file(file_id),
  ingested_at         TIMESTAMP NOT NULL,
  is_current          INTEGER NOT NULL DEFAULT 1,
  PRIMARY KEY (scheme_id, as_of_date, revision, row_number)
);
CREATE INDEX IF NOT EXISTS ix_hold_current ON holding(scheme_id, as_of_date)
  WHERE is_current = 1;
CREATE INDEX IF NOT EXISTS ix_hold_issuer  ON holding(issuer_id, as_of_date)
  WHERE is_current = 1;

-- §4.6: weight_residual and unresolved_mv_pct live HERE, not per row — they are
-- properties of the file. M2 and M3 read them to decide whether the
-- look-through for that scheme can be trusted at all.
CREATE TABLE IF NOT EXISTS holding_disclosure (
  scheme_id         TEXT NOT NULL REFERENCES scheme(scheme_id),
  as_of_date        DATE NOT NULL,
  revision          INTEGER NOT NULL,
  source_file_id    TEXT NOT NULL REFERENCES raw_file(file_id),
  row_count         INTEGER NOT NULL,
  pct_sum_raw       DECIMAL_TEXT,             -- pre-normalisation, audit trail
  weight_residual   DECIMAL_TEXT,             -- the slop that was discarded
  unresolved_mv_pct DECIMAL_TEXT NOT NULL,    -- FIRST-CLASS quality metric (V3)
  total_mv          DECIMAL_TEXT NOT NULL,
  aum_reported      DECIMAL_TEXT,
  mv_vs_aum_pct     DECIMAL_TEXT,
  reported_unit     TEXT,                     -- 'lakh' etc, verbatim from the header
  validation_status TEXT NOT NULL,            -- ok|warn|quarantined
  validation_notes  TEXT,                     -- JSON: every check, passes included
  is_current        INTEGER NOT NULL DEFAULT 1,
  ingested_at       TIMESTAMP NOT NULL,
  PRIMARY KEY (scheme_id, as_of_date, revision)
);
