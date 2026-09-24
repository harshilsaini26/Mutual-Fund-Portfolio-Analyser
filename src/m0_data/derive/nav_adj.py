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
from typing import Any

from src.common.decimals import NAV_Q

#: A re-denomination moves NAV by a power of ten, and only these. Restricted
#: deliberately: `INF174KA1DB4` drops 10.0727 -> 0.0001 in one day, which is
#: 10^-5 and a clean power of ten, but it is a dying fund's last row rather
#: than a split. Ten- and hundred-fold are what AMCs actually do; anything
#: wilder is a defect and must stay visible as one.
SPLIT_RATIOS = (Decimal(100), Decimal(10), Decimal("0.1"), Decimal("0.01"))

#: A split lands NEAR its ratio, not on it: the fund also moved that day. An
#: overnight fund barely does -- ICICI came out at x10.0014 -- but an equity
#: ETF does, and the real ones here run from x0.09729 to x0.10302, up to 3%
#: off. 1% was calibrated on the overnight fund and missed 13 ETF splits.
#:
#: 10% is safe because of how far apart the two populations are: this admits
#: only 0.09-0.11, 0.009-0.011, 9-11 and 90-110, and no fund has a single-day
#: move anywhere near those. The gap between "re-denomination" and "bad day"
#: is three orders of magnitude, not a judgement call.
SPLIT_TOLERANCE = Decimal("0.10")


def _split_ratio(previous: Decimal, current: Decimal) -> Decimal | None:
    """The re-denomination between two NAVs, or None if this is a real move."""
    if previous <= 0:
        return None
    moved = current / previous
    for ratio in SPLIT_RATIOS:
        if abs(moved / ratio - 1) <= SPLIT_TOLERANCE:
            return ratio
    return None


def rescale_splits(
    navs: list[tuple[Any, Decimal]],
) -> list[tuple[Any, Decimal]]:
    """The series on ONE scale, with unit re-denominations divided out.

    A 10-for-1 re-denomination multiplies units and divides NAV; the holding
    is worth exactly what it was a moment earlier. Left alone it reads as a
    900% gain, which is how ICICI Prudential Overnight Fund -- a fund that
    cannot move 1% in a day -- appeared to return 14.8x over seven years.

    55 schemes in this warehouse carry one, clustered on three dates, all at
    x10, x1/10 or x1/100.

    Everything is brought to the LATEST scale: an earlier NAV is multiplied by
    every split that came after it. That way `nav_adj` still reads on the same
    order as today's `nav`, which §9.1 wants, and only history is restated --
    which is what a re-denomination does anyway.

    MODULE_0.md §2.2 states the rule for securities: "Do not use bhavcopy
    close for return computation without applying `security_adjustment` -- a
    bonus issue otherwise reads as a 50% crash." A fund's units split for the
    same reasons and need the same treatment.
    """
    if len(navs) < 2:
        return list(navs)

    # Walk forward to find the splits, then apply each one to everything
    # BEFORE it. Two passes rather than one because a split's multiplier
    # applies retroactively and is not known until it happens.
    splits: list[tuple[int, Decimal]] = []
    for i in range(1, len(navs)):
        ratio = _split_ratio(navs[i - 1][1], navs[i][1])
        if ratio is not None:
            splits.append((i, ratio))
    if not splits:
        return list(navs)

    multiplier = [Decimal(1)] * len(navs)
    running = Decimal(1)
    for i in range(len(navs) - 1, -1, -1):
        multiplier[i] = running
        for at, ratio in splits:
            if at == i:
                running *= ratio
    return [(d, (v * multiplier[i]).quantize(NAV_Q)) for i, (d, v) in enumerate(navs)]


def growth_sibling(conn: sqlite3.Connection, scheme_id: str) -> str | None:
    """The Growth plan of the same scheme, if it has a NAV series of its own.

    Growth and IDCW options of one plan hold ONE portfolio at one TER: their
    returns are identical and the only difference is that the IDCW option pays
    cash out instead of compounding it. That makes the Growth series a complete
    record of what the IDCW plan earned -- which is exactly what `nav_adj` is
    defined to be (§9.1, "IDCW-reinvested total return").

    **The same plan's Growth**: a family holds Direct and Regular together, and
    their TERs differ. Only when the plan has no Growth of its own is the other
    plan's taken -- off by the TER gap, but raw NAV would miss every
    distribution. Ordered, so a rebuild picks the same one (invariant 10).
    """
    row = conn.execute(
        "SELECT g.scheme_id FROM scheme s JOIN scheme g"
        "   ON g.scheme_family = s.scheme_family AND g.option = 'growth'"
        " WHERE s.scheme_id = ? AND s.option LIKE 'idcw%'"
        "   AND s.scheme_family IS NOT NULL"
        "   AND EXISTS (SELECT 1 FROM nav_daily n WHERE n.scheme_id = g.scheme_id)"
        " ORDER BY g.plan IS s.plan DESC, g.scheme_id"
        " LIMIT 1",
        (scheme_id,),
    ).fetchone()
    return str(row[0]) if row else None


def _from_sibling(
    conn: sqlite3.Connection, navs: list[tuple[Any, Decimal]], sibling: str
) -> list[tuple[Decimal, Any]] | None:
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
    g_raw = conn.execute(
        "SELECT nav_date, nav FROM nav_daily"
        " WHERE scheme_id = ? AND nav IS NOT NULL ORDER BY nav_date",
        (sibling,),
    ).fetchall()
    # The sibling needs the same treatment: its split would otherwise travel
    # into this plan's adjusted series as a tenfold return it never had.
    g = rescale_splits([(d, Decimal(v)) for d, v in g_raw])
    if not g:
        return None
    g_dates = [r[0] for r in g]

    def on_or_before(when: Any) -> Decimal | None:
        i = bisect_right(g_dates, when) - 1
        return Decimal(g[i][1]) if i >= 0 else None

    anchor_nav: Decimal | None = None
    anchor_g: Decimal | None = None
    out: list[tuple[Decimal, Any]] = []
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
    raw = conn.execute(
        "SELECT nav_date, nav FROM nav_daily WHERE scheme_id = ? ORDER BY nav_date",
        (scheme_id,),
    ).fetchall()
    if not raw:
        return 0
    # Before anything else: a re-denomination is not a return, and leaving it
    # in makes every figure downstream wrong by a factor of ten.
    navs = rescale_splits([(d, Decimal(v)) for d, v in raw if v is not None])
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
    updates: list[tuple[Decimal, str, Any]] = []
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
