-- Index identity and index levels. MODULE_0.md §2.1 sources S11/S12, §4.6.
--
-- `scheme.benchmark_id` has existed since 002 and is populated on 0 of 19,598
-- schemes, because the two things that would fill it did not exist: a table of
-- indices to point at, and a level series to make pointing worth anything.
-- These are those two tables.
--
-- WHY `is_total_return` IS NOT NULL
--   `PLAN.md` §9.4 is decided: TRI only, and §2.2 says to "refuse to use PRI
--   series for alpha". A price-return index omits dividends, so alpha measured
--   against one is overstated by roughly the index's dividend yield -- about
--   1.3% a year on the Nifty 50. That error runs IN OUR FAVOUR, which is the
--   worst kind: nothing downstream looks wrong. A nullable flag would let a
--   PRI series arrive as NULL and be compared by a reader that treated NULL as
--   false, so the column takes no NULLs and `IndexPoint`'s loader sets it from
--   the endpoint it came from rather than from the index's name.
--
-- WHY THERE IS NO `net_level` COLUMN
--   NSE's TRI endpoint returns `TotalReturnsIndex` (gross) and `NTR_Value`
--   (net of withholding tax) side by side, and only the gross one is stored.
--   Not a judgement that NTR is worthless -- it is that §3.1 archives the raw
--   response permanently and content-hashed, so NTR is already on disk and a
--   later migration can derive it without re-fetching. Invariant 10 says the
--   warehouse rebuilds from the archive; that is exactly what makes a column
--   nobody reads YAGNI rather than a decision to discard data.
--
-- WHY `index_id` CARRIES ITS PROVIDER
--   NSE and BSE both publish a "Housing" index and both publish broad-market
--   indices whose names differ by a word. `'NSE:NIFTY_50_TRI'` cannot collide
--   with a BSE index; `'NIFTY 50'` eventually would. The prefixed form matches
--   `scheme_id`'s own `'AMFI:{code}'` fallback from 002.
--
-- Foreign keys are declared and not enforced -- `connect()` does not set
-- `PRAGMA foreign_keys=ON`, so every REFERENCES in this schema documents a
-- relationship rather than policing one. These are written the same way the
-- other twelve migrations write theirs, deliberately, so the schema reads
-- consistently.

CREATE TABLE IF NOT EXISTS benchmark_index (
  index_id        TEXT PRIMARY KEY,        -- 'NSE:NIFTY_50_TRI'
  index_name      TEXT NOT NULL,           -- verbatim, as the provider prints it
  -- The provider's OTHER spelling, where it publishes one. NSE's catalogue
  -- gives every index both a long name and a trading name, and they are not
  -- always the same words: `Nifty Private Bank` trades as `Nifty Pvt Bank`.
  -- Both spellings turn up inside fund names, and `index_key` cannot bridge
  -- Pvt/Private -- it normalises punctuation and case, not vocabulary. One
  -- nullable column rather than an alias table, because NSE publishes exactly
  -- two spellings and a third has never appeared; a wider vocabulary problem
  -- would be `name_alias`'s shape, not this one's.
  trading_name    TEXT,
  is_total_return INTEGER NOT NULL,        -- §9.4. No NULLs: see above.
  provider        TEXT,                    -- 'NSE Indices' | 'BSE'
  base_date       DATE,
  base_value      DECIMAL_TEXT,
  -- NULL until levels arrive. A row here is an index we know EXISTS; levels
  -- are a separate fact, and 259 catalogue entries against a handful of
  -- backfilled series is the normal state, not a gap.
  first_seen      DATE,
  last_seen       DATE
);

CREATE INDEX IF NOT EXISTS ix_benchmark_index_tr
  ON benchmark_index(is_total_return);

-- One level per index per day. Levels are facts about a published series, so
-- the same (index_id, level_date) must never carry two values; a restatement
-- by the provider replaces the row rather than adding one, which is the same
-- treatment `nav_daily` gives a restated NAV.
CREATE TABLE IF NOT EXISTS index_level (
  index_id       TEXT NOT NULL REFERENCES benchmark_index(index_id),
  level_date     DATE NOT NULL,
  level          DECIMAL_TEXT NOT NULL,   -- DECIMAL(18,6), the gross TRI
  source_file_id TEXT REFERENCES raw_file(file_id),
  PRIMARY KEY (index_id, level_date)
);

-- M2 reads a whole window for one index at a time, so the PK's leading column
-- already serves it. This one is for the other direction: "what did every
-- benchmark do on this date", which the look-through comparison asks.
CREATE INDEX IF NOT EXISTS ix_index_level_date ON index_level(level_date);
