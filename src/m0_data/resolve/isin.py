"""ISIN validation. MODULE_0.md §8.3.

"Reject malformed ISINs rather than creating garbage instruments." The AMFI NAV
file makes the case for itself: its ISIN columns contain the literal string
`Redeemed` on nine rows and `HDFCNIVODG` on one. Accepting those creates a
scheme keyed `Redeemed` that nine unrelated funds collapse into.

An ISIN is twelve characters — a two-letter country code, nine alphanumerics
and a numeric check digit — and the check digit is what separates a real ISIN
from a plausible-looking string.
"""

from __future__ import annotations

import re

ISIN_SHAPE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")


def isin_check_digit(body: str) -> int:
    """Luhn over the digit-expanded body. Letters expand to A=10 .. Z=35.

    Expansion happens before the Luhn pass, not during it: `A` becomes the two
    digits `1` and `0`, and both take part in the alternating doubling. Doing
    it the other way round produces a different digit for any ISIN containing
    a letter, which is most of them.
    """
    digits = "".join(
        str(int(ch, 36)) if ch.isalpha() else ch for ch in body.upper()
    )
    total = 0
    # Double every second digit counting from the RIGHT of the body, because
    # the check digit itself occupies the rightmost position.
    for index, ch in enumerate(reversed(digits)):
        value = int(ch)
        if index % 2 == 0:
            value *= 2
            if value > 9:
                value -= 9
        total += value
    return (10 - total % 10) % 10


def is_valid_isin(value: str | None) -> bool:
    """Shape and check digit. §8.3."""
    if not value:
        return False
    text = value.strip().upper()
    if not ISIN_SHAPE.fullmatch(text):
        return False
    return isin_check_digit(text[:-1]) == int(text[-1])
