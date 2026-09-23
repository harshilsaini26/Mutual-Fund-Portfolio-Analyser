"""Size profile: equity exposure by AMFI market-cap bucket. MODULE_3.md §12.

The one tilt dimension the data supports: sector and industry need M5's
taxonomy (V1-03). The caller picks ONE AMFI list for the whole aggregation —
§12.2 rule 3, because summing buckets from two lists sums incompatible things —
and this only adds up. No benchmark column: that needs index constituents,
which are not loaded, so `benchmark_pct` and `active_tilt_pp` stay None.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date
from decimal import Decimal

from src.common.types import ClassificationBasis, IssuerId
from src.m3_lookthrough.concentration import METRIC_Q
from src.m3_lookthrough.providers.lookthrough import Tilt

#: AMFI's buckets in reading order: the top 100 companies by average market
#: cap, the next 150, the rest.
BUCKETS = ("large", "mid", "small")

#: Equity AMFI's list does not rank: foreign listings, and anything not listed
#: in India. Kept as its own row, never folded into "small".
UNRANKED = "unranked"


def mcap_tilts(
    equity: Mapping[IssuerId, Decimal],
    buckets: Mapping[IssuerId, str],
    basis: ClassificationBasis,
    mcap_basis: date,
) -> list[Tilt]:
    """Every bucket, in reading order, as a share of equity exposure.

    A bucket value outside `BUCKETS` raises (KeyError): a list parsed into a
    fourth size class is a parser defect, not a new kind of company.
    """
    total = sum(equity.values(), Decimal(0))
    if total <= 0:
        return []
    by_bucket = dict.fromkeys((*BUCKETS, UNRANKED), Decimal(0))
    for issuer_id, inr in equity.items():
        by_bucket[buckets.get(issuer_id, UNRANKED)] += inr
    ranked = total - by_bucket[UNRANKED]
    return [
        Tilt(
            dimension="mcap",
            dimension_value=bucket,
            exposure_inr=inr,
            exposure_pct=(inr * 100 / total).quantize(METRIC_Q),
            classification_basis=basis,
            coverage_pct=(ranked * 100 / total).quantize(METRIC_Q),
            benchmark_pct=None,
            active_tilt_pp=None,
            benchmark_index_id=None,
            mcap_basis=mcap_basis,
        )
        for bucket, inr in by_bucket.items()
    ]
