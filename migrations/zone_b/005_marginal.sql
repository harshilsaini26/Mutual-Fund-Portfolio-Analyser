-- Marginal contribution. MODULE_3.md §4.4 and §11.
--
-- "What does this fund add that I do not already have?" One row per held
-- scheme. Derived like everything in 003, and under the same rule: a rebuild
-- replaces the set for its user-date-basis rather than merging into it.
--
-- Amended from §4.4 as 003 was: DECIMAL_TEXT rather than TEXT so reads return
-- Decimal, and `computed_at` beside the figures.
--
-- A held scheme with no disclosure has a row with every measured column NULL.
-- What it adds is unknown, and a row of zeros would say it adds nothing.
CREATE TABLE IF NOT EXISTS fund_marginal_contribution (
  user_id            TEXT NOT NULL,
  as_of              TEXT NOT NULL,
  weight_basis       TEXT NOT NULL,
  scheme_id          TEXT NOT NULL,
  position_inr       DECIMAL_TEXT NOT NULL,
  new_issuers        INTEGER,                -- issuers only this fund provides
  new_exposure_inr   DECIMAL_TEXT,
  new_exposure_pct   DECIMAL_TEXT,
  hhi_with           DECIMAL_TEXT,
  hhi_without        DECIMAL_TEXT,
  hhi_delta          DECIMAL_TEXT,           -- negative => diversifying
  effective_n_delta  DECIMAL_TEXT,
  style_shift_pp     DECIMAL_TEXT,           -- needs M2 style data; NULL
  fee_cost_inr       DECIMAL_TEXT,           -- needs a TER (V0-18); NULL
  computed_at        TEXT NOT NULL,
  PRIMARY KEY (user_id, as_of, weight_basis, scheme_id)
);
