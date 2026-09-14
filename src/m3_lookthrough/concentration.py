"""Concentration metrics. MODULE_3.md §8.

HHI, effective-N, top-N and Gini over look-through exposures — "am I actually
diversified?", which is the question the product exists to answer.

**§8.2 decides whether the answer is true.** Synthetic issuers — cash, TREPS,
receivables, margin, unresolved — are excluded from the denominator in EVERY
scope: including them inflates effective-N and understates concentration, so a
fund that is 20% cash would look meaningfully more diversified than it is.

Every sum is a Python `Decimal` (invariant 1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Protocol, TypeVar

from src.common.types import ExposureScope, IssuerId
from src.m3_lookthrough.engine import Exposure

#: HHI and Gini are ratios; six decimals is far finer than the input weights.
METRIC_Q = Decimal("0.000001")


class Exposed(Protocol):
    """The three fields a concentration figure needs from an exposure.

    There are two `Exposure` dataclasses — the engine's and the frozen contract
    M4 and M6 consume (V1-21). Both carry these three, so typing against the
    SHAPE lets this be called from either side without a conversion, and V1-21
    recorded that the provider is the only place the two meet.

    Properties rather than attributes: mypy treats a Protocol's plain attributes
    as invariant, so `instrument_class: str | None` would reject the engine's
    `str`. Read-only members are covariant, and nothing here writes.
    """

    @property
    def exposure_inr(self) -> Decimal: ...

    @property
    def instrument_class(self) -> str | None: ...

    @property
    def is_synthetic(self) -> bool: ...


E = TypeVar("E", bound=Exposed)


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


def filter_scope(exposures: Sequence[E], scope: str) -> list[E]:
    """§8.2. Synthetics never enter a concentration denominator, in any scope."""
    real = [e for e in exposures if not e.is_synthetic]
    if scope == "all":
        return real
    if scope in ("equity", "debt"):
        return [e for e in real if e.instrument_class == scope]
    raise ValueError(f"unknown exposure scope {scope!r}")


def gini_coefficient(weights: list[Decimal]) -> Decimal | None:
    """§8.3. More intuitive than HHI for a lay reader; feeds M6's Lorenz curve.

    **`None` when any weight is negative.** Gini summarises a Lorenz curve,
    which assumes a non-negative pool: with a short leg the cumulative share is
    not monotonic, the curve crosses its own diagonal, and the formula still
    returns an ordinary-looking number that describes nothing. Real, not
    hypothetical — HDFC discloses a short at -0.001% (V1-07).

    An absent figure is the honest output: §4.3 types the column nullable and M6
    renders a null as an em dash rather than as zero.

    Zero still means zero — an empty pool has no inequality to measure, which is
    a different statement from "the question does not apply".
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


def lorenz_points(
    exposures: Sequence[Exposed], scope: ExposureScope = "all"
) -> list[tuple[Decimal, Decimal]]:
    """§8.3's Lorenz curve: the shape `gini_coefficient` summarises to one number.

    Returns `(cumulative share of issuers, cumulative share of exposure)`,
    smallest first, starting at the origin and ending at (1, 1). The diagonal is
    perfect equality; the further the curve sags below it, the more concentrated
    the portfolio.

    **Built smallest-first, and that is not a presentation choice.** Ordering
    from the largest draws the mirror image — a curve arcing *above* the
    diagonal, which is a perfectly plausible-looking chart that inverts the
    meaning.

    **This lives in M3, not in M6's builder.** `MODULE_6.md` §2.1 forbids the
    view layer from deriving a number and gives `value / total * 100` as its
    example of the mistake; each point here is a cumulative share of a
    cumulative share. A number computed in a view is a second source of truth
    nobody can reconcile against the first.

    Returns `[]` for a signed pool, for the same reason `gini_coefficient`
    returns None (V1-21): with a negative exposure the cumulative share is not
    monotonic and the curve crosses its own diagonal. An absent curve says the
    question does not apply; a drawn one would not.
    """
    pool = sorted(filter_scope(exposures, scope), key=lambda e: e.exposure_inr)
    total = sum((e.exposure_inr for e in pool), Decimal(0))
    if not pool or total <= 0 or any(e.exposure_inr < 0 for e in pool):
        return []

    n = Decimal(len(pool))
    points = [(Decimal(0), Decimal(0))]
    running = Decimal(0)
    for index, exposure in enumerate(pool, start=1):
        running += exposure.exposure_inr
        points.append((Decimal(index) / n, running / total))
    return points
