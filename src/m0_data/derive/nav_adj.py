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
from bisect import bisect_right
from decimal import Decimal

from src.common.decimals import NAV_Q


def growth_sibling(conn: sqlite3.Connection, scheme_id: str) -> str | None:
    """The Growth plan of the same scheme, if it has a NAV series of its own.

    Growth and IDCW options of one plan hold ONE portfolio at one TER: their
    returns are identical and the only difference is that the IDCW option pays
    cash out instead of compounding it. That makes the Growth series a complete
    record of what the IDCW plan earned -- which is exactly what `nav_adj` is
    defined to be (§9.1, "IDCW-reinvested total return").
    """
    row = conn.execute(
        "SELECT g.scheme_id FROM scheme s JOIN scheme g"
        "   ON g.scheme_family = s.scheme_family AND g.option = 'growth'"
        " WHERE s.scheme_id = ? AND s.option LIKE 'idcw%'"
        "   AND s.scheme_family IS NOT NULL"
        "   AND EXISTS (SELECT 1 FROM nav_daily n WHERE n.scheme_id = g.scheme_id)"
        " LIMIT 1",
        (scheme_id,),
    ).fetchone()
    return str(row[0]) if row else None


def _from_sibling(
    conn: sqlite3.Connection, navs: list[tuple[object, Decimal]], sibling: str
) -> list[tuple[Decimal, object]] | None:
    """`nav_adj` for an IDCW plan, taken from its Growth sibling's returns.

    Reinvesting every distribution tracks the Growth plan exactly, so

        nav_adj(t) = nav(anchor) * G(t) / G(anchor)

    where `anchor` is the first date both series carry. No distribution amount
    is recovered because none is needed: the sibling already encodes them all,
    and a recovered amount would only be fed back through a factor that
    reproduces this same ratio less exactly.

    Rows before the anchor keep `nav_adj == nav`: there is no sibling NAV to
    scale against and inventing one would be worse than saying nothing.
    """
    g = conn.execute(
        "SELECT nav_date, nav FROM nav_daily"
        " WHERE scheme_id = ? AND nav IS NOT NULL ORDER BY nav_date",
        (sibling,),
    ).fetchall()
    if not g:
        return None
    g_dates = [r[0] for r in g]

    def on_or_before(when: object) -> Decimal | None:
        i = bisect_right(g_dates, when) - 1
        return Decimal(g[i][1]) if i >= 0 else None

    anchor_nav: Decimal | None = None
    anchor_g: Decimal | None = None
    out: list[tuple[Decimal, object]] = []
    for nav_date, nav in navs:
        gv = on_or_before(nav_date)
        if gv is None or gv <= 0:
            out.append((Decimal(nav).quantize(NAV_Q), nav_date))
            continue
        if anchor_nav is None:
            anchor_nav, anchor_g = Decimal(nav), gv
        assert anchor_g is not None
        out.append(((anchor_nav * gv / anchor_g).quantize(NAV_Q), nav_date))
    return out if anchor_nav is not None else None


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

    # No declarations on record is the common case, not the exception:
    # `scheme_idcw` holds 0 rows against 9,187 IDCW-option schemes, so the
    # factor below stays 1 and `nav_adj` silently becomes raw NAV -- a
    # total-return column in name only. The Growth sibling carries the same
    # portfolio's return and settles it without any new source.
    if not idcw:
        sibling = growth_sibling(conn, scheme_id)
        derived = _from_sibling(conn, navs, sibling) if sibling else None
        if derived is not None:
            conn.executemany(
                "UPDATE nav_daily SET nav_adj = ? WHERE nav_date = ? AND scheme_id = ?",
                [(adj, when, scheme_id) for adj, when in derived],
            )
            return len(derived)

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
