"""Populate `scheme.scheme_family`. MODULE_0.md §3 (L2 -> L3), DECISIONS V1-37.

Derived, re-runnable, reading nothing but the scheme master. Re-running after an
AMFI load is how a newly listed share class joins its family.

**A family that cannot be shown coherent does not fan out.** The coherence check
is SEBI category, because two different funds sharing an AMC and a qualifier-free
name are what would merge if the key were wrong. Three families of 4,195 span
more than one — all AMFI writing the same category two ways — and those get NULL.

Refusing costs nothing today (none of the three has a disclosure) and the
alternative is filing one fund's portfolio against another fund's ISIN, which
would not look like an error.
"""

from __future__ import annotations

import sqlite3
from collections import defaultdict

from src.m0_data.normalise.family import family_key


def derive_scheme_families(conn: sqlite3.Connection) -> dict[str, int]:
    """Write `scheme_family` for every scheme. Returns a small summary.

    Idempotent: it recomputes from `scheme_name` each time and overwrites, so a
    scheme that leaves an incoherent family gains a key on the next run and one
    that joins an incoherent family loses it.
    """
    rows = conn.execute(
        "SELECT scheme_id, scheme_name, amc_id, sebi_category FROM scheme"
    ).fetchall()

    grouped: dict[tuple[str, str], list[tuple[str, str | None]]] = defaultdict(list)
    for scheme_id, scheme_name, amc_id, category in rows:
        key = family_key(str(scheme_name))
        if not key:
            continue
        grouped[(str(amc_id), key)].append((str(scheme_id), category))

    assigned = 0
    refused = 0
    updates: list[tuple[str | None, str]] = []
    for (_amc, key), members in grouped.items():
        categories = {str(c) for _sid, c in members if c}
        if len(categories) > 1:
            refused += len(members)
            updates.extend((None, sid) for sid, _c in members)
            continue
        assigned += len(members)
        updates.extend((key, sid) for sid, _c in members)

    conn.executemany(
        "UPDATE scheme SET scheme_family = ? WHERE scheme_id = ?", updates
    )
    return {
        "schemes": len(rows),
        "families": len(grouped),
        "assigned": assigned,
        "refused_incoherent": refused,
    }


def disclosure_scheme_for(
    conn: sqlite3.Connection, scheme_id: str, on_or_before: str | None = None
) -> str:
    """The scheme whose disclosure describes `scheme_id`'s portfolio.

    Returns `scheme_id` itself when it has one, which is the ordinary case and
    costs one indexed lookup. Otherwise the sibling share class that does.

    **Deterministic by construction** (`CLAUDE.md` invariant 10): siblings are
    ordered by disclosure date and then by `scheme_id`, so a rebuild picks the
    same one. They hold the same portfolio, so which is picked does not change
    a number — but it does change `holding.scheme_id`, and a figure that moves
    between rebuilds is indistinguishable from a figure that is wrong.

    A quarantined disclosure does not count, here or among the siblings
    (V1-66): a share class whose own failed while a sibling's passed is served
    the sibling's, since they hold the same portfolio.
    """
    own = conn.execute(
        "SELECT 1 FROM holding_disclosure WHERE scheme_id = ? AND is_current = 1"
        " AND validation_status <> 'quarantined' LIMIT 1",
        (scheme_id,),
    ).fetchone()
    if own:
        return scheme_id

    family = conn.execute(
        "SELECT amc_id, scheme_family FROM scheme WHERE scheme_id = ?", (scheme_id,)
    ).fetchone()
    if not family or not family[1]:
        return scheme_id

    sql = (
        "SELECT s.scheme_id, max(d.as_of_date) AS latest"
        " FROM scheme s JOIN holding_disclosure d"
        "   ON d.scheme_id = s.scheme_id AND d.is_current = 1"
        "   AND d.validation_status <> 'quarantined'"
        " WHERE s.amc_id = ? AND s.scheme_family = ?"
    )
    params: list[object] = [family[0], family[1]]
    if on_or_before is not None:
        sql += " AND d.as_of_date <= ?"
        params.append(on_or_before)
    sql += " GROUP BY s.scheme_id ORDER BY latest DESC, s.scheme_id ASC LIMIT 1"
    row = conn.execute(sql, tuple(params)).fetchone()
    return str(row[0]) if row else scheme_id


def disclosed_scheme_ids(conn: sqlite3.Connection) -> list[str]:
    """Every scheme with a current disclosure, ORDERED.

    Three callers asked this question with the same SQL and three different
    answers about order: `backfill_scheme_nav` sorted in SQL, `thin_warehouse`
    sorted in Python, and `weights.rebuild_weights` did neither — so the
    rebuild's iteration order was SQLite's, and it reaches the physical row
    order of `lookthrough_contribution`, which `engine.compute_lookthrough`
    appends to and never sorts. Invariant 10 wants a rebuild to reproduce
    byte-identical output.

    Ordered here so a caller cannot forget, which is the same argument
    `disclosure_scheme_for` above makes for its own read.
    """
    return [
        str(r[0])
        for r in conn.execute(
            "SELECT DISTINCT scheme_id FROM holding_disclosure"
            " WHERE is_current = 1 ORDER BY scheme_id"
        )
    ]


__all__ = [
    "derive_scheme_families",
    "disclosed_scheme_ids",
    "disclosure_scheme_for",
]
