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

#: A cell holding nothing but footnote punctuation. Portfolio disclosures put
#: these in numeric columns to reference a note — HDFC's Flexi Cap sheet marks
#: a negative position with a bare `@` in the `% to NAV` column.
#:
#: This is "see the note", not a malformed number, so it coerces to None rather
#: than raising. `12.3.4` still raises: that is a number someone got wrong, and
#: §7.1 is right that it must surface.
FOOTNOTE_MARKER = re.compile(r"^[@*#^$~†‡\s]+$")


class CoercionError(ValueError):
    """A value that looks like a number but cannot be parsed. §7.1 raises."""


def to_decimal(s: str | None) -> Decimal | None:
    """Parse one printed number, or None where the source printed nothing."""
    if s is None:
        return None
    token = s.strip()
    if token.upper() in NULL_TOKENS:
        return None
    if FOOTNOTE_MARKER.match(token):
        return None
    negative = token.startswith("(") and token.endswith(")")
    token = token.strip("()").replace(",", "").replace("\u20b9", "").strip()
    if not token:
        return None
    try:
        value = Decimal(token)
    except InvalidOperation as exc:
        raise CoercionError(f"cannot parse number: {s!r}") from exc
    if not value.is_finite():
        # `Decimal` accepts "Infinity", "-Infinity" and "NaN" happily, and this
        # is the money path. AMFI's NAV column is known to carry `#N/A`,
        # `#DIV/0!` and `B.C.` — all of which raise above — but a spreadsheet
        # that has already divided by zero can print `Infinity` too, and that
        # one parsed and propagated.
        #
        # NaN is the worse of the two because it is QUIET: every comparison
        # against it is False, so a `nav <= 0` guard waves it through and a
        # weight built from it poisons a portfolio without raising anywhere.
        # Observed defect list: captn3m0/historical-mf-data documents what AMFI
        # actually ships in this column.
        raise CoercionError(f"not a finite number: {s!r}")
    return -value if negative else value


def to_date(s: str | None) -> date | None:
    """`DD-MMM-YYYY`, which is the only date format AMFI publishes. §7.5."""
    if s is None or s.strip().upper() in NULL_TOKENS:
        return None
    try:
        return datetime.strptime(s.strip(), "%d-%b-%Y").date()
    except ValueError as exc:
        raise CoercionError(f"cannot parse date: {s!r}") from exc
