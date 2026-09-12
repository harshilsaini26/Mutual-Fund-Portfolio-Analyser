-- Portfolio-level metrics. MODULE_3.md §4.3.
--
-- Zone B, for the same reason as 002: these are keyed on user_id and describe
-- what THIS person owns. `scheme_issuer_weight` is the user-independent half
-- and lives in Zone A.
--
-- Every table here is DERIVED. `CLAUDE.md` invariant 10 makes them droppable
-- and regenerable from `lookthrough_exposure` and `lookthrough_contribution`,
-- so a rebuild REPLACES the set rather than merging into it — V1-18 and V1-20
-- both record what `INSERT OR REPLACE` alone leaves behind.
--
-- Decimal columns are DECIMAL_TEXT, not §4.3's literal TEXT. Both take TEXT
-- affinity, but only DECIMAL_TEXT fires the converter, so a read returns
-- Decimal rather than str. Same amendment 002 carries, for the same two
-- reasons it was needed twice: SZ-13's affinity rules and V1-16's per-driver
-- adapter registry.

-- §8.1. "How concentrated am I really?" — one row per scope, because the
-- answer differs: a hybrid fund's equity sleeve can be very concentrated
-- inside a portfolio that looks diversified overall.
--
-- Synthetics are excluded from the pool before any of these are computed
-- (§8.2, the denominator trap). Counting __CASH__ as an issuer flatters every
-- figure here.
CREATE TABLE IF NOT EXISTS portfolio_concentration (
  user_id            TEXT NOT NULL,
  as_of              TEXT NOT NULL,
  weight_basis       TEXT NOT NULL,          -- disclosed|drift_adjusted
  scope              TEXT NOT NULL,          -- all|equity|debt
  issuer_count       INTEGER NOT NULL,

  hhi                DECIMAL_TEXT,
  effective_n        DECIMAL_TEXT,
  top1_pct           DECIMAL_TEXT,
  top5_pct           DECIMAL_TEXT,
  top10_pct          DECIMAL_TEXT,
  top20_pct          DECIMAL_TEXT,

  -- NULL when the pool contains a negative net exposure. Gini summarises a
  -- Lorenz curve and that construction assumes a non-negative pool; with a
  -- short leg the formula still returns an ordinary-looking number that
  -- describes nothing. V1-07 records a real disclosed short, so this is not
  -- hypothetical. An em dash is the honest rendering (MODULE_6 §9.3).
  gini               DECIMAL_TEXT,

  largest_issuer_id  TEXT,
  largest_issuer_pct DECIMAL_TEXT,
  computed_at        TEXT NOT NULL,
  PRIMARY KEY (user_id, as_of, weight_basis, scope)
);

-- §9.1. "Am I paying twice for the same thing?" — one row per unordered pair.
CREATE TABLE IF NOT EXISTS fund_overlap (
  user_id            TEXT NOT NULL,
  as_of              TEXT NOT NULL,
  weight_basis       TEXT NOT NULL,
  scheme_a           TEXT NOT NULL,          -- lexicographically smaller
  scheme_b           TEXT NOT NULL,

  overlap_pct        DECIMAL_TEXT NOT NULL,  -- Σ min(w_a, w_b), non-synthetic
  overlap_equity_pct DECIMAL_TEXT,           -- renormalised within equity
  common_issuers     INTEGER NOT NULL,
  union_issuers      INTEGER NOT NULL,
  jaccard            DECIMAL_TEXT,

  -- Rupees held by both funds at once: Σ min(w_a·V_a, w_b·V_b) per issuer.
  -- NULL when either position had no value on the date — which is not zero,
  -- because zero reads as "nothing is duplicated".
  overlap_value_inr  DECIMAL_TEXT,

  -- §9.3's alignment audit. Overlap is exactly the metric where a month's
  -- trading moves the answer, so a mismatched-date comparison carries its own
  -- flag rather than inheriting the portfolio's staleness caveat.
  as_of_a            TEXT NOT NULL,
  as_of_b            TEXT NOT NULL,
  as_of_gap_days     INTEGER NOT NULL,
  aligned            INTEGER NOT NULL,       -- 0 ⇒ dates differ, soft comparison
  PRIMARY KEY (user_id, as_of, weight_basis, scheme_a, scheme_b)
);

-- §9.4. "How much of my money is doubled up?" — the question pairwise overlap
-- does not answer. `duplicated_inr` is Σ(issuer exposure − its largest single
-- fund): what the second and later providers add, not the issuer's whole
-- exposure. Descriptive, not a recommendation to consolidate (PLAN.md §3.3).
CREATE TABLE IF NOT EXISTS portfolio_duplication (
  user_id              TEXT NOT NULL,
  as_of                TEXT NOT NULL,
  weight_basis         TEXT NOT NULL,
  duplicated_pct       DECIMAL_TEXT,
  duplicated_inr       DECIMAL_TEXT,
  issuers_multi_fund   INTEGER,
  max_funds_per_issuer INTEGER,
  computed_at          TEXT NOT NULL,
  PRIMARY KEY (user_id, as_of, weight_basis)
);
