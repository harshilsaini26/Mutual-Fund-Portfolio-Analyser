"""Marginal contribution. MODULE_3.md §11.

Not "do these two funds overlap" but **"what does this fund add that I do not
already have?"** — §11.1 calls that the line that changes a decision, and it is
a different question from overlap: a fund can overlap heavily and still be the
only route to something.

Computed by difference. `compute_lookthrough` takes plain lists, so "without
this scheme" is a filter on `positions` and needs no second engine path — the
two runs go through identical code, which is what makes the delta meaningful
rather than an artefact of two implementations.

Synthetics are excluded from the issuer count for the same reason §8.2 excludes
them from concentration: a fund that is the portfolio's only route to TREPS has
not added an issuer anyone wanted.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal

from src.common.types import ExposureScope, IssuerId, SchemeId
from src.m3_lookthrough.concentration import concentration
from src.m3_lookthrough.engine import (
    Exposure,
    IssuerWeight,
    Position,
    compute_lookthrough,
)
from src.m3_lookthrough.providers.lookthrough import Marginal

#: §8.2's scope for the concentration comparison. Equity, because HHI over a
#: pool that includes cash and government paper measures the cash more than the
#: concentration.
SCOPE: ExposureScope = "equity"


def marginal_contribution(
    positions: list[Position],
    weights_by_scheme: dict[SchemeId, list[IssuerWeight]],
    as_of: date,
    scheme_id: SchemeId,
) -> Marginal:
    """What `scheme_id` adds to this portfolio that the rest does not.

    `style_shift_pp` and `fee_cost_inr` stay None: the first needs M2's style
    snapshot and the second a TER, and `WarehouseMarketDataProvider.ter` raises
    by design (V0-18). Both are already typed optional on `Marginal`, so this
    fills what exists rather than inventing the rest.
    """
    # Summed: one scheme in two folios is two positions.
    position_inr = sum(
        (p.value_inr for p in positions if p.scheme_id == scheme_id), Decimal(0)
    )
    if scheme_id not in weights_by_scheme:
        # No disclosure: the engine books it to __NO_DISCLOSURE__, so the
        # difference finds no new issuer. What it adds is unknown, not nothing.
        return Marginal(
            scheme_id=scheme_id, position_inr=position_inr, new_issuers=None,
            new_exposure_inr=None, new_exposure_pct=None, hhi_with=None,
            hhi_without=None, hhi_delta=None, effective_n_delta=None,
            style_shift_pp=None, fee_cost_inr=None,
        )

    full = compute_lookthrough(positions, weights_by_scheme, as_of)
    rest = [p for p in positions if p.scheme_id != scheme_id]
    without = compute_lookthrough(rest, weights_by_scheme, as_of)

    new_ids = _real(full.exposures) - _real(without.exposures)
    new_inr = sum(
        (e.exposure_inr for e in full.exposures if e.issuer_id in new_ids), Decimal(0)
    )
    total = full.summary.total_value_inr

    c_full = concentration(full.exposures, SCOPE)
    c_without = concentration(without.exposures, SCOPE)

    return Marginal(
        scheme_id=scheme_id,
        position_inr=position_inr,
        new_issuers=len(new_ids),
        new_exposure_inr=new_inr,
        new_exposure_pct=(new_inr / total * 100) if total else None,
        hhi_with=c_full.hhi,
        hhi_without=c_without.hhi,
        hhi_delta=_delta(c_full.hhi, c_without.hhi),
        effective_n_delta=_delta(c_full.effective_n, c_without.effective_n),
        style_shift_pp=None,
        fee_cost_inr=None,
    )


def _real(exposures: list[Exposure]) -> set[IssuerId]:
    """Issuers a person would recognise. Synthetics excluded for §8.2's reason:
    being the only route to TREPS is not adding something anyone wanted."""
    return {e.issuer_id for e in exposures if not e.is_synthetic}


def _delta(with_it: Decimal | None, without_it: Decimal | None) -> Decimal | None:
    """None when either side has no pool to measure, rather than a false zero.

    A portfolio of one fund has no `without` concentration at all, and
    reporting the delta as zero would say the fund changes nothing when in
    fact it is everything.
    """
    if with_it is None or without_it is None:
        return None
    return with_it - without_it
