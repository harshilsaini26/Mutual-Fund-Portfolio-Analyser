-- Look-through outputs. MODULE_3.md §4.2 and §4.6.
--
-- Zone B, because these are the USER's exposures: what this person owns, keyed
-- on user_id. `scheme_issuer_weight` is the user-independent half and lives in
-- Zone A (migration 005 there); these are the half that must be encrypted.
--
-- Every table here is DERIVED. `CLAUDE.md` invariant 10 makes them droppable and
-- regenerable from `txn` and `scheme_issuer_weight`, so a rebuild REPLACES rather
-- than appending — the opposite of invariant 2's rule for fact rows, and the
-- distinction is deliberate: nothing here is a fact anyone stated.
--
-- Decimal columns are DECIMAL_TEXT, not §4.2's literal TEXT. Both take TEXT
-- affinity, but only DECIMAL_TEXT fires the converter, so a read returns Decimal
-- rather than str. That has now failed twice for different reasons — SZ-13's
-- affinity rules and V1-16's per-driver registry — so it is not assumed.

CREATE TABLE IF NOT EXISTS lookthrough_exposure (
  user_id          TEXT NOT NULL,
  as_of            TEXT NOT NULL,
  weight_basis     TEXT NOT NULL,          -- disclosed|drift_adjusted
  issuer_id        TEXT NOT NULL,

  exposure_inr     DECIMAL_TEXT NOT NULL,
  exposure_pct     DECIMAL_TEXT NOT NULL,
  via_funds        INTEGER NOT NULL,       -- how many schemes contribute
  fund_inr         DECIMAL_TEXT NOT NULL,
  direct_inr       DECIMAL_TEXT NOT NULL DEFAULT '0',

  instrument_class TEXT,                   -- dominant class for this issuer
  is_synthetic     INTEGER NOT NULL DEFAULT 0,

  -- §5.5: the EARLIEST contributing disclosure date, i.e. worst-case staleness.
  -- The latest would report the portfolio as fresher than it is, which is the
  -- one direction this number must never err in.
  holdings_as_of   TEXT,
  staleness_days   INTEGER,
  coverage_pct     DECIMAL_TEXT NOT NULL,
  confidence       TEXT NOT NULL,          -- §14.1
  computed_at      TEXT NOT NULL,
  PRIMARY KEY (user_id, as_of, weight_basis, issuer_id)
);
CREATE INDEX IF NOT EXISTS ix_lte_size
  ON lookthrough_exposure(user_id, as_of, weight_basis, exposure_inr DESC);

-- "Which of my funds gives me this exposure, and how much?" — the Sankey, every
-- drill-down, and PLAN.md §4.2's audit trail at portfolio level.
CREATE TABLE IF NOT EXISTS lookthrough_contribution (
  user_id        TEXT NOT NULL,
  as_of          TEXT NOT NULL,
  weight_basis   TEXT NOT NULL,
  issuer_id      TEXT NOT NULL,
  scheme_id      TEXT NOT NULL,            -- or '__DIRECT__'
  depth          INTEGER NOT NULL DEFAULT 0,  -- 0 = direct holding, 1 = via FoF
  weight_in_fund DECIMAL_TEXT NOT NULL,
  exposure_inr   DECIMAL_TEXT NOT NULL,
  PRIMARY KEY (user_id, as_of, weight_basis, issuer_id, scheme_id, depth)
);

-- §4.6. Many columns here belong to M1 and M2 — XIRR, TWRR, TER, fee drag,
-- realised P&L — and are written NULL until those modules feed them. The columns
-- exist because the spec defines them; leaving them NULL says "not computed",
-- where a zero would say "computed, and it is nothing".
CREATE TABLE IF NOT EXISTS portfolio_summary (
  user_id             TEXT NOT NULL,
  as_of               TEXT NOT NULL,
  total_value_inr     DECIMAL_TEXT NOT NULL,
  fund_value_inr      DECIMAL_TEXT NOT NULL,
  direct_value_inr    DECIMAL_TEXT NOT NULL,
  invested_net_inr    DECIMAL_TEXT,
  unrealised_pnl_inr  DECIMAL_TEXT,
  realised_pnl_todate DECIMAL_TEXT,

  portfolio_xirr      DECIMAL_TEXT,
  portfolio_twrr_ann  DECIMAL_TEXT,
  blended_ter         DECIMAL_TEXT,
  annual_fee_inr      DECIMAL_TEXT,

  scheme_count        INTEGER,
  folio_count         INTEGER,
  issuer_count        INTEGER,
  direct_issuer_count INTEGER,

  coverage_pct        DECIMAL_TEXT NOT NULL,
  schemes_covered     INTEGER,
  schemes_stale       INTEGER,
  schemes_quarantined INTEGER,
  unresolved_pct      DECIMAL_TEXT NOT NULL,
  worst_staleness_days INTEGER,
  confidence          TEXT NOT NULL,
  caveats             TEXT,                -- JSON array, consumed by M6
  computed_at         TEXT NOT NULL,
  PRIMARY KEY (user_id, as_of)
);
