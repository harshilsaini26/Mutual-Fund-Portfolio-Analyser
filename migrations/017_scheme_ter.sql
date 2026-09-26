-- `scheme_ter`: what each share class charges, dated. DECISIONS V1-78, which
-- closes V0-01; MODULE_0.md §4's table, changed in three ways.
--
-- Both figures (V0-01). AMFI publishes the base expense ratio and the total
-- TER side by side. The total is what a fund deducts from its price, so it is
-- what the pages show; the base is kept because a reader comparing a fund
-- house's own factsheet will meet it, and it cannot be rebuilt from the total.
--
-- Monthly, and no `valid_to`. AMFI publishes a figure for every day, but the
-- total moves with trading costs (a running average that resets each month:
-- 1,142 of 4,708 fund-plans changed within August 2026), so a daily series
-- would be ~100,000 rows a month for a figure shown to two places. One row per
-- share class per finished month instead: the figure on the month's last
-- published day, `valid_from` that day. It holds until the next row's
-- `valid_from`; §4's `valid_to` would have to be written by UPDATE when the
-- next month arrives, and fact rows are never updated (invariant 2).
--
-- `revision`, for a correction: AMFI re-publishing a different figure for a
-- day already loaded appends revision 2 rather than rewriting revision 1.
--
-- Percent, as §4 has it and as every percentage here is stored (0.6200 is
-- 0.62% a year).

CREATE TABLE IF NOT EXISTS scheme_ter (
  scheme_id      TEXT NOT NULL REFERENCES scheme(scheme_id),
  valid_from     DATE NOT NULL,
  revision       INTEGER NOT NULL DEFAULT 1,
  total_ter      DECIMAL_TEXT NOT NULL,   -- percent a year, e.g. 0.6200
  base_ter       DECIMAL_TEXT,            -- percent a year; NULL if not published
  source_file_id TEXT REFERENCES raw_file(file_id),
  ingested_at    TIMESTAMP,
  PRIMARY KEY (scheme_id, valid_from, revision)
);
