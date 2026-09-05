-- MODULE_0.md §4.4 (scheme layer) and §4.5 (NAV and IDCW), narrowed to what
-- PLAN.md §7 V0 steps 2-3 need. Tables the spec defines but V0 does not read
-- -- scheme_ter, scheme_aum, manager, scheme_manager_tenure -- are not created
-- here; V0-18 defers the TER DDL to V2 and the rest belong to M2.
--
-- DECIMAL_TEXT, not DECIMAL: see 001 and DECISIONS SZ-13 / V0-19.

CREATE TABLE IF NOT EXISTS amc (
  amc_id        TEXT PRIMARY KEY,        -- normalised AMC name
  amc_name      TEXT NOT NULL,           -- verbatim, as the source printed it
  amfi_amc_code TEXT,
  website       TEXT,
  parser_id     TEXT
);

-- One scheme_id per plan+option, no exceptions (MODULE_0.md §4.4).
-- Direct and Regular have different NAV series and TERs ~1%/yr apart; Growth
-- and IDCW have different NAV series. Collapsing any of them is the silent
-- ~1%/year error that unit reconciliation cannot catch -- DECISIONS V0-05 is
-- that error, observed.
--
-- Schemes are NEVER deleted. Wound-up and merged schemes get `status` set;
-- deleting them creates survivorship bias in every historical comparison.
CREATE TABLE IF NOT EXISTS scheme (
  scheme_id        TEXT PRIMARY KEY,     -- the ISIN where available, else 'AMFI:{code}'
  amfi_code        TEXT,
  isin             TEXT,
  amc_id           TEXT REFERENCES amc(amc_id),
  scheme_name      TEXT NOT NULL,
  plan             TEXT NOT NULL,        -- direct|regular|unknown
  option           TEXT NOT NULL,        -- growth|idcw_payout|idcw_reinvest|unknown
  option_raw       TEXT,                 -- verbatim: 'QUARTERLY IDCW Payout' etc.
  sebi_category    TEXT,                 -- from the section header line
  benchmark_id     TEXT,
  inception_date   DATE,
  status           TEXT NOT NULL DEFAULT 'active',   -- active|merged|wound_up
  merged_into      TEXT REFERENCES scheme(scheme_id),
  merger_date      DATE,
  merger_ratio_num INTEGER,
  merger_ratio_den INTEGER,
  first_seen       DATE,
  last_seen        DATE,
  source_file_id   TEXT REFERENCES raw_file(file_id)
);
CREATE UNIQUE INDEX IF NOT EXISTS ux_scheme_isin ON scheme(isin) WHERE isin IS NOT NULL;
CREATE INDEX IF NOT EXISTS ix_scheme_amfi   ON scheme(amfi_code);
CREATE INDEX IF NOT EXISTS ix_scheme_status ON scheme(status);
-- Resolution step 3 matches on this triple and requires it to be UNIQUE before
-- it will answer. It is not unique in the source -- see DECISIONS V0-20.
CREATE INDEX IF NOT EXISTS ix_scheme_name_plan_option ON scheme(scheme_name, plan, option);

CREATE TABLE IF NOT EXISTS nav_daily (
  scheme_id       TEXT NOT NULL REFERENCES scheme(scheme_id),
  nav_date        DATE NOT NULL,
  nav             DECIMAL_TEXT NOT NULL,  -- DECIMAL(18,6) as published
  nav_adj         DECIMAL_TEXT,           -- DECIMAL(18,6) IDCW-reinvested total return
  is_interpolated INTEGER NOT NULL DEFAULT 0,
  source_file_id  TEXT REFERENCES raw_file(file_id),
  PRIMARY KEY (scheme_id, nav_date)
);
CREATE INDEX IF NOT EXISTS ix_nav_date ON nav_daily(nav_date);

CREATE TABLE IF NOT EXISTS scheme_idcw (
  scheme_id       TEXT NOT NULL REFERENCES scheme(scheme_id),
  record_date     DATE NOT NULL,
  amount_per_unit DECIMAL_TEXT NOT NULL,  -- DECIMAL(18,6)
  source_file_id  TEXT REFERENCES raw_file(file_id),
  PRIMARY KEY (scheme_id, record_date)
);
