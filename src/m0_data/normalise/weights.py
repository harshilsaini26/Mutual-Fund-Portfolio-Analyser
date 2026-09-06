"""Weight normalisation. MODULE_0.md §7.3.

Disclosed `% to NAV` sums to roughly 98-102% — rounding, plus omitted lines.
Aggregating on the raw figure means **look-through exposure does not equal
portfolio value**, and the error differs per scheme, so two funds' exposures
are not even comparable to each other.

`pct_normalised` sums to exactly 100 and is what every aggregation uses.
`pct_to_nav` is kept beside it, unchanged, because a reader comparing our
number to the AMC's factsheet needs to see the figure the AMC published.
`weight_residual` — the slop that was adjusted away — goes on the disclosure
header so the adjustment is visible rather than silent.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

#: `PLAN.md` §7 V1: "pct_normalised sums to exactly 100 per scheme-date."
#: Exactly, not approximately — which is why the drift is dumped rather than
#: left to round.
TARGET = Decimal(100)

#: §4.6 stores `pct_normalised` as DECIMAL(9,6). Weights are quantised to that
#: BEFORE the drift is settled, and the order matters: a weight like
#: `101.0101010101010101010101010101010` already occupies the decimal context's
#: 34 significant digits, so adding a 1E-32 remainder to it rounds straight
#: back off and the sum never reaches 100. Quantising first makes the final
#: addition exact, and it stores what the column will hold anyway.
WEIGHT_Q = Decimal("0.000001")


class NormalisationError(ValueError):
    """Neither market values nor weights — nothing to normalise from."""


@dataclass(frozen=True)
class NormalisedWeights:
    weights: list[Decimal]
    residual: Decimal
    basis: str  # market_value|pct_to_nav
    total_market_value: Decimal


def normalise_weights(
    market_values: list[Decimal | None],
    pcts: list[Decimal | None],
) -> NormalisedWeights:
    """§7.3. Returns weights summing to exactly 100, plus the discarded residual.

    **Market value is preferred over the reported percentage.** The AMC rounds
    its percentage to two decimals; the market value is exact, so recomputing
    from it is more precise than renormalising a rounded figure — and it is the
    same quantity the fund actually holds.

    A negative position keeps a negative weight. A short leg is real exposure
    and forcing it positive would overstate the net; §10's V8 is what checks
    that only derivatives carry one.
    """
    if len(market_values) != len(pcts):
        raise NormalisationError("market values and percentages differ in length")
    if not market_values:
        raise NormalisationError("no rows to normalise")

    total_mv = sum((v for v in market_values if v is not None), Decimal(0))
    reported_sum = sum((p for p in pcts if p is not None), Decimal(0))

    if total_mv:
        weights = [
            (v / total_mv * TARGET) if v is not None else Decimal(0)
            for v in market_values
        ]
        basis = "market_value"
    else:
        if not reported_sum:
            raise NormalisationError("no market value and no weights")
        weights = [
            (p / reported_sum * TARGET) if p is not None else Decimal(0) for p in pcts
        ]
        basis = "pct_to_nav"

    # Quantise to the stored precision first — see WEIGHT_Q.
    weights = [w.quantize(WEIGHT_Q) for w in weights]

    # §7.3: force the sum exact by dumping the rounding drift into the largest
    # row. The drift is a few millionths; putting it anywhere else would be
    # equally arbitrary, and putting it in the LARGEST row makes it the
    # smallest possible relative distortion.
    drift = TARGET - sum(weights, Decimal(0))
    if weights and drift:
        largest = max(range(len(weights)), key=lambda i: abs(weights[i]))
        weights[largest] += drift

    return NormalisedWeights(
        weights=weights,
        residual=TARGET - reported_sum,
        basis=basis,
        total_market_value=total_mv,
    )
