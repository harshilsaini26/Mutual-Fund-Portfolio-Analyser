"""The look-through engine. MODULE_3.md §2, §5.

The product's actual claim: what companies do I own, through the funds I hold?

**Closure is the whole discipline.** §2.2:

    Σ_i exposure_inr(i)  ==  Σ_s position_value(s)

and it *"must hold exactly (to rounding)"*. §2.2 names the three ways it breaks
and each has a defence here — raw `pct_to_nav` instead of `pct_normalised`
(refused by `assert_weights_sum_to_100`), unresolved holdings dropped by a join
(`__UNRESOLVED__` is carried through and surfaced), and a scheme with no
disclosure contributing silently zero (`__NO_DISCLOSURE__`).

A look-through that quietly under-reports is worse than none: the user concludes
they are less concentrated than they are, which is the opposite of what this
exists to tell them.

**This module is a pure function on plain data.** No SQL, no database handle —
`CLAUDE.md` invariant 3 keeps module boundaries behind interfaces, and invariant
1's second clause forbids aggregating `DECIMAL_TEXT` in SQL anyway. Every sum
here is a Python `Decimal`. `scripts/show_lookthrough.py` does the wiring.

**Scope, deliberately small.** No prices, so no drift-adjusted basis (§3.2) and
`weight_basis` is always `disclosed`. No `direct_holding` (§7). Both are
additions to this shape rather than changes to it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from src.common.types import IssuerId, SchemeId

#: §5.2. One rupee, absolute, on a portfolio-level total.
CLOSURE_TOL = Decimal("1.00")

#: §5.2. Weights are stored to six decimals (`MODULE_0.md` §4.6), so a hundredth
#: of a percent is already two orders of magnitude looser than the data.
WEIGHT_TOL = Decimal("0.01")

#: Percentages are reported at the precision the weights are stored to.
PCT_Q = Decimal("0.000001")

#: §5.3 rule 4 and §5.4: above this share, the gap gets said out loud.
UNRESOLVED_CAVEAT_PCT = Decimal(2)

#: §5.4. A held scheme whose disclosure we do not have.
NO_DISCLOSURE = IssuerId("__NO_DISCLOSURE__")
UNRESOLVED = IssuerId("__UNRESOLVED__")


class ClosureViolation(RuntimeError):
    """§2.2 broke. Rows were dropped or double-counted; do not publish the result."""


class WeightsNotNormalised(ValueError):
    """§5.2. A scheme's weights do not sum to 100, so M0's normalisation failed."""

    def __init__(self, scheme_id: SchemeId, as_of: date, total: Decimal) -> None:
        super().__init__(
            f"{scheme_id} at {as_of}: weights sum to {total}, not 100. "
            f"The bug is in MODULE_0 §7.3's normalise_weights, not in M3."
        )


def is_synthetic(issuer_id: str) -> bool:
    """§2.4's own helper.

    §2.4 adds: *"Always filter via `issuer.is_synthetic`, never by string prefix
    in SQL — the flag is authoritative."* That warning is about SQL, where the
    prefix and the flag can drift apart. This module never sees the issuer
    table; the caller resolves the flag when it has one, and this is the
    fallback §2.4 itself writes.
    """
    return issuer_id.startswith("__") and issuer_id.endswith("__")


@dataclass(frozen=True)
class Position:
    """One held scheme and what it is worth. `units > 0` is the caller's filter."""

    scheme_id: SchemeId
    value_inr: Decimal


@dataclass(frozen=True)
class IssuerWeight:
    """One issuer's normalised share of one scheme. Percent, summing to 100."""

    issuer_id: IssuerId
    weight: Decimal
    instrument_class: str


@dataclass(frozen=True)
class Exposure:
    """§4.2. What the user owns of one issuer, through everything they hold."""

    issuer_id: IssuerId
    exposure_inr: Decimal
    pct_of_portfolio: Decimal
    instrument_class: str
    is_synthetic: bool
    fund_count: int


@dataclass(frozen=True)
class Contribution:
    """Which scheme contributed how much of an issuer. The audit trail for §2.1."""

    scheme_id: SchemeId
    issuer_id: IssuerId
    exposure_inr: Decimal


@dataclass(frozen=True)
class PortfolioSummary:
    """§5.4's coverage, and §5.3's unresolved share."""

    total_value_inr: Decimal
    covered_value_inr: Decimal
    coverage_pct: Decimal
    unresolved_pct: Decimal
    issuer_count: int


@dataclass(frozen=True)
class LookThroughResult:
    """§5.1."""

    exposures: list[Exposure]
    contributions: list[Contribution]
    summary: PortfolioSummary
    caveats: list[str] = field(default_factory=list)


def assert_closure(
    got: Decimal, expected: Decimal, permitted_drift: Decimal = Decimal(0)
) -> None:
    """§5.2. *"Run this on every computation, not just in tests."*

    Takes the computed total rather than the exposure list, so it is callable
    from anywhere a total exists and cannot be accidentally passed a filtered
    list — which would make it pass while proving nothing.

    **`permitted_drift` reconciles two tolerances that otherwise contradict.**
    §5.2 gives closure ±₹1 absolute and gives the weight check ±0.01
    percentage points. Weights that are off by 0.01 move a position's exposure
    by `value x 0.0001`, so on anything above ₹10,000 the weight guard accepts
    data that the closure guard then refuses: 99.995% on a ₹1 crore position
    sums ₹500 short and raised `ClosureViolation` for a discrepancy the
    previous line had explicitly allowed.

    So the caller passes the drift it actually tolerated, and closure is exact
    to ₹1 *beyond* that. When weights sum to exactly 100 — which is what
    `MODULE_0.md` §7.3 guarantees and what every real disclosure produces — the
    drift is zero and the tolerance is ₹1 unchanged. Nothing is loosened for
    correct data; the slack is only ever as wide as the slack already granted.
    """
    tolerance = CLOSURE_TOL + abs(permitted_drift)
    if abs(got - expected) > tolerance:
        raise ClosureViolation(
            f"look-through sums to {got}, expected {expected}, "
            f"delta {got - expected} (tolerance {tolerance})"
        )


def assert_weights_sum_to_100(
    weights: list[IssuerWeight], scheme_id: SchemeId, as_of: date
) -> Decimal:
    """§5.2. If this fires, the bug is upstream in M0. Returns the actual sum.

    The sum is returned rather than discarded so the caller can tell
    `assert_closure` how much drift it just tolerated — see that function.
    """
    total = sum((x.weight for x in weights), Decimal(0))
    if abs(total - Decimal(100)) > WEIGHT_TOL:
        raise WeightsNotNormalised(scheme_id, as_of, total)
    return total


def compute_lookthrough(
    positions: list[Position],
    weights_by_scheme: dict[SchemeId, list[IssuerWeight]],
    as_of: date,
) -> LookThroughResult:
    """§5.1. Positions and per-scheme issuer weights in, exposures out.

    Closure is asserted before returning, so a caller cannot receive a result
    that does not add up.
    """
    live = [p for p in positions if p.value_inr > 0]
    # `CLAUDE.md` invariant 4: never silently drop rows. A position can arrive
    # non-positive when its scheme had no NAV on the valuation date, and
    # dropping it quietly leaves a portfolio that looks whole while missing a
    # fund — `coverage_pct` cannot show it either, since it is computed against
    # a total that already excludes it.
    unvalued = [p.scheme_id for p in positions if p.value_inr <= 0]
    total_value = sum((p.value_inr for p in live), Decimal(0))

    #: How far the weights were allowed to miss 100, in rupees. See assert_closure.
    permitted_drift = Decimal(0)
    exposure_inr: dict[IssuerId, Decimal] = defaultdict(Decimal)
    funds: dict[IssuerId, set[SchemeId]] = defaultdict(set)
    largest: dict[IssuerId, tuple[Decimal, str]] = {}
    contributions: list[Contribution] = []
    covered_value = Decimal(0)
    caveats: list[str] = []
    undisclosed: list[SchemeId] = []

    for position in live:
        weights = weights_by_scheme.get(position.scheme_id)
        if not weights:
            # §5.4. Its full value goes to a visible bucket rather than nowhere.
            undisclosed.append(position.scheme_id)
            exposure_inr[NO_DISCLOSURE] += position.value_inr
            funds[NO_DISCLOSURE].add(position.scheme_id)
            largest.setdefault(NO_DISCLOSURE, (Decimal(0), "unknown"))
            contributions.append(
                Contribution(position.scheme_id, NO_DISCLOSURE, position.value_inr)
            )
            continue

        weight_sum = assert_weights_sum_to_100(weights, position.scheme_id, as_of)
        permitted_drift += (
            position.value_inr * abs(Decimal(100) - weight_sum) / Decimal(100)
        )
        covered_value += position.value_inr

        for weight in weights:
            value = position.value_inr * weight.weight / Decimal(100)
            exposure_inr[weight.issuer_id] += value
            funds[weight.issuer_id].add(position.scheme_id)
            # The portfolio-level class is the one from the biggest contribution:
            # an issuer held as equity by a large fund and as debt by a small one
            # is an equity exposure to a reader, and §2.3 aggregates them anyway.
            if value >= largest.get(weight.issuer_id, (Decimal(-1), ""))[0]:
                largest[weight.issuer_id] = (value, weight.instrument_class)
            contributions.append(
                Contribution(position.scheme_id, weight.issuer_id, value)
            )

    exposures = [
        Exposure(
            issuer_id=issuer_id,
            exposure_inr=value,
            pct_of_portfolio=(
                (value / total_value * 100).quantize(PCT_Q)
                if total_value
                else Decimal(0)
            ),
            instrument_class=largest.get(issuer_id, (Decimal(0), "unknown"))[1],
            is_synthetic=is_synthetic(str(issuer_id)),
            fund_count=len(funds[issuer_id]),
        )
        for issuer_id, value in sorted(
            exposure_inr.items(), key=lambda kv: (-kv[1], str(kv[0]))
        )
    ]

    # §5.2, before anything is returned.
    assert_closure(
        sum((e.exposure_inr for e in exposures), Decimal(0)),
        total_value,
        permitted_drift,
    )

    unresolved_pct = (
        (exposure_inr.get(UNRESOLVED, Decimal(0)) / total_value * 100).quantize(PCT_Q)
        if total_value
        else Decimal(0)
    )
    if unresolved_pct > UNRESOLVED_CAVEAT_PCT:
        caveats.append(
            f"{unresolved_pct}% of the portfolio sits in {UNRESOLVED}: holdings "
            f"whose issuer could not be identified. Every exposure below is "
            f"understated by up to that much."
        )
    if unvalued:
        caveats.append(
            f"{len(unvalued)} held scheme(s) had no value on {as_of} and are "
            f"absent from every figure below, coverage included "
            f"({', '.join(str(s) for s in sorted(unvalued))})"
        )
    if undisclosed:
        caveats.append(
            f"no disclosure for {len(undisclosed)} held scheme(s) "
            f"({', '.join(str(s) for s in sorted(undisclosed))}); "
            f"their value is shown as {NO_DISCLOSURE}"
        )

    return LookThroughResult(
        exposures=exposures,
        contributions=contributions,
        summary=PortfolioSummary(
            total_value_inr=total_value,
            covered_value_inr=covered_value,
            coverage_pct=(
                (covered_value / total_value * 100).quantize(PCT_Q)
                if total_value
                else Decimal(0)
            ),
            unresolved_pct=unresolved_pct,
            issuer_count=len([e for e in exposures if not e.is_synthetic]),
        ),
        caveats=caveats,
    )
