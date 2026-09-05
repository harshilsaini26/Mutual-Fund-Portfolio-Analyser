-- MODULE_0.md §4.3 — the entity layer. PLAN.md §7 V1 steps 1, 2 and 6.
--
-- §8.1: THE EXPOSURE UNIT IS THE ISSUER, NOT THE INSTRUMENT. One fund holding
-- Reliance equity and another holding a Reliance NCD is the same corporate
-- exposure, and only `issuer_id` makes them aggregate. Every downstream join
-- in M3, M4 and M5 is on issuer_id; `isin` identifies the instrument.
--
-- DECIMAL_TEXT, not DECIMAL: SZ-13 and V0-19.

CREATE TABLE IF NOT EXISTS issuer (
  issuer_id      TEXT PRIMARY KEY,
  canonical_name TEXT NOT NULL,
  country        TEXT NOT NULL DEFAULT 'IN',
  is_listed      INTEGER,
  is_synthetic   INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'active',  -- active|merged|delisted|wound_up
  created_at     TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  source_file_id TEXT REFERENCES raw_file(file_id)
);

-- MANDATORY SEED, in the same migration that creates the table (§4.3).
--
-- NINE rows, not the spec's eight. `__NO_DISCLOSURE__` is required by
-- CLAUDE.md invariant 4 ("Missing disclosures -> __NO_DISCLOSURE__") and is
-- declared in src/common/types.py, but §4.3's seed omits it. A scheme that
-- published no portfolio at all is a different fact from a holding that could
-- not be resolved, and collapsing the two would report a fund as 100%
-- unresolved when it simply had not disclosed. DECISIONS V1-01.
INSERT OR IGNORE INTO issuer (issuer_id, canonical_name, is_listed, is_synthetic) VALUES
 ('__UNRESOLVED__',    'Unresolved Holdings',        0, 1),
 ('__NO_DISCLOSURE__', 'No Disclosure Published',    0, 1),
 ('__CASH__',          'Cash & Bank Balance',        0, 1),
 ('__TREPS__',         'TREPS / Repo / Reverse Repo',0, 1),
 ('__DERIV__',         'Derivative Positions',       0, 1),
 ('__RECV__',          'Net Receivables / Payables', 0, 1),
 ('__MFUNIT__',        'Units of Other Schemes',     0, 1),
 ('__GSEC__',          'Government Securities',      0, 1),
 ('__MARGIN__',        'Margin / Deposits',          0, 1);

CREATE TABLE IF NOT EXISTS instrument (
  isin            TEXT PRIMARY KEY,
  issuer_id       TEXT NOT NULL REFERENCES issuer(issuer_id),
  instrument_type TEXT NOT NULL,   -- equity|pref|ncd|cp|cd|gsec|sdl|reit|invit|etf|mfunit|warrant
  ticker_nse      TEXT,
  ticker_bse      TEXT,
  bse_scrip_code  TEXT,
  face_value      DECIMAL_TEXT,    -- DECIMAL(14,4)
  coupon_rate     DECIMAL_TEXT,    -- DECIMAL(8,4)
  maturity_date   DATE,
  listing_date    DATE,
  delisting_date  DATE,
  source_file_id  TEXT REFERENCES raw_file(file_id)
);
CREATE INDEX IF NOT EXISTS ix_instr_issuer ON instrument(issuer_id);
CREATE INDEX IF NOT EXISTS ix_instr_nse    ON instrument(ticker_nse);
CREATE INDEX IF NOT EXISTS ix_instr_bse    ON instrument(bse_scrip_code);

-- Every accepted match writes a row here, so the review queue shrinks
-- monotonically (§8.5). `alias_norm` is the output of normalise_name(), which
-- is deterministic and VERSIONED: if that function changes, this column must
-- be rebuilt, and that is a migration rather than a code change (§7.4).
CREATE TABLE IF NOT EXISTS name_alias (
  alias_norm   TEXT PRIMARY KEY,
  alias_raw    TEXT NOT NULL,
  issuer_id    TEXT NOT NULL REFERENCES issuer(issuer_id),
  isin         TEXT,
  match_method TEXT NOT NULL,      -- isin|manual|fuzzy_accepted|rule
  confidence   DECIMAL_TEXT,       -- DECIMAL(4,3)
  created_at   TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
  created_by   TEXT                -- 'auto' | 'human'
);

-- §8.5: prioritised by MATERIALITY, never by first-seen. The top 20 rows
-- usually account for most unresolved value, so ordering by total_mv_inr is
-- what makes a few hundred decisions in month one tractable.
CREATE TABLE IF NOT EXISTS resolution_queue (
  queue_id          TEXT PRIMARY KEY,   -- hash(raw_name_norm)
  raw_name          TEXT NOT NULL,
  raw_name_norm     TEXT NOT NULL,
  raw_isin          TEXT,
  first_seen        DATE NOT NULL,
  last_seen         DATE NOT NULL,
  occurrence_count  INTEGER NOT NULL DEFAULT 1,
  total_mv_inr      DECIMAL_TEXT,       -- DECIMAL(24,4)
  schemes_affected  INTEGER,
  best_guess_issuer TEXT,
  best_score        DECIMAL_TEXT,       -- DECIMAL(4,3)
  candidates_json   TEXT,
  status            TEXT NOT NULL DEFAULT 'pending',  -- pending|resolved|rejected|synthetic
  resolved_to       TEXT,
  resolved_at       TIMESTAMP
);
CREATE INDEX IF NOT EXISTS ix_queue_pending
  ON resolution_queue(status, total_mv_inr DESC);

-- Point-in-time, per CLAUDE.md invariant 6. `taxonomy` distinguishes the
-- basis: amfi_mcap carries the statutory large/mid/small rank as of a
-- half-year end, canonical_sector the NSE industry. A classification read
-- without its valid_from is the look-ahead bias PLAN.md §9.3 warns about.
CREATE TABLE IF NOT EXISTS issuer_classification (
  issuer_id      TEXT NOT NULL REFERENCES issuer(issuer_id),
  taxonomy       TEXT NOT NULL,   -- canonical_sector|canonical_industry|amfi_mcap|nic
  value          TEXT NOT NULL,
  valid_from     DATE NOT NULL,
  valid_to       DATE,
  source         TEXT,
  source_file_id TEXT REFERENCES raw_file(file_id),
  PRIMARY KEY (issuer_id, taxonomy, valid_from)
);
CREATE INDEX IF NOT EXISTS ix_class_lookup
  ON issuer_classification(taxonomy, issuer_id, valid_from);
