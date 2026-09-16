"""Caveat assembly. MODULE_6.md §6.

**One function, because the text must be identical across the product.** Two
paraphrases of one fact make a reader wonder which is right.

Caveats are SENTENCES, not codes. They render exactly as written and are never
truncated — if there are six, six appear; M6 may collapse them behind a "6 notes"
affordance, but the count stays visible.

Every string is **descriptive** (`PLAN.md` §3.3): "43 days old" is a fact,
"consider refreshing" is advice this product does not give.
`tests/unit/test_m6_discipline.py::test_no_prescriptive_language` lints it.

Where the honesty commitments made upstream become visible or quietly evaporate
(§19.2). Staleness, coverage and the unresolved share are computed upstream; if
they do not reach this list, computing them bought nothing.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Protocol

from src.m6_views.format import format_date, format_pct

#: §6. Thresholds above which a fact is worth saying out loud. Below them the
#: data is unremarkable and a caveat would be noise — the list has to stay short
#: enough that people read it.
STALENESS_WARN_DAYS = 45
UNRESOLVED_WARN_PCT = Decimal("2.0")
COVERAGE_WARN_PCT = Decimal("98.0")
RESIDUAL_WARN_PCT = Decimal("30.0")


class Quality(Protocol):
    """What `assemble_caveats` needs from a portfolio summary.

    Structural rather than a concrete type so M3's `PortfolioSummary` satisfies
    it without M6 importing M3's dataclass — §1.3's one-way dependency. Anything
    carrying these five fields works.

    Declared as read-only properties, not attributes: every summary in this
    project is a frozen dataclass, and mypy treats a Protocol's plain attributes
    as settable and therefore invariant, which rejects them. Nothing here writes,
    so read-only is also the accurate declaration.
    """

    @property
    def coverage_pct(self) -> Decimal: ...

    @property
    def unresolved_pct(self) -> Decimal: ...

    @property
    def worst_staleness_days(self) -> int | None: ...

    @property
    def confidence(self) -> str: ...

    @property
    def caveats(self) -> list[str]: ...


def dedupe_preserving_order(items: list[str]) -> list[str]:
    """§6.1 rule 4. Order matters — data-completeness first, methodology second
    — so a `set` is not available here. `dict` is: it has kept insertion
    order since 3.7, and this project needs 3.11."""
    return list(dict.fromkeys(items))


def assemble_caveats(
    *,
    quality: Quality | None = None,
    view_id: str = "",
    basis: str | None = None,
    truncated: bool = False,
    extra: list[str] | None = None,
    holdings_as_of: object = None,
) -> list[str]:
    """§6. The sentences this view must show, in §6.1's order.

    `quality.caveats` — the ones M3 already assembled and stored — are carried
    through **unchanged**. §3.2: *"if a caveat exists upstream and isn't in this
    list, that is a bug."* They come first because they are the most specific:
    M3 knows which scheme had no disclosure; M6 only knows that coverage is 94%.
    """
    out: list[str] = []

    if quality is not None:
        out.extend(quality.caveats)

        staleness = quality.worst_staleness_days
        if staleness is not None and staleness > STALENESS_WARN_DAYS:
            dated = (
                f"Holdings as of {format_date(holdings_as_of)} — "  # type: ignore[arg-type]
                if holdings_as_of is not None
                else "Holdings are "
            )
            out.append(
                f"{dated}{staleness} days old. Funds disclose monthly."
            )
        if quality.coverage_pct < COVERAGE_WARN_PCT:
            out.append(
                f"{format_pct(quality.coverage_pct)} of your portfolio has "
                f"current holdings data. The rest is shown as unclassified."
            )
        if quality.unresolved_pct > UNRESOLVED_WARN_PCT:
            out.append(
                f"{format_pct(quality.unresolved_pct)} of your exposure could "
                f"not be identified to a company; it is shown as a separate "
                f"block."
            )

    if basis == "drift_adj":
        out.append(
            "Weights are repriced to today. Disclosed weights are available "
            "as an alternative view."
        )

    if truncated:
        out.append(
            "Smaller holdings are grouped. Export the full list to see all rows."
        )

    out.extend(extra or [])
    return dedupe_preserving_order(out)


__all__ = [
    "COVERAGE_WARN_PCT",
    "RESIDUAL_WARN_PCT",
    "STALENESS_WARN_DAYS",
    "UNRESOLVED_WARN_PCT",
    "Quality",
    "assemble_caveats",
    "dedupe_preserving_order",
]
