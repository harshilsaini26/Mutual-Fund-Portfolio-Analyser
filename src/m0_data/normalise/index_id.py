"""Index identity, and matching a fund's benchmark text to it. S12.

Two different jobs, deliberately kept apart.

`index_id_for` mints the id from the PROVIDER's own name, which is the only
name that is authoritative and stable. `index_key` reduces any name -- a
provider's, a fund's, a disclosure row's -- to a comparison key, so that the
same index written six ways by six AMCs resolves to one index.

The six ways are real. Across the workbooks in this archive and the schemes in
this warehouse, one NSE index appears as:

    NIFTY MIDCAP 150 TRI          Nifty Midcap 150 (TRI)
    Nifty Midcap150 - TRI         NIFTY MIDCAP 150 Total Return Index
    Nifty Midcap 150 Index        NIFTY MIDCAP150

Every one of those is `NIFTY MIDCAP 150` at NSE. The key strips the TRI
marker, strips a trailing "INDEX", and then removes every non-alphanumeric --
so spacing, case, hyphens and brackets stop mattering all at once. Removing
separators wholesale is what makes `NIFTY MIDCAP150` and `NIFTY MIDCAP 150`
agree, and NSE's own catalogue contains names like `NIFTY500 MULTICAP 50:25:25`
where a rule that inserted spaces between letters and digits would do damage.

**The TRI marker is stripped from the key but recorded in the id.** §9.4 refuses
a price-return series for alpha, so `is_total_return` has to survive; what must
NOT survive into the matching key is whether a given AMC happened to write
"TRI" that day. A fund benchmarked to "Nifty 50" and one benchmarked to
"Nifty 50 TRI" are pointing at the same index, and it is our job to hold the
total-return series for it.

**Composites refuse rather than resolve.** One fund in this archive is
benchmarked to `85 % Nifty 500 TRI + 15% MSCI ACWI INFORMATION TECHNOLOGY INDEX
TRI`. Matching that to `NIFTY 500` because that is the leg we recognise would
produce an alpha measured against the wrong thing, silently and plausibly --
invariant 5's exact prohibition. `is_composite` sends it to the resolution
queue instead, where a person can decide.
"""

from __future__ import annotations

import re

#: Ways a source says "this is the total-return variant". Order matters: the
#: longest phrasings are removed first so a shorter one cannot strand a word.
TRI_MARKERS = (
    "total returns index",
    "total return index",
    "(total return index (tri))",
    "(tri)",
    " tri",
    "-tri",
)

#: A benchmark made of more than one index. Two forms appear: a weighted sum
#: ("85% X + 15% Y") and an unweighted conjunction, which AMCs write when the
#: scheme document does.
_COMPOSITE = re.compile(r"\+|\d\s*%\s*\w|\bplus\b(?!\s*aaa)", re.IGNORECASE)

_NON_ALNUM = re.compile(r"[^A-Z0-9]")

#: Provider prefixes for `index_id`. An index id carries its provider because
#: NSE and BSE both publish indices whose names differ by a word, and
#: `'NIFTY 50'` alone would eventually collide with something.
PROVIDERS = {"NSE Indices": "NSE", "BSE": "BSE"}


def is_composite(text: str) -> bool:
    """Is this benchmark made of more than one index?

    `Nifty SDL Plus AAA PSU Bond Jul 2028 60:40` is a real SINGLE index whose
    name contains "Plus", which is why the `plus` alternative refuses to fire
    in front of `AAA`. The `60:40` in that name is not a composite marker
    either -- a ratio without a percent sign is part of an index's name at NSE.
    """
    return bool(_COMPOSITE.search(text))


def strip_tri(text: str) -> tuple[str, bool]:
    """The name without its total-return marker, and whether one was there."""
    lowered = text.lower()
    found = False
    for marker in TRI_MARKERS:
        while marker in lowered:
            lowered = lowered.replace(marker, " ")
            found = True
    return lowered, found


def index_key(text: str) -> str:
    """A comparison key: case, spacing, punctuation and TRI markers removed.

    Not reversible and not meant to be. It exists so two spellings of one index
    compare equal, and nothing is ever displayed from it.
    """
    without_tri, _ = strip_tri(text)
    # Separators collapse BEFORE the trailing-"INDEX" rule, because stripping a
    # TRI marker leaves its brackets behind: `Nifty Alpha 50 Index (Total
    # Return Index (TRI))` becomes `nifty alpha 50 index ( )`, and a rule
    # anchored on the end of the string then never sees the INDEX it is for.
    # That one mismatch would have cost every Kotak index fund its benchmark,
    # since this is exactly how Kotak writes them.
    upper = re.sub(r"[^A-Z0-9]+", " ", without_tri.upper()).strip()
    # A trailing "INDEX" is decoration -- NSE's own catalogue says "NIFTY
    # MIDCAP 150", never "NIFTY MIDCAP 150 INDEX". Only trailing: "NIFTY INDIA
    # FPI 150" would lose a word to a blanket removal, and a CRISIL name like
    # `CRISIL-IBX AAA Financial Services Index - Sep 2027` carries it in the
    # middle. Repeated, because "... Index (Total Return Index)" leaves two.
    while (trimmed := re.sub(r"\bINDEX$", "", upper).strip()) != upper:
        upper = trimmed
    return _NON_ALNUM.sub("", upper)


def index_id_for(
    name: str, *, provider: str = "NSE Indices", is_total_return: bool = True
) -> str:
    """The stable id for a provider's index. `'Nifty 50'` -> `'NSE:NIFTY_50_TRI'`.

    Built from the provider's own spelling, never from a fund's, so the id does
    not change the day an AMC writes the name differently.
    """
    prefix = PROVIDERS.get(provider, provider.upper().replace(" ", "_"))
    base, _ = strip_tri(name)
    slug = re.sub(r"[^A-Z0-9]+", "_", base.upper()).strip("_")
    suffix = "TRI" if is_total_return else "PRI"
    return f"{prefix}:{slug}_{suffix}"
