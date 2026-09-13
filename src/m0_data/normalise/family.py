"""Grouping a scheme's share classes. DECISIONS V1-37.

**A disclosure describes a scheme, not an ISIN.** Direct and Regular, Growth and
IDCW, are share classes of one pool of assets: they differ in what they charge
and what they pay out, never in what they hold. So one published portfolio is
the portfolio of every ISIN in its family, and that is definitional rather than
an approximation.

It matters because the wrong answer is the common one. Of 19,598 active schemes
the five disclosures this project holds cover **34 ISINs**, and it served five —
telling a Regular-plan holder `__NO_DISCLOSURE__` for a portfolio sitting in the
warehouse. Regular is what distributors sell.

### Why the key is built from segments

AMFI writes a share class as a suffix after a separator: `- Growth Option`,
`- Direct Plan`, `- IDCW Plan`, `- Reinvestment of Income Distribution cum
capital withdrawal option`. The base name never contains a separator AMFI did
not put there. So the rule is: split on separators, drop the segments made
**entirely** of share-class vocabulary, keep the rest.

Substring removal was tried first and is the trap, measured twice:

- Stripping the word `regular` merged `ICICI Prudential Regular Savings Fund`
  into `ICICI Prudential Savings Fund` — two different funds, one key.
- Removing each scheme's own verbatim `option_raw` split HDFC Flexi Cap in
  half, because AMFI writes `- Growth Option - Direct Plan` for the Direct
  class and `- Growth Plan` for the Regular one, while recording `Growth
  Option` as the raw option for both. Three ISINs of six, which is the bug this
  exists to fix rather than a fix for it.

The whole-segment condition is also what makes the vocabulary safe to extend.
`capital` had to be added for Kotak, whose IDCW classes read `Reinvestment of
Income Distribution cum capital withdrawal option` — and it cannot damage
`ICICI Prudential Capital Protection Oriented Fund`, because `Capital
Protection Oriented` is not made only of qualifiers and so is never dropped.

Measured over the whole universe: 19,598 active schemes group into 4,195
families, and **three** span more than one SEBI category. All three are AMFI
spelling the same category two ways for one fund, but a family that cannot be
shown coherent does not fan out at all — see `derive/scheme_family.py`.
"""

from __future__ import annotations

import re

#: Words that can only be describing a share class, never naming a fund.
#:
#: Read this together with the whole-segment rule in `family_key`: a word here
#: is dropped only when every other word in its segment is here too. `growth`
#: is in the list and `Nippon India Growth Mid Cap Fund` keeps its `Growth`,
#: because that segment also contains `mid`, `cap` and `fund`.
QUALIFIER = frozenset({
    "direct", "regular", "plan", "plans", "option", "options",
    "growth", "idcw", "dividend", "div", "payout", "reinvestment",
    "reinvest", "reinvested", "income", "distribution", "cum",
    "withdrawal", "wdrl", "capital", "bonus",
    "daily", "weekly", "fortnightly", "monthly", "quarterly",
    "half", "yearly", "annual", "discretionary",
    "institutional", "retail", "super", "unclaimed",
    "and", "of", "the",
})

#: AMFI's separators, including the en and em dashes it uses
#: inconsistently. Named by code point rather than typed, because an en
#: dash and a hyphen are indistinguishable in a source file and this one
#: decides where a scheme name ends.
DASHES = "-" + chr(0x2013) + chr(0x2014)
SEPARATORS = re.compile(r"\s*[" + DASHES + r"]\s*|\s*\(\s*|\s*\)\s*")


def family_key(scheme_name: str) -> str:
    """The scheme name with its share-class segments removed.

    Not unique on its own — pair it with `amc_id`, because two fund houses name
    funds alike (`HDFC Flexi Cap Fund` and `Kotak Flexi Cap Fund` share
    everything but the house).
    """
    parts = [
        part
        for part in SEPARATORS.split(re.sub(r"\s+", " ", scheme_name).strip())
        if part
    ]
    kept: list[str] = []
    for part in parts:
        words = [w for w in re.split(r"[^a-z0-9]+", part.lower()) if w]
        if words and all(w in QUALIFIER for w in words):
            continue
        kept.append(part)
    text = re.sub(r"[^a-z0-9]+", " ", " ".join(kept).lower())
    return re.sub(r"\s+", " ", text).strip()


__all__ = ["DASHES", "QUALIFIER", "SEPARATORS", "family_key"]
