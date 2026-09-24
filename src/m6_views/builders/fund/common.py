"""What the fund page's views share: window tabs, labels, and series packing.

No figure is made here. Every number arrives from M2 or M3 already computed;
this turns it into what a chart needs (a position and a label the reader sees)
and what the table and CSV need (the raw value). The browser positions points
with the number and shows the label, so it never formats a figure itself
(MODULE_6.md §16.4).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any
from urllib.parse import quote

from src.m6_views.aggregate import lttb_indices
from src.m6_views.format import format_inr

#: The growth chart's periods, in tab order. "max" is every price on record.
WINDOWS = (("1y", "1 year"), ("3y", "3 years"), ("5y", "5 years"), ("max", "All"))
DEFAULT_WINDOW = "5y"

#: The words a reader knows for each `instrument_class`.
CLASS_NAMES = {
    "equity": "Shares",
    "debt": "Bonds and other debt",
    "cash": "Cash and equivalents",
    "derivative": "Derivatives",
    "mfunit": "Other funds",
    "other": "REITs, InvITs and other",
}

#: The same, as they read inside a sentence.
CLASS_PHRASES = {
    "equity": "shares",
    "debt": "bonds and other debt",
    "cash": "cash and equivalents",
    "derivative": "derivatives",
    "mfunit": "other funds",
    "other": "REITs, InvITs and other",
}

SIZE_NAMES = {
    "large": "Large companies",
    "mid": "Mid-sized companies",
    "small": "Small companies",
    "unranked": "Not ranked by AMFI",
}


#: What a fund page says where the public copy leaves the benchmark out. The
#: public copy's market data withholds index levels (`index_levels_withheld`),
#: because NSE licenses them for personal use (DECISIONS V1-72); "no benchmark
#: on record" would be untrue there, so the builders say this instead.
INDEX_WITHHELD = (
    "Benchmark comparisons are left out of this public copy: the index data is "
    "licensed for personal use. The self-hosted app shows them."
)


def withholds_index(market: Any) -> bool:
    return bool(getattr(market, "index_levels_withheld", False))


#: The empty state for a fund page opened with no fund.
NO_FUND = (
    "No fund chosen. Search for one by name at the top of the page, or open "
    "one from your holdings."
)


@dataclass(frozen=True)
class FundQuality:
    """`caveats.Quality` for one fund. Coverage and unresolved share are
    look-through properties, so None ("not applicable") unless the view is
    about the fund's own portfolio."""

    worst_staleness_days: int
    confidence: str
    caveats: list[str] = field(default_factory=list)
    coverage_pct: Decimal | None = None
    unresolved_pct: Decimal | None = None


def window_of(params: dict[str, Any]) -> str:
    key = str(params.get("window") or DEFAULT_WINDOW)
    return key if key in dict(WINDOWS) else DEFAULT_WINDOW


def tabs(view_id: str, scheme_id: str, active: str) -> list[dict[str, Any]]:
    """One link per period. Without JavaScript each is an ordinary link to the
    page; with it, `fragment` swaps just this panel."""
    sid = quote(scheme_id, safe="")
    return [
        {
            "key": key,
            "label": label,
            "href": f"/fund/{sid}?window={key}",
            "fragment": f"/fragment/{view_id}?scope_id={sid}&window={key}",
            "active": key == active,
        }
        for key, label in WINDOWS
    ]


def rupees(v: Decimal | None) -> str:
    """Whole rupees: paise on a chart of Rs 10,000 growing are noise."""
    return format_inr(v, precision=0, compact=False)


def thin(
    rows: Sequence[tuple[date, Decimal, Decimal | None]],
) -> list[tuple[date, Decimal, Decimal | None]]:
    """Downsample on the first series and keep the SAME dates for the second,
    so the two lines stay comparable point for point (§11.3)."""
    return [rows[i] for i in lttb_indices([r[1] for r in rows])]


def points(
    rows: Sequence[tuple[date, Decimal | None]], label: Any
) -> list[list[str]]:
    """`[iso date, number, label]` per point, skipping days with no value."""
    return [[d.isoformat(), str(v), label(v)] for d, v in rows if v is not None]


__all__ = [
    "CLASS_NAMES",
    "CLASS_PHRASES",
    "DEFAULT_WINDOW",
    "INDEX_WITHHELD",
    "NO_FUND",
    "SIZE_NAMES",
    "WINDOWS",
    "FundQuality",
    "points",
    "rupees",
    "tabs",
    "thin",
    "window_of",
    "withholds_index",
]
