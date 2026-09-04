"""`SchemeCharacteristics` — the ONLY M2 surface M3 consumes.

MODULE_2.md §14.1, transcribed. `PLAN.md` §8.2 rule 2: M3 reaches M2 only via
this protocol.

Slice Zero — Protocol stub and result dataclasses, no logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Protocol

from src.common.contracts.quality import DataQuality
from src.common.types import ManagerId, PeerGroupId, SchemeId, UserId


@dataclass(frozen=True)
class Window:
    """MODULE_2.md §3 — one analysis window.

    Three windows exist and standard platforms show only W_fund:

      W_fund     inception -> as_of                 "Is this a good fund?"   context only
      W_manager  tenure start -> tenure end/as_of   "Is this a good manager?" attributable
      W_user     first_purchase -> as_of            "Did I do well?"   the user's reality

    Showing a 10-year return beside a manager who arrived 14 months ago is the
    attribution failure in `PLAN.md` §1.1.
    """

    start: date
    end: date
    manager_id: ManagerId | None
    role: str | None


@dataclass(frozen=True)
class StyleSnapshot:
    """MODULE_2.md §6.1 / `scheme_style_snapshot`.

    `mcap_basis` is the audit trail: which AMFI list was applied. Stored so the
    number stays reproducible after AMFI revises the list, and so M3 can verify
    that every scheme it is summing used the same basis (§14.2).
    """

    scheme_id: SchemeId
    as_of_date: date
    mcap_basis: date
    unresolved_pct: Decimal

    pct_large: Decimal | None
    pct_mid: Decimal | None
    pct_small: Decimal | None
    pct_unclassified: Decimal | None

    pct_equity: Decimal | None
    pct_debt: Decimal | None
    pct_cash: Decimal | None
    pct_derivative: Decimal | None
    pct_mfunit: Decimal | None

    top5_weight: Decimal | None
    top10_weight: Decimal | None
    hhi: Decimal | None
    effective_n: Decimal | None

    holding_count: int | None
    equity_count: int | None
    sector_hhi: Decimal | None
    sector_count: int | None

    wtd_avg_mcap_inr: Decimal | None
    median_mcap_inr: Decimal | None


@dataclass(frozen=True)
class PeerContext:
    """MODULE_2.md §11 / `peer_group`.

    `plan` is never mixed: Direct and Regular differ by ~1%/yr of TER, so a
    blended peer group would rank a scheme against its own other share class.

    `PLAN.md` §9.9 is still open on `basis` — SEBI category (objective,
    heterogeneous) vs style clusters (honest, unstable). Leaning SEBI primary.
    """

    peer_group_id: PeerGroupId
    basis: str  # sebi_category|style_cluster
    plan: str
    as_of: date
    member_count: int
    coverage_pct: Decimal
    sebi_category: str | None
    universe_count: int | None
    percentile: Decimal | None
    rank: int | None


@dataclass(frozen=True)
class FeeDrag:
    """MODULE_2.md §12 / the fee block of `position_xray`.

    Fees in rupees, not basis points — "₹3,400/year" is the number a user can
    act on. `regular_penalty_inr` is None unless the position is in a Direct
    plan; it is what the Regular plan would have cost instead.
    """

    user_id: UserId
    scheme_id: SchemeId
    as_of: date
    fee_paid_inr: Decimal | None
    fee_pct_of_gain: Decimal | None
    blended_ter: Decimal | None
    regular_penalty_inr: Decimal | None


class SchemeCharacteristics(Protocol):
    """The ONLY M2 surface M3 consumes.

    M3 needs far less from M2 than it first appears: it aggregates *exposures*,
    which come from M0 holdings and M1 position values. What it needs from M2 is
    the quality and characterisation layer.

    What does NOT flow to M3: M2's return tables. Aggregate portfolio return is
    computed in M3 from M1's cashflows directly, never by averaging M2's
    per-scheme XIRRs — averaging XIRRs is mathematically wrong and would produce
    a number that does not tie to the ledger.
    """

    def style(
        self,
        scheme_id: SchemeId,
        as_of: date,
        mcap_basis: date | None = None,
    ) -> StyleSnapshot:
        """§14.2 — the shared-basis requirement.

        M3 picks one basis date (normally the latest AMFI list) and requests
        every snapshot on it. All schemes must use the same `mcap_basis` or
        incompatible classifications are being summed.
        """
        ...

    def quality(self, scheme_id: SchemeId, as_of: date) -> DataQuality:
        """§14.3 — the gating contract M3 keys its inclusion decisions off."""
        ...

    def peer_context(self, scheme_id: SchemeId) -> PeerContext | None: ...

    def return_correlation(
        self, scheme_a: SchemeId, scheme_b: SchemeId, months: int = 36
    ) -> Decimal | None: ...

    def fee_drag(
        self, user_id: UserId, scheme_id: SchemeId, as_of: date
    ) -> FeeDrag | None: ...
