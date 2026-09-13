-- DECISIONS V1-37. A disclosure describes a scheme, not an ISIN.
--
-- Direct and Regular, Growth and IDCW, are share classes of one pool of assets.
-- They differ in what they charge and what they pay out, never in what they
-- hold, so one published portfolio is the portfolio of every ISIN in its
-- family. Definitional, not an approximation.
--
-- Before this, `holding.scheme_id` was the single ISIN a file happened to be
-- loaded against, and every other share class of the same scheme resolved to
-- `__NO_DISCLOSURE__`. Five disclosures covered five ISINs of the 34 they
-- describe -- and the ones being refused were mostly REGULAR plans, which is
-- what distributors sell.
--
-- NULL is meaningful and is the safe state: it means this scheme does not fan
-- out, either because its family could not be shown coherent or because the
-- derivation has not run. A NULL family behaves exactly as the warehouse did
-- before this migration, so nothing degrades if the derive step is skipped.
--
-- Populated by `src/m0_data/derive/scheme_family.py`, which refuses any family
-- spanning more than one SEBI category -- three of 4,195 do, all of them AMFI
-- spelling one category two ways, and none worth the risk of merging two funds.

ALTER TABLE scheme ADD COLUMN scheme_family TEXT;

CREATE INDEX IF NOT EXISTS ix_scheme_family
  ON scheme(amc_id, scheme_family) WHERE scheme_family IS NOT NULL;
