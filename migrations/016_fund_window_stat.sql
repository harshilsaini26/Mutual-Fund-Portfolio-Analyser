-- `fund_window_stat`: each live fund's figures per window, computed once per
-- build. DECISIONS V1-77; MODULE_2 §4.3 (`scheme_return_window`), trimmed.
--
-- A fund's peer rank and its category's risk-against-return picture need every
-- peer's figures. Computing them on each fund page would read every peer's
-- whole history for every page (O(n^2) across the site); computing them once
-- per build and reading ~1,800 rows is the difference.
--
-- Derived (invariant 10): `src/m2_fund/stats.py` replaces the whole set on each
-- rebuild, so there is no revision column. Fund-only figures: no benchmark
-- statistic is stored here, because the public build carries no index data and
-- nothing index-derived may be readable through a table its pages use.
--
-- `spans` is 1 when the prices cover the whole window (`m2_fund.windows.spans`):
-- a "5y" row over four years of history is kept for the record but never ranked
-- or labelled as five years.

CREATE TABLE IF NOT EXISTS fund_window_stat (
  scheme_id      TEXT NOT NULL REFERENCES scheme(scheme_id),
  window_key     TEXT NOT NULL,          -- 1y | 3y | 5y | since_first_nav
  as_of          DATE NOT NULL,          -- the window's last price
  obs_days       INTEGER NOT NULL,
  spans          INTEGER NOT NULL,       -- 1: the prices cover the whole window
  return_ann     DECIMAL_TEXT,           -- fraction a year
  return_cum     DECIMAL_TEXT,           -- fraction over the window
  volatility_ann DECIMAL_TEXT,           -- fraction a year
  max_dd         DECIMAL_TEXT,           -- worst fall, a negative fraction
  sharpe         DECIMAL_TEXT,
  computed_at    TIMESTAMP NOT NULL,
  PRIMARY KEY (scheme_id, window_key)
);
