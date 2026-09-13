-- DECISIONS V1-29. A disclosure's revision must also turn over when OUR
-- interpretation of it changes, not only when the AMC restates the file.
--
-- `holding.issuer_id` is DERIVED, not disclosed. The AMC publishes a name and
-- an ISIN; which issuer that is comes from the resolution cascade, and the
-- answer changes when the cascade improves. `next_revision` skipped on
-- `source_file_id` alone, so identical bytes meant "already loaded" and a
-- resolver fix could never reach a holding already in the warehouse.
--
-- That made MODULE_0.md §3's own promise false -- "a bug in any one of them is
-- fixed by re-running from the layer to its left, never by editing data". You
-- could re-run all you liked; the loader returned `skipped=1` and the stale
-- issuer stayed. Measured when V1-29 taught the cascade to read an ISIN's
-- issuer segment: the job reported 15.98% unresolved and the stored rows were
-- still the 23.25% ones.
--
-- Recording the version keeps invariant 2 intact. Nothing is updated in place:
-- a changed cascade produces a NEW revision, the previous one keeps its rows
-- and loses `is_current`, and the chain still says what we believed and when.
-- A re-run with an unchanged cascade still skips, which is what stopped a
-- nightly cron from stacking revisions in the first place.
--
-- DEFAULT '0' is deliberately not the current version: every row written
-- before this migration was resolved by a cascade that predates the segment
-- step, and saying so is what makes the first re-run after an upgrade actually
-- re-resolve them rather than skip.

ALTER TABLE holding_disclosure ADD COLUMN resolver_version TEXT NOT NULL DEFAULT '0';

CREATE INDEX IF NOT EXISTS ix_holding_disclosure_resolver
  ON holding_disclosure(scheme_id, as_of_date, resolver_version);
