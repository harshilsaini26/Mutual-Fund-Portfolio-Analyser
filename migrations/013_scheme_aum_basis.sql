-- A CHECK for `scheme_aum.basis`. Review of DECISIONS V1-52.
--
-- 012 declared the vocabulary `point_in_time | quarterly_average` in a COMMENT
-- and left the column taking any TEXT at all. §10's V2 picks its tolerance
-- from that value, and `tolerance_for` raises `UnknownAumBasis` on anything
-- else (invariant 5: a plausible wrong answer is worse than a crash) -- so one
-- unreadable row does not degrade one check, it stops V2 for every disclosure
-- that reads it.
--
-- And it stops it in the worst available shape. `jobs/ingest_inbox.py` catches
-- the raise per sheet, so a single bad row is reported 119 times for one
-- workbook, each time as though that sheet were at fault, and `main` exits 0
-- having loaded nothing. The condition is a property of THIS table. The
-- constraint belongs on this table, not in every reader's batch loop.
--
-- SQLite cannot ALTER a CHECK onto an existing column, so this is the standard
-- rebuild. The table is derived and regenerable in full from
-- `jobs/fetch_aum.py` (invariant 10), which is what makes a rebuild the cheap
-- option here.
--
-- WRAPPED, because statement order is not a crash plan. `apply_migrations`
-- runs this through `executescript`, which commits what is pending and then
-- executes the script as written -- so without an explicit transaction every
-- statement below lands on its own and a process killed between any two of
-- them leaves the rebuild half done.
--
-- Two windows, and ordering can only ever close one of them. Killed between
-- the INSERT and `DROP TABLE scheme_aum`, the scratch table is left populated
-- with no `schema_migration` row, and the next run inserts the same rows into
-- it again: `UNIQUE constraint failed`, on that run and every run after it,
-- blocking 014 and everything past it. Killed between `DROP TABLE scheme_aum`
-- and the RENAME, the ONLY copy of the data is in `scheme_aum_next` -- and a
-- leading `DROP TABLE IF EXISTS` on the next run destroys it. The guard that
-- closes the first window widens the second.
--
-- SQLite has transactional DDL, so BEGIN/COMMIT closes both: a crash anywhere
-- inside rolls the whole rebuild back and `scheme_aum` is untouched. The
-- leading DROP stays for the case a transaction cannot cover -- a scratch
-- table left behind by some earlier hand-run -- but it is no longer what
-- makes this safe.
--
-- This file has not been applied to any warehouse yet (the live one is at
-- 012), so it is corrected here rather than superseded. `apply.py`'s rule is
-- that a migration is never edited ONCE APPLIED.
--
-- IF THIS MIGRATION FAILS, it is because a row already in `scheme_aum` holds a
-- `basis` the constraint will not take, and the whole rebuild rolls back with
-- the table untouched. `apply.py` names this file in the error; find the row
-- with
--
--     SELECT scheme_id, as_of_date, basis FROM scheme_aum
--      WHERE basis NOT IN ('point_in_time', 'quarterly_average');
--
-- and delete it, or drop the table and reload it from scratch --
-- `python -m jobs.fetch_aum` rebuilds every row of it (invariant 10). What it
-- must NOT do is widen the vocabulary to admit the bad value: §10's V2 picks
-- its tolerance from this column, and a basis no reader knows is a check that
-- does not run.
--
-- The column list below is 012's. Re-running this file against a table that a
-- LATER migration has widened would copy the columns named here and silently
-- drop the rest -- which is why `apply.py` commits each `schema_migration` row
-- with its own migration rather than at the end of the loop, so an applied
-- rebuild is never a candidate for re-running.

BEGIN;

DROP TABLE IF EXISTS scheme_aum_next;

CREATE TABLE scheme_aum_next (
  scheme_id      TEXT NOT NULL REFERENCES scheme(scheme_id),
  as_of_date     DATE NOT NULL,
  aum_inr        DECIMAL_TEXT,            -- rupees ABSOLUTE, already converted
  folio_count    BIGINT,
  basis          TEXT NOT NULL DEFAULT 'point_in_time'
                 CHECK (basis IN ('point_in_time', 'quarterly_average')),
  period_label   TEXT,
  source_file_id TEXT REFERENCES raw_file(file_id),
  ingested_at    TIMESTAMP,
  PRIMARY KEY (scheme_id, as_of_date)
);

INSERT INTO scheme_aum_next
  SELECT scheme_id, as_of_date, aum_inr, folio_count, basis, period_label,
         source_file_id, ingested_at
  FROM scheme_aum;

-- CRASH WINDOW 1 -- killed here, the scratch table is populated and the
-- source is still whole. The transaction rolls both away.
DROP TABLE scheme_aum;
-- CRASH WINDOW 2 -- killed here, the scratch table is the ONLY copy of the
-- data, and a leading `DROP TABLE IF EXISTS` on the next run would destroy it.
-- This is the window statement order cannot close. The transaction can.
ALTER TABLE scheme_aum_next RENAME TO scheme_aum;

-- Dropped with the old table, so it is recreated rather than left behind.
CREATE INDEX IF NOT EXISTS ix_scheme_aum_date ON scheme_aum(as_of_date);

COMMIT;
