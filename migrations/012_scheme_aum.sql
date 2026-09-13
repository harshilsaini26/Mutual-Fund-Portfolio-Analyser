-- DECISIONS V1-49. MODULE_0.md §4's `scheme_aum`, at last.
--
-- §10's V2 -- "THE units check", the one that catches §7.2's 100x error and
-- QUARANTINES rather than warns -- reconciles a disclosure's summed market
-- value against this table. The table did not exist, so V2 has never run on a
-- single disclosure in this warehouse: 0 of 205 carried an `aum_reported`, and
-- a portfolio a hundred times too large would have loaded clean on any tier.
--
-- Two columns beyond §4's schema, and both exist because of what the source
-- actually is.
--
-- `basis` -- AMFI publishes AVERAGE AUM over a quarter, not a month-end
-- balance. §4's schema (an `as_of_date`, a `folio_count`) describes a
-- point-in-time figure, and silently storing an average in it would make every
-- reader that assumes a snapshot quietly wrong. Measured against the two funds
-- whose portfolios we hold, the April-June 2026 average sits -10.4% (HDFC
-- Flexi Cap) and -4.6% (PPFAS Flexi Cap) from their August portfolios -- real
-- economic drift over two months, not error, and far outside V2's +/-3%. The
-- column is what lets V2 pick a tolerance that matches the number.
--
-- `period_label` -- the publisher's own words for the window (`April - June
-- 2026`), kept verbatim so a figure can be traced back to the file it came
-- from without re-deriving which quarter `as_of_date` implies.
--
-- `folio_count` is in the schema and will be NULL: the endpoint this is
-- loaded from does not carry it. NULL rather than 0, because "not published"
-- and "no folios" are different facts.

CREATE TABLE IF NOT EXISTS scheme_aum (
  scheme_id      TEXT NOT NULL REFERENCES scheme(scheme_id),
  as_of_date     DATE NOT NULL,
  aum_inr        DECIMAL_TEXT,            -- rupees ABSOLUTE, already converted
  folio_count    BIGINT,
  -- point_in_time | quarterly_average
  basis          TEXT NOT NULL DEFAULT 'point_in_time',
  period_label   TEXT,
  source_file_id TEXT REFERENCES raw_file(file_id),
  ingested_at    TIMESTAMP,
  PRIMARY KEY (scheme_id, as_of_date)
);

CREATE INDEX IF NOT EXISTS ix_scheme_aum_date ON scheme_aum(as_of_date);
