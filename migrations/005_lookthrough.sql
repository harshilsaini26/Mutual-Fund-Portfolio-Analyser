-- `scheme_issuer_weight`. MODULE_0.md §9.3, PLAN.md §7 V1 item 7.
--
-- Zone A, not Zone B: this is user-independent. It collapses a scheme's
-- holdings from several instruments of one issuer to one row per issuer, which
-- is what MODULE_3.md §2.3 requires — "the exposure unit is the issuer, not the
-- ISIN". Two funds holding different series of one issuer's debt are the same
-- corporate exposure and must aggregate.
--
-- `weight_drift_adj` is NULL for now and the column exists anyway. §3.2 wants
-- both bases stored so M6 can show how much staleness matters, but the
-- drift-adjusted one needs `security_price`, which is not built. A NULL that
-- means "not computed" is honest; a copy of the disclosed weight would be a
-- second basis that silently is not one.

CREATE TABLE IF NOT EXISTS scheme_issuer_weight (
  scheme_id        TEXT NOT NULL REFERENCES scheme(scheme_id),
  as_of_date       DATE NOT NULL,
  issuer_id        TEXT NOT NULL REFERENCES issuer(issuer_id),
  weight_disclosed DECIMAL_TEXT NOT NULL,   -- percent, sums to 100 per scheme-date
  weight_drift_adj DECIMAL_TEXT,            -- NULL until security_price exists
  instrument_class TEXT NOT NULL,
  quantity         DECIMAL_TEXT,
  source_file_id   TEXT,
  built_at         TEXT NOT NULL,
  PRIMARY KEY (scheme_id, as_of_date, issuer_id)
);
CREATE INDEX IF NOT EXISTS ix_siw_issuer ON scheme_issuer_weight(issuer_id);
