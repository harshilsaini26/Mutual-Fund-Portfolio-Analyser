"""Number and date coercion. MODULE_0.md §7.1 and §7.5.

M1 has an equivalent `to_decimal` in `src/m1_ledger/cas/parse.py`. The
duplication is deliberate: `CLAUDE.md` invariant 3 makes the dependency
direction one-way, M0 -> M1, so M0 cannot import from M1 — and MODULE_0.md §7.1
specifies its own coercion rather than deferring to M1's. The two differ where
their sources do: a CAS prints outflows in parentheses, AMFI prints `N.A.`.
"""

from __future__ import annotations

import re
from datetime import date
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
#: The characters AMCs hang footnotes on. `$` earns its place here rather
#: than being read as a currency: these are rupee disclosures, and Kotak
#: marks a written-off bond `0.00 $` while ICICI marks a covered call
#: `(Covered call) $$`.
FOOTNOTE_CHARS = "@*#^$~†‡"

#: A cell holding nothing BUT footnote punctuation.
FOOTNOTE_MARKER = re.compile(r"^[" + re.escape(FOOTNOTE_CHARS) + r"\s]+$")

#: The same characters clinging to a number, at either end.
FOOTNOTE_ATTACHED = re.compile(
    r"^[" + re.escape(FOOTNOTE_CHARS) + r"\s]+|["
    + re.escape(FOOTNOTE_CHARS) + r"\s]+$"
)


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
        # A footnote marker attached to a number is a footnote, not a defect.
        # Kotak writes a written-off bond as `0.00 $` and its weight as
        # `0.00 #` — YES BANK's AT1s, 428 units held and worth nothing, which
        # is a REAL holding at zero rather than a row to drop or raise on.
        #
        # Stripped only when what remains parses. That is the whole safety
        # argument: `#N/A` loses its `#` and is still not a number, so it still
        # raises, and so do `#DIV/0!`, `B.C.` and every other thing AMFI puts
        # in a numeric column. A marker on a number is the only case that
        # changes.
        bare = FOOTNOTE_ATTACHED.sub("", token).strip()
        if bare and bare != token:
            try:
                value = Decimal(bare)
            except InvalidOperation:
                raise CoercionError(f"cannot parse number: {s!r}") from exc
        else:
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


#: Month number by name, abbreviated and full, lowercased. Spelled out rather
#: than read with `%b`/`%B`, which go through `LC_TIME`: on a machine with a
#: German locale `strptime("31-Mar-2026", "%d-%b-%Y")` raises, so an archived
#: file that parses here would be refused there. Invariant 10 wants the same
#: bytes to rebuild the same warehouse on any machine, and a parser that reads
#: dates by geography cannot deliver that.
#:
#: `parse/index/nifty.py` keeps its own table for NSE's spelling, and the two
#: fetchers keep theirs for the other direction — the fetch layer imports
#: nothing from the rest of M0 so that every test in it runs offline.
MONTH_NUMBERS = {
    "jan": 1, "january": 1,
    "feb": 2, "february": 2,
    "mar": 3, "march": 3,
    "apr": 4, "april": 4,
    "may": 5,
    "jun": 6, "june": 6,
    "jul": 7, "july": 7,
    "aug": 8, "august": 8,
    "sep": 9, "september": 9,
    "oct": 10, "october": 10,
    "nov": 11, "november": 11,
    "dec": 12, "december": 12,
}

#: `31-Jan-2018`. The month is captured as letters and looked up, never handed
#: to `%b` — see `MONTH_NUMBERS`.
DDMMMYYYY = re.compile(r"^(\d{1,2})-([A-Za-z]+)-(\d{4})$")


def month_number(name: str) -> int | None:
    """`"Mar"` or `"March"` -> 3, case-insensitively and locale-independently.

    None for anything else, so a caller decides whether an unreadable month is
    a warning or a refusal — §6.3 rule 3 has parsers raise, §7.5 has some
    date probes fall through to the next candidate.
    """
    return MONTH_NUMBERS.get(name.strip().lower())


def to_date(s: str | None) -> date | None:
    """`DD-MMM-YYYY`, which is the only date format AMFI publishes. §7.5."""
    if s is None or s.strip().upper() in NULL_TOKENS:
        return None
    match = DDMMMYYYY.match(s.strip())
    month = month_number(match.group(2)) if match else None
    if match is None or month is None:
        raise CoercionError(f"cannot parse date: {s!r}")
    try:
        return date(int(match.group(3)), month, int(match.group(1)))
    except ValueError as exc:
        raise CoercionError(f"cannot parse date: {s!r}") from exc
