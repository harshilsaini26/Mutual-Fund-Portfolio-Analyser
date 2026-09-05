"""The contract M0 owes M1. MODULE_0.md §10.3, asserting §11.4.

Runs after every M0 load and before any M1 rebuild. Cheap, and it converts a
class of silent corruption into a loud failure — which is the same argument
`PLAN.md` §7 makes for the V0 reconciliation gate.

Only the guarantees V0.4 can actually check are checked. `scheme_tax_class` and
`scheme_ter` have no source yet, so asserting them would be asserting the
absence of a table; those lines are named as `not_checked` in the result rather
than silently skipped, because a gate that quietly checks less than it claims is
worse than one that checks nothing.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import date, timedelta
from itertools import pairwise

#: §11.4: "nav_daily has no gaps > 3 business days for held schemes".
MAX_NAV_GAP_BUSINESS_DAYS = 3


class ContractViolation(RuntimeError):
    """M0 cannot supply what M1 requires. The rebuild must not proceed."""


@dataclass
class ContractReport:
    violations: list[str] = field(default_factory=list)
    not_checked: list[str] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.violations


def business_days_between(start: date, end: date) -> int:
    """Weekdays strictly between two dates. Holidays are not modelled.

    Deliberately an over-estimate of trading days: a market holiday inside a
    gap makes this report more days than were actually tradeable, so the check
    errs toward flagging. A missed gap is a silent wrong return; a flagged one
    that turns out to be Diwali costs a look.
    """
    days = 0
    cursor = start + timedelta(days=1)
    while cursor < end:
        if cursor.weekday() < 5:
            days += 1
        cursor += timedelta(days=1)
    return days


def nav_gaps(
    conn: sqlite3.Connection, scheme_id: str, start: date, end: date
) -> list[tuple[date, date, int]]:
    """Runs of missing NAV longer than the tolerance, as (from, to, days)."""
    rows = conn.execute(
        "SELECT nav_date FROM nav_daily WHERE scheme_id = ? AND nav_date BETWEEN ? AND ? "
        "ORDER BY nav_date",
        (scheme_id, start, end),
    ).fetchall()
    dates = [
        r[0] if isinstance(r[0], date) else date.fromisoformat(str(r[0]))
        for r in rows
    ]
    gaps = []
    for earlier, later in pairwise(dates):
        span = business_days_between(earlier, later)
        if span > MAX_NAV_GAP_BUSINESS_DAYS:
            gaps.append((earlier, later, span))
    return gaps


def assert_m1_contract(
    conn: sqlite3.Connection,
    held_scheme_ids: list[str],
    earliest_txn: date,
    today: date,
) -> ContractReport:
    """§10.3. Returns a report; the caller decides whether to raise.

    Returning rather than raising is a departure from §10.3's sketch. One
    scheme with a NAV gap should not hide a second scheme with a cyclic merger
    chain — the first `raise` would end the loop and the second problem would
    surface only after the first was fixed. Collect everything, then fail once.
    """
    report = ContractReport()
    report.not_checked.append("scheme_tax_class coverage — no source loaded in V0.4")
    report.not_checked.append("scheme_ter coverage — deferred to V2 (V0-18)")

    for scheme_id in held_scheme_ids:
        # Indexed by position, not by name: this must work whatever
        # `row_factory` the caller happens to have set on the connection.
        row = conn.execute(
            "SELECT option FROM scheme WHERE scheme_id = ?", (scheme_id,)
        ).fetchone()
        if row is None:
            report.violations.append(f"{scheme_id}: not in the scheme master")
            continue
        option = str(row[0])

        for earlier, later, span in nav_gaps(conn, scheme_id, earliest_txn, today):
            report.violations.append(
                f"{scheme_id}: NAV gap {earlier} -> {later} ({span} business days)"
            )

        if option.startswith("idcw"):
            missing = conn.execute(
                "SELECT COUNT(*) FROM nav_daily WHERE scheme_id = ? AND nav_adj IS NULL",
                (scheme_id,),
            ).fetchone()[0]
            if missing:
                report.violations.append(
                    f"{scheme_id}: IDCW option with {missing} rows missing nav_adj"
                )

        if _merger_chain_has_cycle(conn, scheme_id):
            report.violations.append(f"{scheme_id}: cyclic merger chain")

    # §11.4: the grandfathering NAV. MODULE_1.md §7.5 cannot compute a
    # grandfathered cost basis without it, and OPEN-07 makes it mandatory in
    # the backfill for exactly this reason.
    if earliest_txn < date(2018, 1, 31):
        for scheme_id in held_scheme_ids:
            has = conn.execute(
                "SELECT 1 FROM nav_daily WHERE scheme_id = ? AND nav_date = ?",
                (scheme_id, date(2018, 1, 31)),
            ).fetchone()
            if not has:
                report.violations.append(
                    f"{scheme_id}: no 31-Jan-2018 NAV; grandfathering unavailable"
                )
    return report


def _merger_chain_has_cycle(conn: sqlite3.Connection, scheme_id: str) -> bool:
    seen = {scheme_id}
    current = scheme_id
    while True:
        row = conn.execute(
            "SELECT merged_into FROM scheme WHERE scheme_id = ?", (current,)
        ).fetchone()
        if row is None or row[0] is None:
            return False
        current = row[0]
        if current in seen:
            return True
        seen.add(current)
