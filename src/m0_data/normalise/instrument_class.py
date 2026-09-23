"""§4.6's `instrument_class`, in one place. DECISIONS V1-47.

This lived in `jobs/load_holdings.py` while there was one load path. V1-43 added
a second, and `jobs/fetch_groww.py` wrote its own copy rather than import a
module that does unrelated work at import time. **The copy disagreed**: a REIT
mapped to `equity` instead of `other`, and with no `mutual fund` branch a
fund-of-fund unit came out `other` instead of `mfunit`, so §10's nested
look-through could never find it.

The same fund loaded through the two tiers therefore reported different
`instrument_class` values for identical holdings — worse than the duplication it
came from, because nothing downstream can tell which tier a stored class came
from.

Moved here so both jobs can import it without importing each other. Nothing
about the rules changed in the move.
"""

from __future__ import annotations

#: From the SECTION the row sits under, most specific first.
#:
#: The section heading is the disclosure's own statement of what a block of
#: rows is, and it is the only reliable signal on the sheet. The instrument
#: name is not: HDFC's short leg is written `Eternal Limited`, identically to
#: the long position twelve rows above it, and only the `OPTIONS` heading says
#: one is a derivative. Getting that wrong fails §10's V8 — a negative market
#: value on something the loader called equity.
#:
#: **Order is load-bearing.** `derivative` precedes `cash` so `Repo Future`
#: reads as the contract it is, and `cash` precedes `debt` so `Cash Margin`
#: does not become a bond. Reordering these is a behaviour change.
#:
#: **An unmatched heading falls back to equity**, so every debt heading an AMC
#: prints has to be here. `Certificate of Deposit`, `Commercial Paper`,
#: `Non Convertible Debentures` and `Government Dated Securities` were not, and
#: 18.0% of all equity-classed weight turned out to be bonds, CPs and CDs.
CLASS_BY_SECTION: tuple[tuple[str, str], ...] = (
    ("option", "derivative"),
    ("future", "derivative"),
    ("derivativ", "derivative"),
    ("hedg", "derivative"),
    ("treps", "cash"),
    ("repo", "cash"),
    ("cash", "cash"),
    ("net current asset", "cash"),
    ("margin", "cash"),
    ("government", "debt"),
    ("treasury bill", "debt"),
    ("money market", "debt"),
    ("certificate of deposit", "debt"),
    ("commercial paper", "debt"),
    ("debenture", "debt"),
    ("floating rate", "debt"),
    ("debt", "debt"),
    ("bond", "debt"),
    ("reit", "other"),
    ("invit", "other"),
    ("infrastructure investment trust", "other"),
    ("preference share", "other"),
    ("alternative investment", "other"),
    ("mutual fund", "mfunit"),
    ("equity", "equity"),
    ("listed", "equity"),
)

#: Fallback when the sheet carried no usable heading. A row that resolved to a
#: synthetic issuer is what that issuer says it is.
CLASS_BY_ISSUER: dict[str, str] = {
    "__CASH__": "cash",
    "__TREPS__": "cash",
    "__RECV__": "cash",
    "__MARGIN__": "cash",
    "__DERIV__": "derivative",
    "__MFUNIT__": "mfunit",
    "__COMMODITY__": "other",
    "__GSEC__": "debt",
}


def class_from_section(section: str | None) -> str | None:
    """What the sheet's own heading says this row is, before anyone resolves it.

    Split from `instrument_class` because the cascade needs it and cannot wait:
    §8.4's `CLASS_FALLBACK` routes a row the parser already called cash or a
    derivative *"even when its name says nothing recognisable"* — and it was
    being handed `None`, so it never once fired. `TRP_030826` and
    `The Clearing Corporation of India Limited` both sit under a cash heading
    and both sat in `__UNRESOLVED__`; between them Rs 12,046 Cr (V1-42).

    Only the section half. The issuer half cannot run yet — it is the answer
    resolution is about to produce.
    """
    text = (section or "").lower()
    for needle, klass in CLASS_BY_SECTION:
        if needle in text:
            return klass
    return None


def instrument_class(section: str | None, issuer_id: str) -> str:
    """Section first, then the synthetic issuer, then equity.

    Except for cash. TREPS and net current assets usually trail a disclosure
    under no heading of their own, so they carry whichever heading came last —
    `Units of an Alternative Investment Fund`, `Treasury Bills` — and read as
    that. A row whose own name resolved to cash is cash, unless its heading is
    a derivative one: `Repo Future` resolves to `__TREPS__` by name and is
    still the contract.
    """
    from_section = class_from_section(section)
    from_issuer = CLASS_BY_ISSUER.get(issuer_id)
    if from_issuer == "cash" and from_section != "derivative":
        return "cash"
    if from_section is not None:
        return from_section
    return from_issuer or "equity"
