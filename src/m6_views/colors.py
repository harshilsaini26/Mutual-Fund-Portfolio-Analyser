"""Colour assignment. MODULE_6.md §10.

**Stability is the requirement, not prettiness.** If Financials is one colour on
the sector chart, another on the treemap and a third on the drift chart,
cross-chart reading breaks entirely — and cross-chart reading is most of what a
portfolio screen is for. So a colour is assigned **once, globally,
deterministically**, stored in `color_assignment`, and read back by every view.

Two rules that are not about aesthetics:

1. **Never encode by colour alone** (§10.3). Every colour-encoded distinction
   needs a redundant channel — a plus/minus prefix, a shape, a dashed stroke.
   Roughly one in twelve men cannot distinguish the obvious gain/loss hues, and
   a chart that is only legible in colour is not legible.
2. **Synthetics get a fixed neutral grey with a hatched fill** and are never
   given a palette colour. `__UNRESOLVED__` is missing mass, not a company, and
   it must not look like one.
"""

from __future__ import annotations

import sqlite3

#: Okabe-Ito, the default per §10.2 — eight hues distinguishable under every
#: common form of colour vision deficiency. Ordered as published.
OKABE_ITO = (
    "#E69F00",  # orange
    "#56B4E9",  # sky blue
    "#009E73",  # bluish green
    "#F0E442",  # yellow
    "#0072B2",  # blue
    "#D55E00",  # vermillion
    "#CC79A7",  # reddish purple
    "#000000",  # black
)

PALETTES: dict[str, tuple[str, ...]] = {"okabe_ito": OKABE_ITO}

DEFAULT_PALETTE = "okabe_ito"

#: §10.2. Semantic colours are fixed and NEVER reused for a category — if a
#: sector is allocated the gain hue, every gain on the screen becomes ambiguous.
SEMANTIC = {
    "gain": "#009E73",
    "loss": "#D55E00",
    "synthetic": "#9E9E9E",
    "neutral": "#767676",
}

#: §10.2 again: the synthetics carry a pattern as well as a colour, so they stay
#: distinguishable in greyscale and to a colour-blind reader.
SYNTHETIC_PATTERN = "hatch"


def assign_colors(
    conn: sqlite3.Connection,
    entity_type: str,
    entities: list[str],
    palette_id: str = DEFAULT_PALETTE,
) -> dict[str, str]:
    """§10.1. Assign once, globally, deterministically. Idempotent.

    Returns the full mapping for the entities asked about, including ones
    already assigned. New entities are appended after the highest existing
    `order_index`, so adding a fund next month does not recolour the ones
    already on screen — which is the entire point.

    **Sorted before assignment.** Without it, two runs over the same set in a
    different order produce different colours, and the mapping is supposed to be
    a function of the entity rather than of when it was first seen.
    """
    palette = PALETTES[palette_id]
    existing = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT entity_id, color_hex FROM color_assignment"
            " WHERE entity_type = ?",
            (entity_type,),
        )
    }
    row = conn.execute(
        "SELECT max(order_index) FROM color_assignment WHERE entity_type = ?",
        (entity_type,),
    ).fetchone()
    next_index = (row[0] + 1) if row and row[0] is not None else 0

    for entity in sorted(entities):
        if entity in existing:
            continue
        colour = palette[next_index % len(palette)]
        conn.execute(
            "INSERT INTO color_assignment"
            " (entity_type, entity_id, color_hex, order_index, palette_id)"
            " VALUES (?,?,?,?,?)",
            (entity_type, entity, colour, next_index, palette_id),
        )
        existing[entity] = colour
        next_index += 1
    conn.commit()
    return {e: existing[e] for e in entities if e in existing}


def color_for(
    issuer_id: str, is_synthetic: bool, assigned: dict[str, str]
) -> tuple[str, str | None]:
    """The colour and the redundant channel for one issuer.

    Returns `(hex, pattern)`. The pattern is what §10.3 requires so the
    distinction survives greyscale printing and colour-blind vision — it is not
    decoration, and a renderer that ignores it has broken the rule.
    """
    if is_synthetic:
        return SEMANTIC["synthetic"], SYNTHETIC_PATTERN
    return assigned.get(issuer_id, SEMANTIC["neutral"]), None


__all__ = [
    "DEFAULT_PALETTE",
    "OKABE_ITO",
    "PALETTES",
    "SEMANTIC",
    "SYNTHETIC_PATTERN",
    "assign_colors",
    "color_for",
]
