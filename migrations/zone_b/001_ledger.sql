-- Zone B: the personal ledger. MODULE_1.md §4.
--
-- Separate from `/migrations` because this is a DIFFERENT DATABASE, not a later
-- version of the warehouse. Zone A holds public market data and may be rebuilt
-- from the archive at any time; Zone B holds transactions, units, folios and a
-- PAN, is encrypted at rest, and `PLAN.md` §6.3 says it never leaves the device
-- unencrypted. Applying one schema to the other's file would be a category
-- error the numbering alone would not prevent.
--
-- Every Decimal column is DECIMAL_TEXT, not §4's literal TEXT. Both take TEXT
-- affinity so both are safe from SZ-13 — SQLite gives a `DECIMAL` column
-- NUMERIC affinity and silently rewrites a decimal string as a REAL — but only
-- DECIMAL_TEXT fires the registered converter, so a read returns `Decimal`
-- rather than `str`. §4.1 registers the adapter and no converter, which leaves
-- every call site to remember to re-wrap: exactly the boundary where a stray
-- float() gets introduced.

-- --- §4.2 identity and configuration ---------------------------------------

CREATE TABLE IF NOT EXISTS app_user (
  user_id            TEXT PRIMARY KEY,
  display_name       TEXT,
  marginal_tax_rate  DECIMAL_TEXT,
  surcharge_rate     DECIMAL_TEXT,
  cess_rate          DECIMAL_TEXT DEFAULT '4.0',
  residency_status   TEXT NOT NULL DEFAULT 'resident',  -- resident|nri
  base_currency      TEXT NOT NULL DEFAULT 'INR',
  fy_start_month     INTEGER NOT NULL DEFAULT 4,        -- April
  created_at         TEXT NOT NULL
);

-- "When reconciliation breaks three months from now, the first question is
-- 'which statement introduced this?' Without this table you're guessing."
CREATE TABLE IF NOT EXISTS cas_import (
  import_id       TEXT PRIMARY KEY,
  user_id         TEXT NOT NULL REFERENCES app_user,
  source_file_id  TEXT NOT NULL,       -- sha256; mirrors Zone A's raw_file
  statement_from  TEXT,                -- DATE
  statement_to    TEXT,
  cas_type        TEXT,                -- cams|kfintech|mfcentral|nsdl|cdsl
  rows_parsed     INTEGER NOT NULL DEFAULT 0,
  rows_inserted   INTEGER NOT NULL DEFAULT 0,
  rows_duplicate  INTEGER NOT NULL DEFAULT 0,
  rows_unmatched  INTEGER NOT NULL DEFAULT 0,
  folios_seen     INTEGER,
  schemes_seen    INTEGER,
  parser_version  TEXT NOT NULL,
  imported_at     TEXT NOT NULL,
  status          TEXT NOT NULL        -- ok|partial|failed
);

-- --- §4.3 immutable facts --------------------------------------------------

CREATE TABLE IF NOT EXISTS txn (
  txn_id            TEXT PRIMARY KEY,  -- deterministic hash, §5.6
  user_id           TEXT NOT NULL REFERENCES app_user,
  folio             TEXT NOT NULL,
  scheme_id         TEXT,              -- NULLABLE: the quarantine path
  scheme_raw_name   TEXT NOT NULL,     -- verbatim, enables re-resolution
  scheme_raw_isin   TEXT,
  scheme_raw_amfi   TEXT,
  txn_date          TEXT NOT NULL,     -- DATE
  txn_seq           INTEGER NOT NULL DEFAULT 0,
  txn_type          TEXT NOT NULL,
  txn_desc_raw      TEXT,              -- original free text, for audit
  units             DECIMAL_TEXT,      -- signed
  nav               DECIMAL_TEXT,
  amount            DECIMAL_TEXT,      -- signed, gross
  stamp_duty        DECIMAL_TEXT NOT NULL DEFAULT '0',
  stt               DECIMAL_TEXT NOT NULL DEFAULT '0',
  exit_load         DECIMAL_TEXT NOT NULL DEFAULT '0',
  tds               DECIMAL_TEXT NOT NULL DEFAULT '0',
  units_balance_rep DECIMAL_TEXT,      -- as printed on the CAS
  switch_group_id   TEXT,
  reverses_txn_id   TEXT REFERENCES txn,
  is_reversed       INTEGER NOT NULL DEFAULT 0,
  flags             TEXT,              -- JSON array
  resolution_conf   TEXT,              -- high|medium|low|unresolved
  import_id         TEXT REFERENCES cas_import,
  source_file_id    TEXT NOT NULL,
  row_number        INTEGER,
  ingested_at       TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_txn_natural ON txn(
  user_id, folio,
  COALESCE(scheme_id, scheme_raw_isin, scheme_raw_name),
  txn_date, txn_seq, txn_type, units, amount
);
CREATE INDEX IF NOT EXISTS ix_txn_scheme     ON txn(user_id, scheme_id, txn_date);
CREATE INDEX IF NOT EXISTS ix_txn_unresolved ON txn(user_id) WHERE scheme_id IS NULL;
CREATE INDEX IF NOT EXISTS ix_txn_import     ON txn(import_id);

-- --- §4.4 derived: lots ----------------------------------------------------
--
-- `acquisition_date` is the TAX CLOCK and `book_date` is the credit date. They
-- diverge on mergers and segregations, and collapsing them is the single most
-- common source of wrong tax output.

CREATE TABLE IF NOT EXISTS lot (
  lot_id            TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL,
  folio             TEXT NOT NULL,
  scheme_id         TEXT NOT NULL,
  open_txn_id       TEXT NOT NULL REFERENCES txn,
  acquisition_date  TEXT NOT NULL,
  book_date         TEXT NOT NULL,
  units_original    DECIMAL_TEXT NOT NULL,
  units_remaining   DECIMAL_TEXT NOT NULL,
  cost_per_unit     DECIMAL_TEXT NOT NULL,
  cost_total        DECIMAL_TEXT NOT NULL,   -- incl. stamp duty
  grandfathered_nav DECIMAL_TEXT,            -- 31-Jan-2018 FMV, if applicable
  carried_from_lot  TEXT REFERENCES lot,
  origin            TEXT NOT NULL,
  exit_load_free_on TEXT,                    -- precomputed DATE
  is_closed         INTEGER NOT NULL DEFAULT 0,
  rebuilt_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_lot_fifo ON lot(
  user_id, folio, scheme_id, acquisition_date, book_date) WHERE is_closed = 0;
CREATE INDEX IF NOT EXISTS ix_lot_scheme ON lot(user_id, scheme_id);

-- `tax_class_at_sale` and `tax_rule_id` are PERSISTED, not recomputed. Once a
-- gain is realised its tax character is a historical fact — the difference
-- between a tax report you can defend and one that silently changes when the
-- rules table is updated.
CREATE TABLE IF NOT EXISTS lot_consumption (
  consumption_id    TEXT PRIMARY KEY,
  user_id           TEXT NOT NULL,
  lot_id            TEXT NOT NULL REFERENCES lot,
  close_txn_id      TEXT NOT NULL REFERENCES txn,
  units_consumed    DECIMAL_TEXT NOT NULL,
  sale_nav          DECIMAL_TEXT,
  proceeds_gross    DECIMAL_TEXT,
  proceeds_net      DECIMAL_TEXT,            -- net of exit load + STT
  cost_allocated    DECIMAL_TEXT,
  cost_basis_method TEXT NOT NULL,           -- actual|grandfathered
  holding_days      INTEGER NOT NULL,
  gain_type         TEXT NOT NULL,           -- STCG|LTCG
  gain_amount       DECIMAL_TEXT,
  tax_class_at_sale TEXT NOT NULL,
  tax_rule_id       TEXT NOT NULL,
  tax_estimated     DECIMAL_TEXT,
  fy                TEXT NOT NULL,           -- '2026-27'
  sequence_in_txn   INTEGER NOT NULL,
  rebuilt_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_consumption_fy  ON lot_consumption(user_id, fy);
CREATE INDEX IF NOT EXISTS ix_consumption_txn ON lot_consumption(close_txn_id);

-- --- §4.5 derived: positions and reconciliation ----------------------------

CREATE TABLE IF NOT EXISTS position (
  user_id               TEXT NOT NULL,
  folio                 TEXT NOT NULL,
  scheme_id             TEXT NOT NULL,
  as_of                 TEXT NOT NULL,
  units                 DECIMAL_TEXT NOT NULL,
  nav                   DECIMAL_TEXT,
  nav_date              TEXT,
  market_value          DECIMAL_TEXT,
  invested_gross        DECIMAL_TEXT,        -- total ever contributed
  invested_net          DECIMAL_TEXT,        -- net of redemption proceeds
  avg_cost_nav          DECIMAL_TEXT,
  unrealised_pnl        DECIMAL_TEXT,
  realised_pnl_todate   DECIMAL_TEXT,
  idcw_received_todate  DECIMAL_TEXT,
  first_purchase        TEXT,
  last_purchase         TEXT,
  weight_in_portfolio   DECIMAL_TEXT,
  is_active_sip         INTEGER,
  reconciled            INTEGER NOT NULL,
  reconcile_delta_units DECIMAL_TEXT,
  confidence            TEXT NOT NULL,       -- high|medium|low
  rebuilt_at            TEXT NOT NULL,
  PRIMARY KEY (user_id, folio, scheme_id, as_of)
);

-- Historised: a folio that passed last month and fails now points directly at
-- the import that broke it.
CREATE TABLE IF NOT EXISTS reconciliation (
  recon_id         TEXT PRIMARY KEY,
  user_id          TEXT NOT NULL,
  folio            TEXT NOT NULL,
  scheme_id        TEXT NOT NULL,
  as_of            TEXT NOT NULL,
  units_computed   DECIMAL_TEXT,
  units_reported   DECIMAL_TEXT,
  delta_units      DECIMAL_TEXT,
  value_computed   DECIMAL_TEXT,
  value_reported   DECIMAL_TEXT,
  delta_value_pct  DECIMAL_TEXT,
  status           TEXT NOT NULL,            -- ok|warn|fail
  diagnosis        TEXT,                     -- JSON: ranked hypotheses
  checked_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_recon_status ON reconciliation(user_id, as_of, status);
