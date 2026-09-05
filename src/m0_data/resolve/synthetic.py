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
        (r"net\s+receivable|net\s+payable|other\s+(current\s+)?asset", "__RECV__"),
        (r"cash\s*(&|and)?\s*(bank|equivalent)|bank\s+balance", "__CASH__"),
        (r"margin|deposit\s+with|collateral", "__MARGIN__"),
        (r"\b(future|option|call|put)\b|\bfut\b|\bopt\b", "__DERIV__"),
        (r"units?\s+of\s+.*(fund|scheme)|\betf\b.*units?", "__MFUNIT__"),
        (
            r"government\s+of\s+india|\bgoi\b|\bg[- ]?sec\b|treasury\s+bill",
            "__GSEC__",
        ),
    )
)

#: §8.4's fallback: a row the parser already classified as cash or a derivative
#: is one of those even when its name says nothing recognisable.
CLASS_FALLBACK = {
    "cash": IssuerId("__CASH__"),
    "derivative": IssuerId("__DERIV__"),
}


def match_synthetic(name: str, instrument_class: str | None = None) -> IssuerId | None:
    """The synthetic bucket for this row, or None if it is a real security.

    §8.4's note is load-bearing and easy to lose: **only sovereign paper maps to
    `__GSEC__`.** State development loans and corporate bonds have identifiable
    issuers and must resolve to real ones — burying `7.26% Maharashtra SDL 2032`
    in a government bucket would hide a real state-government exposure, and
    burying an NCD there would hide corporate credit risk entirely.
    """
    for pattern, issuer_id in SYNTHETIC_RULES:
        if pattern.search(name):
            return issuer_id
    if instrument_class in CLASS_FALLBACK:
        return CLASS_FALLBACK[instrument_class]
    return None
