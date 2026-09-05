"""IDCW-adjusted NAV. MODULE_0.md §9.1.

Raw NAV of an IDCW plan drops on every payout, so **any return computed on
unadjusted NAV is wrong** — it reads a distribution as a loss. `nav_adj` is
built once at ingest and is what M1's TWRR actually consumes.

For a Growth option with no IDCW events `nav_adj == nav`. It is populated
anyway, per §9.1, so downstream code has one path and never has to ask which
column to read — the question that produces the bug.
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal

from src.common.decimals import NAV_Q


def build_nav_adj(conn: sqlite3.Connection, scheme_id: str) -> int:
    """Chain the reinvestment factor forward across the series. §9.1.

    On a payout date the factor grows by `prev / (prev - amount)` — the ratio
    by which units multiply if the distribution is reinvested at the ex-date
    NAV. Every subsequent NAV carries the accumulated factor, so the series
    becomes a total-return series.

    Two guards the spec's sketch leaves implicit:

    - The **first** row cannot be adjusted; there is no previous NAV to reinvest
      at. §9.1 writes `i > 0`, and the reason is that a payout on the first day
      of the series has no denominator.
    - A payout at or above the previous NAV would make the denominator zero or
      negative. That is a data error, not an arithmetic one, and is skipped
      rather than allowed to produce a negative or infinite factor.
    """
    navs = conn.execute(
        "SELECT nav_date, nav FROM nav_daily WHERE scheme_id = ? ORDER BY nav_date",
        (scheme_id,),
    ).fetchall()
    if not navs:
        return 0

    idcw = {
        row[0]: row[1]
        for row in conn.execute(
            "SELECT record_date, amount_per_unit FROM scheme_idcw WHERE scheme_id = ?",
            (scheme_id,),
        )
    }

    factor = Decimal(1)
    updates: list[tuple[Decimal, str, object]] = []
    for i, (nav_date, nav) in enumerate(navs):
        amount = idcw.get(nav_date)
        if amount is not None and i > 0:
            previous = navs[i - 1][1]
            if previous is not None and previous > amount > 0:
                factor *= previous / (previous - amount)
        updates.append(((nav * factor).quantize(NAV_Q), scheme_id, nav_date))

    conn.executemany(
        "UPDATE nav_daily SET nav_adj = ? WHERE scheme_id = ? AND nav_date = ?",
        updates,
    )
    return len(updates)


def build_all_nav_adj(conn: sqlite3.Connection) -> int:
    """Rebuild `nav_adj` for every scheme that has a NAV series.

    Cheap enough to run wholesale: `nav_adj` is derived, so §3's invariant says
    it must be regenerable from L2 rather than patched, and a partial rebuild
    is how the two drift apart.
    """
    total = 0
    schemes = conn.execute("SELECT DISTINCT scheme_id FROM nav_daily").fetchall()
    for (scheme_id,) in schemes:
        total += build_nav_adj(conn, scheme_id)
    return total
