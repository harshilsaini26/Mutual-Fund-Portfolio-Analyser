"""Issuer, instrument, holding and scheme-resolution types.

Shared by M0, M2, M3, M5. Slice Zero — dataclasses only, no logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal

from src.common.types import (
    Confidence,
    InstrumentClass,
    Isin,
    IssuerId,
    SchemeId,
    SourceFileId,
)


@dataclass(frozen=True)
class SchemeRef:
    """MODULE_0.md §11.2 — the result of scheme resolution.

    `scheme_id is None` means unresolvable; M1 quarantines rather than guessing.
    `matched_by == 'name_fuzzy'` is always low confidence and always flagged:
    resolving on name when an ISIN is present is the path to Direct/Regular
    confusion and a silent ~1%/year error.
    """

    scheme_id: SchemeId | None
    plan: str
    option: str
    confidence: Confidence
    matched_by: str  # isin|amfi_code|name_fuzzy|none
    merged_from: SchemeId | None


@dataclass(frozen=True)
class MergerLink:
    """MODULE_0.md §11.2.

    The ratio is a fraction, kept as two ints so no precision is lost before it
    is applied. `MODULE_1.md`: rescale `grandfathered_nav` by it — that is a
    per-unit figure.
    """

    predecessor_scheme_id: SchemeId
    successor_scheme_id: SchemeId
    merger_date: date
    ratio_num: int
    ratio_den: int


@dataclass(frozen=True)
class Holding:
    """MODULE_0.md `holding` (line 518). One disclosed instrument row.

    Referenced by `FundDataProvider.holdings()` and `MarketDataFeed.holdings()`;
    the corpus names it in both but defines it in neither, so it is defined once
    here (DECISIONS: the 47 undefined contract types).

    `issuer_id` is NOT NULL by construction — unresolved rows carry the
    `__UNRESOLVED__` synthetic issuer rather than being dropped (`PLAN.md` §4.10).

    Aggregate on `pct_normalised`, never `pct_to_nav`: the latter is as-reported
    and sums to 98-102%, so look-through would not equal portfolio value.
    """

    scheme_id: SchemeId
    as_of_date: date
    row_number: int
    issuer_id: IssuerId
    instrument_raw_name: str
    market_value: Decimal
    pct_normalised: Decimal
    instrument_class: InstrumentClass
    resolution_method: str  # isin|alias|fuzzy|rule|synthetic|unresolved
    isin: Isin | None
    quantity: Decimal | None
    pct_to_nav: Decimal | None
    credit_rating: str | None
    reported_sector: str | None
    yield_pct: Decimal | None
    resolution_conf: Decimal | None
    revision: int
    source_file_id: SourceFileId


@dataclass(frozen=True)
class IssuerWeight:
    """MODULE_0.md §9.3 `scheme_issuer_weight`.

    Collapses multi-row holdings to one row per issuer. Join on `issuer_id`,
    never `isin` — a Reliance equity line and a Reliance NCD are one exposure.

    Both bases are stored so M6 can show the user how much staleness costs them
    for their specific portfolio. `weight_drift_adj` is None when the issuer had
    no priceable ISIN or quantity.
    """

    scheme_id: SchemeId
    as_of_date: date
    issuer_id: IssuerId
    weight_disclosed: Decimal
    instrument_class: InstrumentClass
    weight_drift_adj: Decimal | None
    quantity: Decimal | None
