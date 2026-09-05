"""Number and date coercion. MODULE_0.md §7.1 and §7.5.

M1 has an equivalent `to_decimal` in `src/m1_ledger/cas/parse.py`. The
duplication is deliberate: `CLAUDE.md` invariant 3 makes the dependency
direction one-way, M0 -> M1, so M0 cannot import from M1 — and MODULE_0.md §7.1
specifies its own coercion rather than deferring to M1's. The two differ where
their sources do: a CAS prints outflows in parentheses, AMFI prints `N.A.`.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

#: §7.1. Indian digit grouping is 2-2-3, so naive comma-stripping is correct
#: and locale-aware parsing is not — `locale.atof` reads `1,23,456.78` wrongly
#: when it reads it at all.
INDIAN_NUM = re.compile(r"^\(?-?[\d,]+\.?\d*\)?$")

#: Values that mean "no number here", not "zero". A NAV of 0 and a NAV that was
#: not published are different facts and must not collapse.
NULL_TOKENS = frozenset({"", "-", "--", "N.A.", "NA", "N/A", "NIL", "NIL.", "NULL"})


class CoercionError(ValueError):
    """A value that looks like a number but cannot be parsed. §7.1 raises."""


def to_decimal(s: str | None) -> Decimal | None:
    """Parse one printed number, or None where the source printed nothing."""
    if s is None:
        return None
    token = s.strip()
    if token.upper() in NULL_TOKENS:
        return None
    negative = token.startswith("(") and token.endswith(")")
    token = token.strip("()").replace(",", "").replace("\u20b9", "").strip()
    if not token:
        return None
    try:
        value = Decimal(token)
    except InvalidOperation as exc:
        raise CoercionError(f"cannot parse number: {s!r}") from exc
    return -value if negative else value


def to_date(s: str | None) -> date | None:
    """`DD-MMM-YYYY`, which is the only date format AMFI publishes. §7.5."""
    if s is None or s.strip().upper() in NULL_TOKENS:
        return None
    try:
        return datetime.strptime(s.strip(), "%d-%b-%Y").date()
    except ValueError as exc:
        raise CoercionError(f"cannot parse date: {s!r}") from exc
