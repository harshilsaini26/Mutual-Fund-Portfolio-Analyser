"""`LookThroughDataProvider` — M3's view of M0 and M1.

MODULE_3.md §15.4, transcribed. One amendment:

  D4  `portfolio_cashflows(..., scope)` is typed `CashflowScope`, not `str`.
      Per `PLAN.md` §9.6 this parameter is the switch-leg flag: switch legs are
      included at scheme level and excluded at portfolio level as internal
      transfers. It shares the literal values 'scheme'/'portfolio' with M4's
      unrelated `AnalysisScope`, which is exactly the confusion §9.6 warns will
      "read as a bug".

Slice Zero — Protocol stub, no implementation. `FakeLookThroughDataProvider`
reading YAML fixtures ships alongside it, so M3's tests run with no database.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from datetime import date
from decimal import Decimal
from typing import Protocol

from src.common.contracts.entity import DirectHolding, IssuerWeight
from src.common.contracts.market import PricePoint
from src.common.types import CashflowScope, Isin, IssuerId, SchemeId, UserId
from src.m1_ledger.handoff import PositionContext


class LookThroughDataProvider(Protocol):
    """M3's only route to M0 and M1. No direct SQL across either boundary."""

    # --- from M0 -----------------------------------------------------------

    def scheme_issuer_weights(
        self, scheme_id: SchemeId, as_of: date, basis: str
    ) -> tuple[list[IssuerWeight], str]:
        """Returns the weights and the basis actually used.

        The second element may differ from the requested `basis`: drift
        adjustment needs priceable quantities, and falls back to disclosed
        weights when they are missing. M3 surfaces the fallback as a caveat
        rather than silently returning a different number than was asked for.
        """
        ...

    def issuer_of(self, isin: Isin) -> IssuerId | None: ...

    def issuer_name(self, issuer_id: IssuerId) -> str: ...

    def class_of(self, issuer_id: IssuerId) -> str | None: ...

    def price(self, isin: Isin, on: date) -> PricePoint | None: ...

    def classification(
        self,
        issuer_id: IssuerId,
        dimension: str,
        on: date,
        mcap_basis: date | None,
    ) -> str | None: ...

    def amfi_mcap_effective_date(self, on: date) -> date:
        """Which half-yearly AMFI list is in force on `on`.

        M3 calls this once and applies the answer to every scheme — one
        `mcap_basis` across a single aggregation, or incompatible buckets are
        being summed.
        """
        ...

    def benchmark_weights(self, dimension: str, on: date) -> dict[str, Decimal]:
        """Requires `index_constituent` — `BUILD_ORDER.md` R4."""
        ...

    def resolve_underlying_scheme(
        self, isin: Isin | None, raw_name: str
    ) -> SchemeId | None:
        """For fund-of-funds recursion: an `mfunit` holding to a scheme_id."""
        ...

    def scheme_name(self, scheme_id: SchemeId) -> str: ...

    def ter(self, scheme_id: SchemeId, on: date) -> Decimal | None: ...

    # --- from M1 -----------------------------------------------------------

    def position_contexts(
        self, user_id: UserId, as_of: date
    ) -> list[PositionContext]: ...

    def position_value(
        self, user_id: UserId, scheme_id: SchemeId, as_of: date
    ) -> Decimal: ...

    def direct_holdings(self, user_id: UserId, as_of: date) -> list[DirectHolding]: ...

    def portfolio_cashflows(
        self, user_id: UserId, as_of: date, scope: CashflowScope
    ) -> list[tuple[date, Decimal]]:
        """Never rebuilt outside M1. Portfolio XIRR is computed from these
        directly, never by averaging per-scheme XIRRs.
        """
        ...

    # --- recursion support -------------------------------------------------

    def recursion_path(self) -> set[SchemeId]:
        """Schemes currently on the FoF resolution stack."""
        ...

    def recursion_guard(self, scheme_id: SchemeId) -> AbstractContextManager[None]:
        """Depth 2, cycle-guarded. A FoF holding itself must not loop."""
        ...
