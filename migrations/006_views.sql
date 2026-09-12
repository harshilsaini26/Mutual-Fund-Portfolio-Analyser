-- `view_definition`. MODULE_6.md §4.1.
--
-- Zone A, not Zone B: the view catalogue is user-independent. It says what
-- views exist and what each one asks, which is a fact about the code rather
-- than about anyone's portfolio.
--
-- Seeded from `src/m6_views/registry.py::VIEW_DEFS`, one way only. A startup
-- check asserts every row here has a registered builder and every builder has
-- a row (§5.3) — a mismatch is a deployment error, not a runtime surprise.
--
-- No DECIMAL_TEXT columns: M6 stores almost no facts and none of them are
-- money. §1.3 rule 1 is that M6 performs no financial computation, and a
-- schema with no decimal column in it is one way of making that structural.

CREATE TABLE IF NOT EXISTS view_definition (
  view_id         TEXT PRIMARY KEY,
  view_name       TEXT NOT NULL,
  module_source   TEXT NOT NULL,   -- m1|m2|m3|m4|m5

  -- The ONE question this view answers, and a REQUIRED column. §2.3: "if the
  -- question can't be stated in a sentence, the view shouldn't exist." This is
  -- a design forcing-function rather than documentation — a view with no
  -- question cannot be constructed, so it cannot be registered.
  question        TEXT NOT NULL,

  chart_type      TEXT NOT NULL,   -- sankey|treemap|heatmap|lorenz|table|kpi|...
  default_scope   TEXT NOT NULL,   -- portfolio|scheme|issuer|sector|manager
  requires_fields TEXT NOT NULL,   -- JSON array: provider methods needed
  drill_targets   TEXT,            -- JSON object: {element_type: view_id}

  -- §7.1. Most views are 'low' and render WITH caveats rather than hiding.
  -- Suppression is for views where a low-confidence number actively misleads,
  -- not merely one that is uncertain — there is no hide_low_confidence
  -- preference and §4.4 locks that in.
  min_confidence  TEXT NOT NULL DEFAULT 'low',

  supports_export INTEGER NOT NULL DEFAULT 1,
  is_builtin      INTEGER NOT NULL DEFAULT 1,
  sort_order      INTEGER
);

CREATE INDEX IF NOT EXISTS ix_view_order ON view_definition(sort_order, view_id);
