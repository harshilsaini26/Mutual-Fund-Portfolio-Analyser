"""Pairwise fund overlap. MODULE_3.md §9.1.

"How much of these two funds is the same thing?" — the figure most likely to be
the launch story, because a portfolio of five large-cap funds usually turns out
to be one fund bought five times.

**Keyed on `issuer_id`, never on ISIN.** §2.3 is explicit that writing this
against `isin` *"produces a subtly understated answer — funds holding different
series of the same issuer's debt look non-overlapping"*. Worse in the common
case: one fund holds Reliance equity and another a Reliance NCD, which is the
same corporate exposure and must count as overlap.

**Synthetics are dropped before comparing.** Two unrelated funds both hold cash;
counting `__CASH__` would give every pair a floor of overlap and make the metric
useless at exactly the low end where it should reassure.

The measure is `Σ min(w_a, w_b)` over shared issuers: the share of each fund
that the *other* one also holds. Identical funds give 100, disjoint give 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.types import SchemeId
from src.m3_lookthrough.engine import IssuerWeight, is_synthetic

OVERLAP_Q = Decimal("0.000001")


@dataclass(frozen=True)
class Overlap:
    """§4.3. `aligned` is false when the two disclosures are from different dates.

    A soft comparison is still worth making — funds do not turn over completely
    in a month — but it must say so rather than presenting a same-date figure.
    """

    scheme_a: SchemeId
    scheme_b: SchemeId
    overlap_pct: Decimal
    overlap_equity_pct: Decimal
    common_issuers: int
    union_issuers: int
    jaccard: Decimal
    as_of_a: date
    as_of_b: date
    as_of_gap_days: int
    aligned: bool
    #: §4.3's `overlap_value_inr` — rupees held by both funds at once. `None`
    #: when either position's value is unknown, which is not the same as zero:
    #: zero would read as "nothing is duplicated".
    overlap_value_inr: Decimal | None = None


def pairwise_overlap(
    scheme_a: SchemeId,
    scheme_b: SchemeId,
    as_of_a: date,
    as_of_b: date,
    weights_a: list[IssuerWeight],
    weights_b: list[IssuerWeight],
    value_a: Decimal | None = None,
    value_b: Decimal | None = None,
) -> Overlap:
    """§9.1. Two funds' issuer weights in, one comparable figure out.

    `value_a` / `value_b` are the two positions' rupee values. Supply both and
    §4.3's `overlap_value_inr` is computed; omit either and it is `None`. They
    are optional because `--equal` and every caller written before this slice
    has weights without values, and because an unknown value must not become a
    zero rupee figure.
    """
    wa = {x.issuer_id: x.weight for x in weights_a if not is_synthetic(str(x.issuer_id))}
    wb = {x.issuer_id: x.weight for x in weights_b if not is_synthetic(str(x.issuer_id))}

    common = wa.keys() & wb.keys()
    union = wa.keys() | wb.keys()
    overlap = sum((min(wa[i], wb[i]) for i in common), Decimal(0))

    # §4.3's rupee figure, summed PER ISSUER rather than derived from
    # `overlap_pct`. The percentage is `Σ min(w_a, w_b)` in each fund's own
    # weights, so multiplying it by any single total answers a question about
    # neither fund. min(w_a·V_a, w_b·V_b) is the amount of that issuer both
    # funds hold simultaneously, which is what "duplicated in rupees" means.
    overlap_value = (
        sum(
            (
                min(wa[i] * value_a / 100, wb[i] * value_b / 100)
                for i in common
            ),
            Decimal(0),
        )
        if value_a is not None and value_b is not None
        else None
    )

    # The equity-only view renormalises within each fund's equity sleeve, so a
    # hybrid fund is not penalised for the part that is not equity at all.
    ea = {
        x.issuer_id: x.weight
        for x in weights_a
        if x.instrument_class == "equity" and not is_synthetic(str(x.issuer_id))
    }
    eb = {
        x.issuer_id: x.weight
        for x in weights_b
        if x.instrument_class == "equity" and not is_synthetic(str(x.issuer_id))
    }
    ta = sum(ea.values(), Decimal(0)) or Decimal(1)
    tb = sum(eb.values(), Decimal(0)) or Decimal(1)
    overlap_eq = sum(
        (min(ea[i] / ta, eb[i] / tb) for i in ea.keys() & eb.keys()), Decimal(0)
    ) * 100

    gap = abs((as_of_a - as_of_b).days)
    ordered = sorted((str(scheme_a), str(scheme_b)))
    forward = str(scheme_a) == ordered[0]

    return Overlap(
        scheme_a=SchemeId(ordered[0]),
        scheme_b=SchemeId(ordered[1]),
        overlap_pct=overlap.quantize(OVERLAP_Q),
        overlap_equity_pct=overlap_eq.quantize(OVERLAP_Q),
        common_issuers=len(common),
        union_issuers=len(union),
        jaccard=(
            Decimal(len(common)) / Decimal(len(union)) if union else Decimal(0)
        ),
        as_of_a=as_of_a if forward else as_of_b,
        as_of_b=as_of_b if forward else as_of_a,
        as_of_gap_days=gap,
        aligned=(gap == 0),
        overlap_value_inr=overlap_value,
    )
