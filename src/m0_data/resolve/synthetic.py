"""Synthetic instrument rules. MODULE_0.md §8.4.

A disclosure is not all securities. Subtotals, cash, TREPS, receivables,
derivative margin and units of other schemes are a meaningful share of rows, and
without these rules they flood the review queue — which then stops being
reviewable.

These run FIRST in the cascade (§8.2 step 0), before ISIN.
"""

from __future__ import annotations

import re

from src.common.types import IssuerId

#: §8.4, in order. First match wins.
SYNTHETIC_RULES: tuple[tuple[re.Pattern[str], IssuerId], ...] = tuple(
    (re.compile(pattern, re.I), IssuerId(issuer))
    for pattern, issuer in (
        (r"\btreps?\b|tri[- ]?party|reverse\s+repo|\brepo\b", "__TREPS__"),
        # §8.4 writes this as `other\s+(current\s+)?asset`, which requires the
        # word "other". HDFC discloses the line as `Net Current Assets`, so the
        # spec pattern misses it and a fund's working capital lands in
        # __UNRESOLVED__ — inflating the very metric (V3) that gates whether
        # the look-through may be shown at all. DECISIONS V1-04.
        (
            r"net\s+(receivable|payable|current\s+asset)|"
            r"other\s+(current\s+)?assets?|receivables?\s*/\s*\(?payables?",
            "__RECV__",
        ),
        (r"cash\s*(&|and)?\s*(bank|equivalent)|bank\s+balance", "__CASH__"),
        (r"margin|deposit\s+with|collateral", "__MARGIN__"),
        (r"\b(future|option|call|put)\b|\bfut\b|\bopt\b", "__DERIV__"),
        # §8.4 writes this as `units of ... fund` or `etf ... units`, and
        # across 532 security rows in four real disclosures it has never once
        # matched — AMCs write `ICICI Prudential Gold ETF` and `Ishares Nasdaq
        # 100 UCITS ETF USD`, and not one says "units" (V1-34).
        #
        # An ETF and a UCITS are fund wrappers by definition, and a name ENDING
        # in `Fund` is a fund rather than a company that mentions one. That last
        # clause is anchored: `SBI Funds Management Limited` is an operating
        # company and the plural defeats the word boundary.
        (
            r"units?\s+of\s+.*(fund|scheme)"
            r"|\betf\b"
            r"|\bucits\b"
            r"|\bfund\b\s*$",
            "__MFUNIT__",
        ),
        (
            r"government\s+of\s+india|\bgoi\b|\bg[- ]?sec\b|treasury\s+bill",
            "__GSEC__",
        ),
    )
)

#: Central government paper, by ISIN STRUCTURE rather than by name.
#:
#: An Indian government ISIN is `IN` + a two-digit issuing-government code +
#: `20` + a serial. Code `00` is the Union and every other code is a state, so
#: `IN0020` is sovereign whatever the row is called.
#:
#: The vocabulary rule above misses the plainest possible name: ICICI writes
#: dated sovereign paper as exactly `Government Securities`, so 1,837 Cr sat in
#: `__UNRESOLVED__` while the T-bills beside it resolved fine (V1-30).
#:
#: Verified on the file: all 17 central rows carry `IN0020`, and the eleven
#: state segments present (`IN1020` through `IN4520`) carry none.
CENTRAL_GOVERNMENT_ISIN = re.compile(r"^IN0020")

#: Units of a mutual fund, by ISIN structure. India's numbering agency
#: allocates `INE` to a company and **`INF` to a mutual fund**, so an INF ISIN
#: is units of a scheme whatever the row is called.
#:
#: V1-34's name rule cannot see most of them: `Kotak Arbitrage Fund Direct Plan
#: Growth` ends in `Growth`. 34 ISINs and Rs 17,063 Cr sat in `__UNRESOLVED__`
#: for want of a naming convention (V1-41).
MUTUAL_FUND_ISIN = re.compile(r"^INF")

#: Metal held as metal. Matched ONLY on a row carrying no ISIN, and that
#: condition is the whole rule rather than a refinement of it.
#:
#: These words are all over real company names — `Multi Commodity Exchange of
#: India`, `Goldman Sachs India Finance`, `Gold Circuit Electronics`, `Liquid
#: Gold Series` — and every one carries an ISIN. A bar of gold has none,
#: because nobody issued it, so the absence is the evidence and the name only
#: says which metal (V1-42).
COMMODITY_NAME = re.compile(r"\b(gold|silver|platinum|palladium|bullion)\b", re.I)

#: §8.4's fallback: a row the parser already classified as cash or a derivative
#: is one of those even when its name says nothing recognisable.
CLASS_FALLBACK = {
    "cash": IssuerId("__CASH__"),
    "derivative": IssuerId("__DERIV__"),
    # Units of another scheme are not an issuer, and the loader already works
    # this out from the section heading. Classed `mfunit`, such a row still
    # resolved to `__UNRESOLVED__` because this table stopped at cash and
    # derivatives — and `__UNRESOLVED__` means "we could not identify this".
    #
    # REITs and InvITs are deliberately NOT here: they classify as `other` and
    # have real issuers, the same line §8.4 draws for state loans (V1-30).
    "mfunit": IssuerId("__MFUNIT__"),
}


def match_synthetic(
    name: str,
    instrument_class: str | None = None,
    isin: str | None = None,
) -> IssuerId | None:
    """The synthetic bucket for this row, or None if it is a real security.

    §8.4's note is load-bearing: **only sovereign paper maps to `__GSEC__`.**
    State development loans and corporate bonds have identifiable issuers —
    burying `7.26% Maharashtra SDL 2032` in a government bucket hides a real
    state exposure, and an NCD there hides corporate credit risk.

    That is why the ISIN check is `IN0020` and not `IN` plus two digits: the
    wider pattern swept eleven state governments into the sovereign bucket in
    one fund, 1,553 Cr. `isin` is checked BEFORE the name patterns — structure
    is harder evidence than wording, and the wording is what was wrong.
    """
    if isin:
        code = isin.strip().upper()
        if CENTRAL_GOVERNMENT_ISIN.match(code):
            return IssuerId("__GSEC__")
        if MUTUAL_FUND_ISIN.match(code):
            return IssuerId("__MFUNIT__")
    for pattern, issuer_id in SYNTHETIC_RULES:
        if pattern.search(name):
            return issuer_id

    # Metal, and only after every name rule has had its say.
    #
    # Order is the rule. `ICICI Prudential Gold ETF` is a FUND holding gold, and
    # on a sheet omitting its ISIN the only thing separating it from a bar of
    # gold is that `etf` matched first.
    #
    # `not isin` is the other half: a row with an ISIN is somebody's security
    # however it is named, which keeps `Goldman Sachs India Finance` and
    # `Multi Commodity Exchange of India` out of here.
    #
    # Derivatives are excluded deliberately: a gold contract is not gold, and
    # the sheet's section heading already said derivative (V1-07).
    if not isin and instrument_class != "derivative" and COMMODITY_NAME.search(name):
        return IssuerId("__COMMODITY__")
    if instrument_class in CLASS_FALLBACK:
        return CLASS_FALLBACK[instrument_class]
    return None
