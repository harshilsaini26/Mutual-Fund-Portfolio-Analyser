"""Market value -> rupees absolute. MODULE_0.md §7.2.

**This is the 100x-error path.** Most AMCs report market value in lakhs, some
in crores, a few absolute. The header text is the only signal, and it is free
text: `Market/ Fair Value (Rs. in Lacs.)`, `Market Value (Rs. in Crores)`,
`Amount (Rs.)`. Getting it wrong scales an entire portfolio by 100 and nothing
downstream notices, because the weights still sum to 100 — only the absolute
values are wrong, and they are wrong consistently.

So: **the unit is read from the header, and an unreadable header raises.**
§7.2 says so and it is the one place in this module where guessing is worst.

The sanity net is §10's V2 — `sum(market_value)` against the scheme's AUM
within 3%. A units error fails that by two orders of magnitude, which is why V2
quarantines rather than warns.
"""

from __future__ import annotations

import re
from decimal import Decimal

#: §7.2 verbatim.
UNIT_MULTIPLIER: dict[str, Decimal] = {
    "absolute": Decimal(1),
    "thousand": Decimal(1_000),
    "lakh": Decimal(100_000),
    "crore": Decimal(10_000_000),
}

#: Header spellings seen in the wild, most specific first. `lacs` and `lakhs`
#: are the same word; `mn`/`million` appear in a few foreign-feeder sheets.
_UNIT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bcr(?:s|ore|ores)?\b\.?", re.I), "crore"),
    (re.compile(r"\bla(?:c|k|kh|khs|cs|cs\.)?\b\.?", re.I), "lakh"),
    (re.compile(r"\bmn\b|\bmillion\b", re.I), "lakh"),  # 1 mn = 10 lakh; see below
    (re.compile(r"\b(?:in\s+)?thousands?\b|\b000s?\b", re.I), "thousand"),
)

#: A header that names rupees and no scale means absolute — `Market Value (Rs.)`
#: is a complete statement, not an ambiguous one.
_ABSOLUTE = re.compile(r"\b(?:rs|inr|rupees?)\b\.?\s*\)?\s*$", re.I)


class AmbiguousUnitError(ValueError):
    """§7.2: the parser must raise rather than guess. The 100x path."""


def unit_from_header(header: str) -> str:
    """Read the scale out of a market-value column header.

    Raises rather than defaulting. `absolute` is returned only when the header
    positively says rupees with no scale — never as a fallback, because a
    header we failed to understand is far more likely to be an unusual scale
    than a plain one.
    """
    text = (header or "").strip()
    if not text:
        raise AmbiguousUnitError("empty market-value header; cannot determine unit")

    # `Rs. in Million` contains both "mn" and nothing else; a million is ten
    # lakh, so it is not expressible in the four multipliers §7.2 defines.
    if re.search(r"\bmn\b|\bmillion\b", text, re.I):
        raise AmbiguousUnitError(
            f"header states millions, which §7.2 has no multiplier for: {text!r}"
        )

    for pattern, unit in _UNIT_PATTERNS:
        if unit == "lakh" and pattern is _UNIT_PATTERNS[2][0]:
            continue
        if pattern.search(text):
            return unit

    if _ABSOLUTE.search(text):
        return "absolute"

    raise AmbiguousUnitError(f"cannot determine unit from header: {text!r}")


def to_inr(raw: Decimal, unit: str) -> Decimal:
    """§7.2. Scale a reported figure to rupees absolute."""
    try:
        return raw * UNIT_MULTIPLIER[unit]
    except KeyError:
        raise AmbiguousUnitError(f"unknown unit {unit!r}") from None
