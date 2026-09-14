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
-- The leading DROP is the whole difference between safe to re-run and not.
-- `apply_migrations` runs this through `executescript`, which is autocommit:
-- a process killed between the INSERT and the DROP below leaves
-- `scheme_aum_next` fully populated with no `schema_migration` row, so the
-- next run inserts the same rows into it again. Reproduced without it --
-- `UNIQUE constraint failed`, on that run and on every run after it, blocking
-- 014 and everything past it behind a table only manual surgery could clear.
-- This file has not been applied to any warehouse yet, so it is corrected
-- here rather than superseded.

DROP TABLE IF EXISTS scheme_aum_next;

CREATE TABLE IF NOT EXISTS scheme_aum_next (
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

DROP TABLE scheme_aum;
ALTER TABLE scheme_aum_next RENAME TO scheme_aum;

-- Dropped with the old table, so it is recreated rather than left behind.
CREATE INDEX IF NOT EXISTS ix_scheme_aum_date ON scheme_aum(as_of_date);
