"""A portfolio's look-through, computed and stored in one place.

What the portfolio views read -- exposure, the summary, fund overlap,
duplication, concentration and each fund's marginal contribution -- used to be
stored only by the terminal report `scripts.show_lookthrough`, so importing a
statement left every portfolio page empty (DECISIONS V1-74). `jobs.import_cas`
and that report now both come through here, and cannot store different things.

Positions arrive as arguments: M3 never reads M1's tables (invariant 3).
"""

from __future__ import annotations

import itertools
import sqlite3
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.types import SchemeId, UserId
from src.m3_lookthrough.concentration import concentration
from src.m3_lookthrough.duplication import Duplication, portfolio_duplication
from src.m3_lookthrough.engine import (
    IssuerWeight,
    LookThroughResult,
    Position,
    compute_lookthrough,
)
from src.m3_lookthrough.marginal import marginal_contribution
from src.m3_lookthrough.overlap import Overlap, pairwise_overlap
from src.m3_lookthrough.persist import save_lookthrough
from src.m3_lookthrough.persist_metrics import (
    SCOPES,
    save_concentration,
    save_duplication,
    save_marginal,
    save_overlap,
)
from src.m3_lookthrough.weights import rebuild_weights

Weights = dict[SchemeId, list[IssuerWeight]]


@dataclass(frozen=True)
class Analysis:
    as_of: date
    result: LookThroughResult
    pairs: list[Overlap]
    duplication: Duplication


def overlap_pairs(
    weights_by_scheme: Weights,
    as_of_by_scheme: dict[SchemeId, date],
    positions: list[Position],
) -> list[Overlap]:
    """§9.1. "How much of these two funds is the same thing?"

    Usually the launch story: a portfolio of five large-cap funds tends to be
    one fund bought five times. Keyed on issuer, never ISIN (§2.3), and
    synthetics are dropped so two funds both holding cash do not read as
    overlapping.

    Position values are passed through so §4.3's `overlap_value_inr` is computed
    — the same answer in rupees, which is the form the question is usually asked
    in. A scheme absent from `values` gives None rather than zero.
    """
    # Summed: one scheme in two folios is two positions.
    values: dict[SchemeId, Decimal] = {}
    for p in positions:
        values[p.scheme_id] = values.get(p.scheme_id, Decimal(0)) + p.value_inr
    # The funds HELD, not every one disclosed: for a real ledger the latter
    # stored ~15,000 pairs, which `--equal` hid by holding everything.
    schemes = sorted(s for s in values if s in weights_by_scheme)
    if len(schemes) < 2:
        return []
    pairs = [
        pairwise_overlap(
            a, b, as_of_by_scheme[a], as_of_by_scheme[b],
            weights_by_scheme[a], weights_by_scheme[b],
            value_a=values.get(a), value_b=values.get(b),
        )
        for a, b in itertools.combinations(schemes, 2)
    ]
    pairs.sort(key=lambda o: -o.overlap_pct)
    return pairs


def analyse(
    positions: list[Position],
    weights_by_scheme: Weights,
    as_of_by_scheme: dict[SchemeId, date],
    today: date | None = None,
) -> Analysis:
    """The look-through of `positions`, dated by what is HELD.

    The date is the newest disclosure behind a held fund: an unrelated fund's
    later file must not date this portfolio. With none behind any holding, today.
    """
    as_of = max(
        (as_of_by_scheme[p.scheme_id]
         for p in positions if p.scheme_id in as_of_by_scheme),
        default=today or date.today(),
    )
    result = compute_lookthrough(positions, weights_by_scheme, as_of)
    return Analysis(
        as_of=as_of,
        result=result,
        pairs=overlap_pairs(weights_by_scheme, as_of_by_scheme, positions),
        duplication=portfolio_duplication(
            result.contributions, result.summary.total_value_inr
        ),
    )


def store(
    ledger: sqlite3.Connection,
    user_id: UserId,
    analysis: Analysis,
    positions: list[Position],
    weights_by_scheme: Weights,
    as_of_by_scheme: dict[SchemeId, date],
) -> dict[str, int]:
    """Write everything the portfolio views read, replacing what was there.

    Only ever for a real ledger: an illustration written here would sit exactly
    where the portfolio's numbers belong, indistinguishable on the next read.
    """
    on = analysis.as_of
    result = analysis.result
    exposures = save_lookthrough(ledger, user_id, on, result, as_of_by_scheme)
    # §4.3, beside §4.2's tables and under the same rule: these are derived,
    # so a rebuild replaces the set rather than merging into it.
    scopes = save_concentration(
        ledger, user_id, on, [concentration(result.exposures, s) for s in SCOPES],
    )
    save_overlap(ledger, user_id, on, analysis.pairs)
    save_duplication(ledger, user_id, on, analysis.duplication)
    # §11: one more look-through per held fund, without it. §11.2 puts that
    # well under a second each at 5-15 funds.
    marginals = save_marginal(
        ledger, user_id, on,
        [
            marginal_contribution(positions, weights_by_scheme, on, s)
            for s in sorted({p.scheme_id for p in positions})
        ],
    )
    return {
        "exposures": exposures,
        "scopes": scopes,
        "pairs": len(analysis.pairs),
        "marginals": marginals,
    }


def refresh(
    warehouse: sqlite3.Connection,
    ledger: sqlite3.Connection,
    user_id: UserId,
    positions: list[Position],
) -> Analysis:
    """Rebuild the weights, then compute and store the look-through.

    The weights are a derived table in the warehouse (invariant 10), rebuilt
    unconditionally for the reason `rebuild_weights` gives.
    """
    weights, as_ofs = rebuild_weights(warehouse)
    analysis = analyse(positions, weights, as_ofs)
    store(ledger, user_id, analysis, positions, weights, as_ofs)
    return analysis
