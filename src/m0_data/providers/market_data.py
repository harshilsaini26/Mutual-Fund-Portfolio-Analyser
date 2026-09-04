"""`MarketDataProvider` — the ONLY interface M1 uses to reach M0.

MODULE_0.md §11.1, transcribed. `PLAN.md` §8.2 rule 2: M1 issues no SQL against
Zone A.

Slice Zero — Protocol stub, no implementation. `FakeMarketDataProvider` reading a
YAML fixture ships alongside it so M1's entire test suite runs with no database.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Protocol

from src.common.contracts.entity import MergerLink, SchemeRef
from src.common.contracts.market import IdcwEvent, NavPoint
from src.common.types import IndexId, Isin, Plan, SchemeId


class MarketDataProvider(Protocol):
    """The ONLY interface M1 uses to reach M0.

    Guarantees M0 owes M1 (§11.4), enforced by the §10.3 validation gates:

      - `scheme_id` is stable and never reused; ledger rows reference it forever
      - `nav_daily` has no gaps > 3 business days for held schemes
      - `nav_adj` is correct for IDCW plans
      - merger chains are complete and acyclic
      - `scheme_tax_class` covers every held scheme's full holding period
      - plan and option are never conflated
      - the 31-Jan-2018 NAV exists for pre-2018 equity holdings (grandfathering)
    """

    # --- scheme resolution -------------------------------------------------

    def resolve_scheme(
        self,
        isin: Isin | None,
        name: str,
        amfi_code: str | None,
        txn_date: date,
    ) -> SchemeRef:
        """Resolve in order: ISIN, then AMFI code, then fuzzy name (§11.3).

        Never resolve on name when an ISIN is present.
        """
        ...

    def merger_chain(self, scheme_id: SchemeId) -> list[MergerLink]: ...

    def inception(self, scheme_id: SchemeId) -> date | None: ...

    # --- NAV ---------------------------------------------------------------

    def nav(self, scheme_id: SchemeId, on: date, adjusted: bool = False) -> NavPoint: ...

    def nav_series(
        self,
        scheme_id: SchemeId,
        start: date,
        end: date,
        adjusted: bool = True,
    ) -> list[NavPoint]:
        """Adjusted by default: return math on raw NAV is wrong for IDCW plans."""
        ...

    def idcw_events(
        self, scheme_id: SchemeId, start: date, end: date
    ) -> list[IdcwEvent]: ...

    # --- scheme attributes (all point-in-time) -----------------------------

    def tax_class(self, scheme_id: SchemeId, on: date) -> str: ...

    def ter(self, scheme_id: SchemeId, on: date) -> Decimal: ...

    def exit_load_period(self, scheme_id: SchemeId) -> timedelta: ...

    def sibling_plan(self, scheme_id: SchemeId, plan: Plan) -> SchemeId | None: ...

    # --- benchmark ---------------------------------------------------------

    def benchmark_for(self, scheme_id: SchemeId) -> IndexId | None: ...

    def index_level(self, index_id: IndexId, on: date) -> Decimal | None: ...
