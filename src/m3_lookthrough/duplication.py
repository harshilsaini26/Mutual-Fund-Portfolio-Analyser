"""Portfolio-level duplication. MODULE_3.md §9.4.

Pairwise overlap answers *"are these two funds similar?"*. This answers the
question the user actually has: **"how much of my money is doubled up?"**

The whole module turns on one definition. For an issuer reached through several
funds, the redundant part is `Σ contributions - largest contribution` — §9.4:
*"the exposure you would still have if you kept only the largest provider of that
issuer"*. Counting the issuer's **whole** exposure instead is the obvious
mistake and it roughly doubles the reported figure, in the alarming direction.

§9.4 is explicit that this is **descriptive, not a recommendation to
consolidate** (`PLAN.md` §3.3). It states what is true about the portfolio. What
to do about it is the user's call, and two funds holding the same company is
often deliberate.

Aggregated in Python with `Decimal` — `CLAUDE.md` invariant 1 forbids `SUM()`
over a `DECIMAL_TEXT` column, and `lookthrough_contribution.exposure_inr` is one.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from src.common.types import IssuerId, SchemeId
from src.m3_lookthrough.engine import Contribution, is_synthetic

#: §7's directly-held equity. Excluded because owning a share yourself and
#: owning it inside a fund is not one fund duplicating another — there is no
#: second fee and no second manager. `direct_holding` is not built yet, so
#: nothing produces this today; the guard is here because §9.4 specifies it and
#: because the alternative is a silent behaviour change when §7 lands.
DIRECT = SchemeId("__DIRECT__")

PCT_Q = Decimal("0.000001")


@dataclass(frozen=True)
class Duplication:
    """§4.3's `portfolio_duplication`.

    One of the 47 result types the corpus names but never defines (SZ-01..12's
    family), so the shape is taken from §9.4's own return statement.
    """

    duplicated_inr: Decimal
    duplicated_pct: Decimal
    issuers_multi_fund: int
    max_funds_per_issuer: int


def portfolio_duplication(
    contributions: list[Contribution], total_value_inr: Decimal
) -> Duplication:
    """§9.4. How much of the portfolio is the same company bought twice."""
    by_issuer: dict[IssuerId, list[Contribution]] = defaultdict(list)
    for contribution in contributions:
        if contribution.scheme_id == DIRECT:
            continue
        if is_synthetic(str(contribution.issuer_id)):
            # Every fund holds cash and most have something unresolved. Counting
            # the synthetics would put a floor under this figure for every
            # portfolio ever built, at exactly the low end where a small number
            # is the reassuring answer.
            continue
        by_issuer[contribution.issuer_id].append(contribution)

    duplicated = Decimal(0)
    multi_fund = 0
    max_funds = 0

    for rows in by_issuer.values():
        # Keyed on the scheme, not the row: two contribution rows from one fund
        # are one fund. Nothing produces those today, but `depth` will once
        # fund-of-funds recursion (§6) lands.
        per_fund: dict[SchemeId, Decimal] = defaultdict(Decimal)
        for row in rows:
            per_fund[row.scheme_id] += row.exposure_inr

        max_funds = max(max_funds, len(per_fund))
        if len(per_fund) > 1:
            multi_fund += 1
            duplicated += sum(per_fund.values(), Decimal(0)) - max(per_fund.values())

    return Duplication(
        duplicated_inr=duplicated,
        duplicated_pct=(
            (duplicated / total_value_inr * 100).quantize(PCT_Q)
            if total_value_inr
            else Decimal(0)
        ),
        issuers_multi_fund=multi_fund,
        max_funds_per_issuer=max_funds,
    )


__all__ = ["DIRECT", "Duplication", "portfolio_duplication"]
