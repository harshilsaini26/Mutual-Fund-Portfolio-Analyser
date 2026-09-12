"""Concentration metrics. MODULE_3.md §8.

HHI, effective-N, top-N and Gini over look-through exposures — "am I actually
diversified?", which is the question the product exists to answer.

**§8.2 is the part that decides whether the answer is true.** Synthetic issuers
— cash, TREPS, receivables, margin, unresolved — are excluded from the
denominator **in every scope**. Including `__CASH__` and `__TREPS__` inflates
effective-N and understates concentration, *"precisely the number the product
exists to surface"*. A fund that is 20% cash would otherwise look meaningfully
more diversified than it is.

Every sum is a Python `Decimal` (`CLAUDE.md` invariant 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from src.common.types import ExposureScope, IssuerId
from src.m3_lookthrough.engine import Exposure

#: HHI and Gini are ratios; six decimals is far finer than the input weights.
METRIC_Q = Decimal("0.000001")


@dataclass(frozen=True)
class Concentration:
    """§4.3. `hhi` and `effective_n` are None when the pool is empty."""

    scope: ExposureScope
    issuer_count: int
    hhi: Decimal | None = None
    effective_n: Decimal | None = None
    top1_pct: Decimal | None = None
    top5_pct: Decimal | None = None
    top10_pct: Decimal | None = None
    top20_pct: Decimal | None = None
    gini: Decimal | None = None
    largest_issuer_id: IssuerId | None = None


def filter_scope(exposures: list[Exposure], scope: str) -> list[Exposure]:
    """§8.2. Synthetics never enter a concentration denominator, in any scope."""
    real = [e for e in exposures if not e.is_synthetic]
    if scope == "all":
        return real
    if scope in ("equity", "debt"):
        return [e for e in real if e.instrument_class == scope]
    raise ValueError(f"unknown exposure scope {scope!r}")


def gini_coefficient(weights: list[Decimal]) -> Decimal | None:
    """§8.3. More intuitive than HHI for a lay reader; feeds M6's Lorenz curve.

    **`None` when any weight is negative.** Gini summarises a Lorenz curve, and
    that construction assumes a non-negative pool: with a short leg the
    cumulative share is not monotonic, the "curve" crosses its own diagonal, and
    the formula below still returns a perfectly ordinary-looking number between
    -1 and 1 that describes nothing. V1-07 records that HDFC discloses exactly
    this — Eternal Limited's short at -0.001% — so an issuer's net exposure
    going negative is a real case, not a hypothetical.

    An absent figure is the honest output. §4.3 types the column nullable, and
    M6 renders a null as an em dash (§9.3) rather than as zero. V1-20 deferred
    this decision to §4.3's persistence; this is that slice.

    Zero still means zero: an empty pool, or one that is entirely worthless, has
    no inequality to measure, which is a different statement from "the question
    does not apply".
    """
    if any(x < 0 for x in weights):
        return None
    ascending = sorted(weights)
    n = len(ascending)
    total = sum(ascending, Decimal(0))
    if n == 0 or total == 0:
        return Decimal(0)
    cumulative = sum(
        ((Decimal(i + 1) * x) for i, x in enumerate(ascending)), Decimal(0)
    )
    return ((2 * cumulative) / (Decimal(n) * total) - Decimal(n + 1) / Decimal(n))


def concentration(exposures: list[Exposure], scope: ExposureScope) -> Concentration:
    """§8.1. Over the scoped pool, renormalised within it."""
    pool = sorted(filter_scope(exposures, scope), key=lambda e: -e.exposure_inr)
    total = sum((e.exposure_inr for e in pool), Decimal(0))
    if not pool or total <= 0:
        return Concentration(scope=scope, issuer_count=0)

    weights = [e.exposure_inr / total for e in pool]
    hhi = sum((x * x for x in weights), Decimal(0))
    gini = gini_coefficient(weights)

    def top(n: int) -> Decimal:
        # Saturates rather than overrunning: `top10_pct` of a three-issuer
        # portfolio is 100, and slicing already does the right thing.
        return (sum(weights[:n], Decimal(0)) * 100).quantize(METRIC_Q)

    return Concentration(
        scope=scope,
        issuer_count=len(pool),
        hhi=hhi.quantize(METRIC_Q),
        effective_n=(Decimal(1) / hhi).quantize(METRIC_Q) if hhi > 0 else None,
        top1_pct=top(1),
        top5_pct=top(5),
        top10_pct=top(10),
        top20_pct=top(20),
        gini=gini.quantize(METRIC_Q) if gini is not None else None,
        largest_issuer_id=pool[0].issuer_id,
    )
