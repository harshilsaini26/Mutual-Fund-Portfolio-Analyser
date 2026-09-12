-- `color_assignment`. MODULE_6.md §4.3.
--
-- Zone B, and it is one of the very few real facts M6 owns. Nothing here is
-- derived from upstream data: the mapping from an entity to a colour is a
-- decision made once and then kept, which is the opposite of every other table
-- in this database.
--
-- **That is why it is a table rather than a function of the entity id.** §10.1:
-- if Financials is one colour on the sector chart, another on the treemap and a
-- third on the drift chart, cross-chart reading breaks entirely. Hashing the id
-- to a palette index would be stable too, but it would recolour everything the
-- day the palette changes and gives no way to pin a colour deliberately.
--
-- `CLAUDE.md` invariant 10 does NOT apply: this is not regenerable from
-- anything, so it is never dropped as part of a rebuild.
--
-- Not built here, deliberately: `view_payload_cache`, `saved_view`,
-- `user_display_pref`, `annotation`, `export_log`. MODULE_6's own build
-- sequence puts caching at step 12, after the views exist; the rest are V1.9+
-- surface. `upstream_hash` is still computed on every envelope — it is a
-- required field on the frozen contract — it simply has no table to key yet.

CREATE TABLE IF NOT EXISTS color_assignment (
  entity_type TEXT NOT NULL,   -- sector|scheme|issuer|mcap_bucket|instrument_class
  entity_id   TEXT NOT NULL,
  color_hex   TEXT NOT NULL,
  order_index INTEGER,
  palette_id  TEXT NOT NULL,
  PRIMARY KEY (entity_type, entity_id)
);

CREATE INDEX IF NOT EXISTS ix_color_type ON color_assignment(entity_type, order_index);
