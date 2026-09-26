"""The funds the public build covers: one share class per live fund, Direct plan.

AMFI's list keeps every scheme it has ever published -- 19,676 rows, and nothing
sets `status` to anything but `active` -- so "live" is read from `last_seen`: a
scheme in the newest daily file, give or take a week of holidays. Of 3,382 live
funds, 1,863 have a Direct plan; the rest are Regular-only legacy schemes (fixed
term and interval series) that nothing new can be bought in.

**Direct plan only.** MODULE_2 §11.2 never ranks Direct and Regular together, and
a Direct plan is the one an investor choosing today would buy. Within it, the
Growth option before IDCW: IDCW returns are derived from the Growth series
(`derive/nav_adj.py`), so one share class per fund is the honest count.

One function for the history backfill, the peer groups and the public site, so
the three can never cover different funds (DECISIONS V1-75).
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

#: A scheme seen in AMFI's daily file within this many days of the newest one.
LIVE_WINDOW_DAYS = 7
ISIN = re.compile(r"IN[A-Z0-9]{10}")


@dataclass(frozen=True)
class Fund:
    scheme_id: str
    amfi_code: str | None
    name: str
    category: str
    plan: str
    option: str
    amc_id: str


def live_funds(conn: sqlite3.Connection) -> list[Fund]:
    """Every live fund with a Direct plan, as its Direct share class, by name."""
    latest = conn.execute("SELECT max(last_seen) FROM scheme").fetchone()[0]
    if latest is None:
        return []
    best: dict[str, tuple[tuple[bool, str], Fund]] = {}
    rows = conn.execute(
        "SELECT scheme_id, amfi_code, scheme_name, fund_name, plan, option,"
        " sebi_category, amc_id, scheme_family FROM scheme"
        " WHERE plan = 'direct' AND last_seen >= date(?, ?)",
        (str(latest), f"-{LIVE_WINDOW_DAYS} days"),
    )
    for sid, code, scheme_name, fund_name, plan, option, category, amc, family in rows:
        key = f"{amc}|{family or sid}"
        rank = (option != "growth", str(sid))
        if key not in best or rank < best[key][0]:
            best[key] = (rank, Fund(
                scheme_id=str(sid),
                amfi_code=str(code) if code else None,
                name=str(fund_name or scheme_name),
                category=str(category or "Other"),
                plan=str(plan),
                option=str(option),
                amc_id=str(amc),
            ))
    return sorted((fund for _, fund in best.values()), key=lambda f: f.name.lower())


__all__ = ["ISIN", "LIVE_WINDOW_DAYS", "Fund", "live_funds"]
