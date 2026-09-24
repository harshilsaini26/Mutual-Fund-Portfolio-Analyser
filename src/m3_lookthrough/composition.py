"""What one fund holds, summed for a picture of it. MODULE_3.md §12, one fund.

The portfolio views answer "what do I own through everything I hold". The fund
page asks the same question of one fund, so this is that sum over a portfolio of
one: the disclosure's normalised weights grouped by company, by asset class, by
the sector the fund house printed, and by AMFI size bucket through the same
`mcap_tilts` the portfolio's size profile uses.

Every figure is a share of the fund (percent). Negative rows stay in: net
payables are a real negative slice, and dropping them would push the others
past 100.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.types import ClassificationBasis, IssuerId
from src.m3_lookthrough.concentration import METRIC_Q
from src.m3_lookthrough.engine import IssuerWeight, is_synthetic
from src.m3_lookthrough.providers.lookthrough import Tilt
from src.m3_lookthrough.tilts import mcap_tilts

#: A holding the fund house printed no sector for. Named, never dropped.
NO_SECTOR = "Not stated"


@dataclass(frozen=True)
class CompositionRow:
    """One disclosed row: who, how much of the fund, what kind, which sector."""

    issuer_id: IssuerId
    weight: Decimal
    instrument_class: str
    sector: str | None


@dataclass(frozen=True)
class FundComposition:
    holdings: list[IssuerWeight]  # one per company, largest first
    by_class: list[tuple[str, Decimal]]  # share of the fund, largest first
    by_sector: list[tuple[str, Decimal]]  # share of the EQUITY, largest first
    size: list[Tilt]  # share of the equity, AMFI's bucket order


def composition(
    rows: list[CompositionRow],
    buckets: Mapping[IssuerId, str],
    basis: ClassificationBasis,
    mcap_basis: date,
) -> FundComposition:
    """Sorted here, in Python: weights are `DECIMAL_TEXT` (invariant 1, V1-18)."""
    per_issuer: dict[IssuerId, Decimal] = {}
    largest: dict[IssuerId, tuple[Decimal, str]] = {}
    per_class: dict[str, Decimal] = {}
    for r in rows:
        per_issuer[r.issuer_id] = per_issuer.get(r.issuer_id, Decimal(0)) + r.weight
        if r.issuer_id not in largest or r.weight > largest[r.issuer_id][0]:
            largest[r.issuer_id] = (r.weight, r.instrument_class)
        per_class[r.instrument_class] = (
            per_class.get(r.instrument_class, Decimal(0)) + r.weight
        )

    equity = [r for r in rows if r.instrument_class == "equity"]
    equity_total = sum((r.weight for r in equity), Decimal(0))
    per_sector: dict[str, Decimal] = {}
    for r in equity:
        key = r.sector or NO_SECTOR
        per_sector[key] = per_sector.get(key, Decimal(0)) + r.weight

    equity_by_issuer: dict[IssuerId, Decimal] = {}
    for r in equity:
        if not is_synthetic(str(r.issuer_id)):
            equity_by_issuer[r.issuer_id] = (
                equity_by_issuer.get(r.issuer_id, Decimal(0)) + r.weight
            )

    return FundComposition(
        holdings=sorted(
            (IssuerWeight(i, w, largest[i][1]) for i, w in per_issuer.items()),
            key=lambda h: (-h.weight, str(h.issuer_id)),
        ),
        by_class=sorted(per_class.items(), key=lambda kv: (-kv[1], kv[0])),
        by_sector=sorted(
            (
                (sector, (w * 100 / equity_total).quantize(METRIC_Q))
                for sector, w in per_sector.items()
            ),
            key=lambda kv: (-kv[1], kv[0]),
        )
        if equity_total > 0
        else [],
        size=mcap_tilts(equity_by_issuer, buckets, basis, mcap_basis),
    )


__all__ = ["NO_SECTOR", "CompositionRow", "FundComposition", "composition"]
