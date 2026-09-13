"""Synthetic instrument rules. MODULE_0.md §8.4.

A disclosure is not all securities. Subtotals, cash, TREPS, receivables,
derivative margin and units of other schemes are a meaningful share of rows,
and without these rules they flood the review queue — which then stops being
reviewable, which is how a review queue dies.

These run FIRST in the cascade (§8.2 step 0), before ISIN. A row matching one
of them never reaches the queue.
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
        # across 532 security rows in four real disclosures **it has never
        # once matched**. AMCs do not name a holding the way the spec
        # imagined: the rows read `ICICI Prudential Gold ETF`, `Ishares
        # Nasdaq 100 UCITS ETF USD` and `Geninnov Global Master Fund`, and
        # not one of them says "units". V1-34, and the third outing for
        # this defect class — V1-04 on `Net Current Assets`, V1-30 on
        # `Government Securities`.
        #
        # An ETF and a UCITS are fund wrappers by definition, and a name
        # ENDING in `Fund` is a fund rather than a company that mentions
        # one. That last clause carries the risk, so it is anchored: `SBI
        # Funds Management Limited` is an operating company held by both
        # HDFC and ICICI, and this does not touch it — the plural defeats
        # the word boundary and the trailing words defeat the anchor.
        # Verified against every row of all four files, not reasoned about.
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
#: `20` + a serial. Code `00` is the Union; every other code is a state. So
#: `IN0020` is sovereign whatever the row is called, and no state loan can
#: match it.
#:
#: This exists because the vocabulary rule above misses the plainest possible
#: name. ICICI writes dated sovereign paper as exactly `Government Securities`,
#: which contains neither `government of india`, nor `goi`, nor a `g-sec`
#: token, nor `treasury bill` — so 1,837 Cr of it sat in `__UNRESOLVED__` while
#: the T-bills beside it, named `91 Days Treasury Bills`, resolved fine. That
#: is V1-04's lesson again: a pattern written against one AMC's wording is a
#: guess about every other AMC's.
#:
#: Verified on the file rather than assumed: all 17 central rows carry `IN0020`
#: — dated securities and treasury bills alike — and the eleven state segments
#: present (`IN1020` Andhra Pradesh through `IN4520` Telangana) carry none.
CENTRAL_GOVERNMENT_ISIN = re.compile(r"^IN0020")

#: §8.4's fallback: a row the parser already classified as cash or a derivative
#: is one of those even when its name says nothing recognisable.
CLASS_FALLBACK = {
    "cash": IssuerId("__CASH__"),
    "derivative": IssuerId("__DERIV__"),
    # Units of another scheme are not an issuer, and the loader already
    # works this out from the section heading — `Units of Mutual Fund` sits
    # over ICICI's Gold ETF. It was classed `mfunit` and still resolved to
    # `__UNRESOLVED__`, because this table stopped at cash and derivatives.
    # `__UNRESOLVED__` means "we could not identify this"; we had.
    #
    # REITs and InvITs are deliberately NOT here. They classify as `other`
    # and have real, identifiable issuers — the same line §8.4 draws for
    # state loans against sovereign paper (V1-30).
    "mfunit": IssuerId("__MFUNIT__"),
}


def match_synthetic(
    name: str,
    instrument_class: str | None = None,
    isin: str | None = None,
) -> IssuerId | None:
    """The synthetic bucket for this row, or None if it is a real security.

    §8.4's note is load-bearing and easy to lose: **only sovereign paper maps to
    `__GSEC__`.** State development loans and corporate bonds have identifiable
    issuers and must resolve to real ones — burying `7.26% Maharashtra SDL 2032`
    in a government bucket would hide a real state-government exposure, and
    burying an NCD there would hide corporate credit risk entirely.

    That note is why the ISIN check below is `IN0020` and not `IN` plus two
    digits. The wider pattern would have swept eleven state governments into
    the sovereign bucket in this one fund alone — 1,553 Cr across Maharashtra,
    Rajasthan, Madhya Pradesh and eight more — and each of those is a real
    exposure to a real borrower that someone might reasonably want to see.
    `isin` is checked BEFORE the name patterns: structure is harder evidence
    than wording, and the wording is what was wrong.
    """
    if isin and CENTRAL_GOVERNMENT_ISIN.match(isin.strip().upper()):
        return IssuerId("__GSEC__")
    for pattern, issuer_id in SYNTHETIC_RULES:
        if pattern.search(name):
            return issuer_id
    if instrument_class in CLASS_FALLBACK:
        return CLASS_FALLBACK[instrument_class]
    return None
