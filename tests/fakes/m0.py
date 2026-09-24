"""`FakeMarketDataProvider`.

MODULE_0.md §11.5 and §14. It reads YAML fixtures, so M1's entire test suite
runs with zero database — `BUILD_ORDER.md` R1 step 2, and what makes V0.1
(ledger on fixtures) possible before any of M0's ingestion exists.

Lives under `tests/` rather than `src/`: a test double is not library code,
and shipping one means every install carries it.

These fakes are deliberately strict. Where the real provider would raise on a
missing scheme or an absent NAV, so does this one: a fake that returns a
plausible zero teaches tests to pass against data that would crash in
production.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from src.common.contracts.entity import MergerLink, SchemeRef
from src.common.contracts.market import IdcwEvent, IndexPoint, NavPoint
from src.common.types import (
    Confidence,
    IndexId,
    Isin,
    Plan,
    SchemeId,
)

from tests.fakes.loader import (
    FixtureError,
    FixtureStore,
    as_date,
    as_decimal,
    default_store,
)


class FakeMarketDataProvider:
    """The ONLY interface M1 uses to reach M0 — fixture-backed.

    Satisfies `MarketDataProvider`.
    """

    def __init__(self, store: FixtureStore | None = None) -> None:
        self._store = store or default_store()
        self._md = self._store.section("market_data")

    # --- scheme resolution -------------------------------------------------

    def _schemes(self) -> list[dict[str, Any]]:
        return self._store.table("market_data", "schemes")

    def _scheme_row(self, scheme_id: str) -> dict[str, Any]:
        for row in self._schemes():
            if row["scheme_id"] == scheme_id:
                return row
        raise FixtureError(f"no scheme {scheme_id!r} in market_data.yaml")

    def resolve_scheme(
        self,
        isin: Isin | None,
        name: str,
        amfi_code: str | None,
        txn_date: date,
    ) -> SchemeRef:
        """§11.3 resolution order: ISIN, then AMFI code, then fuzzy name.

        Never resolves on name when an ISIN is present — that is the path to
        Direct/Regular confusion and a silent ~1%/year error.
        """
        if isin:
            for row in self._schemes():
                if row.get("isin") == isin:
                    return self._follow_merger(row, txn_date, "isin", Confidence.HIGH)
            # An ISIN that resolves to nothing is unresolved, NOT a fallback to
            # name. Falling through here is the bug this ordering exists to stop.
            return SchemeRef(None, "", "", Confidence.UNRESOLVED, "none", None)

        if amfi_code:
            for row in self._schemes():
                if row.get("amfi_code") == amfi_code:
                    return self._follow_merger(
                        row, txn_date, "amfi_code", Confidence.MEDIUM
                    )

        # §11.3's third step is a fuzzy name match. Narrowed to a UNIQUE exact
        # match, so this fake cannot resolve something the real provider will
        # not — DECISIONS V0-21. In the live AMFI file 1,467 distinct
        # (name, plan, option) triples map to more than one scheme, so a fuzzy
        # match is a coin flip between two real funds with different NAVs.
        #
        # A fake more permissive than production teaches tests to pass against
        # behaviour that will never ship, which is the failure this module's
        # own docstring warns about.
        needle = name.strip().lower()
        matches = [
            row for row in self._schemes()
            if needle and needle == str(row["scheme_name"]).strip().lower()
        ]
        if len(matches) == 1:
            return self._follow_merger(matches[0], txn_date, "name_exact", Confidence.LOW)

        return SchemeRef(None, "", "", Confidence.UNRESOLVED, "none", None)

    def _follow_merger(
        self,
        row: dict[str, Any],
        txn_date: date,
        matched_by: str,
        confidence: Confidence,
    ) -> SchemeRef:
        merged_from: str | None = None
        current = row
        seen: set[str] = set()
        while current.get("status") == "merged":
            merger_date = as_date(current.get("merger_date"))
            if merger_date is None or txn_date < merger_date:
                break
            successor = str(current["merged_into"])
            if successor in seen:  # acyclic is a §11.4 guarantee; prove it here
                raise FixtureError(f"merger cycle at {successor!r}")
            seen.add(successor)
            merged_from = str(current["scheme_id"])
            current = self._scheme_row(successor)

        return SchemeRef(
            scheme_id=SchemeId(str(current["scheme_id"])),
            plan=str(current["plan"]),
            option=str(current["option"]),
            confidence=confidence,
            matched_by=matched_by,
            merged_from=SchemeId(merged_from) if merged_from else None,
        )

    def merger_chain(self, scheme_id: SchemeId) -> list[MergerLink]:
        out = []
        for row in self._store.table("market_data", "mergers"):
            if scheme_id in (row["predecessor_scheme_id"], row["successor_scheme_id"]):
                merger_date = as_date(row["merger_date"])
                assert merger_date is not None
                out.append(
                    MergerLink(
                        predecessor_scheme_id=SchemeId(str(row["predecessor_scheme_id"])),
                        successor_scheme_id=SchemeId(str(row["successor_scheme_id"])),
                        merger_date=merger_date,
                        ratio_num=int(row["ratio_num"]),
                        ratio_den=int(row["ratio_den"]),
                    )
                )
        return out

    def inception(self, scheme_id: SchemeId) -> date | None:
        return as_date(self._scheme_row(scheme_id).get("inception_date"))

    # --- NAV ---------------------------------------------------------------

    def _navs(self, scheme_id: str) -> list[dict[str, Any]]:
        navs = self._md.get("navs") or {}
        rows = navs.get(scheme_id)
        if rows is None:
            raise FixtureError(f"no NAV series for {scheme_id!r}")
        return list(rows)

    def nav(self, scheme_id: SchemeId, on: date, adjusted: bool = False) -> NavPoint:
        """Latest NAV on or before `on`.

        Raises when the series starts after `on`. §11.4 guarantees no gaps > 3
        business days for a held scheme; a fake that invented a NAV here would
        let a test pass on a portfolio the real system cannot price.
        """
        best: dict[str, Any] | None = None
        best_date: date | None = None
        for row in self._navs(scheme_id):
            row_date = as_date(row["nav_date"])
            assert row_date is not None, "every fixture NAV row needs a date"
            if row_date <= on and (best_date is None or row_date > best_date):
                best, best_date = row, row_date
        if best is None or best_date is None:
            raise FixtureError(f"no NAV for {scheme_id!r} on or before {on}")

        nav_value = as_decimal(best["nav"])
        assert nav_value is not None
        return NavPoint(
            scheme_id=SchemeId(scheme_id),
            nav_date=best_date,
            nav=nav_value,
            is_interpolated=bool(best.get("is_interpolated", False)),
        )

    def nav_series(
        self,
        scheme_id: SchemeId,
        start: date,
        end: date,
        adjusted: bool = True,
    ) -> list[NavPoint]:
        out = []
        for row in self._navs(scheme_id):
            row_date = as_date(row["nav_date"])
            nav_value = as_decimal(row["nav"])
            assert row_date is not None and nav_value is not None
            if start <= row_date <= end:
                out.append(
                    NavPoint(
                        scheme_id=SchemeId(scheme_id),
                        nav_date=row_date,
                        nav=nav_value,
                        is_interpolated=bool(row.get("is_interpolated", False)),
                    )
                )
        return sorted(out, key=lambda p: p.nav_date)

    def idcw_events(self, scheme_id: SchemeId, start: date, end: date) -> list[IdcwEvent]:
        events = (self._md.get("idcw_events") or {}).get(scheme_id) or []
        out = []
        for row in events:
            record_date = as_date(row["record_date"])
            amount = as_decimal(row["amount_per_unit"])
            assert record_date is not None and amount is not None
            if start <= record_date <= end:
                out.append(
                    IdcwEvent(
                        scheme_id=SchemeId(scheme_id),
                        record_date=record_date,
                        amount_per_unit=amount,
                    )
                )
        return sorted(out, key=lambda e: e.record_date)

    # --- scheme attributes -------------------------------------------------

    def tax_class(self, scheme_id: SchemeId, on: date) -> str:
        classes = self._md.get("tax_class") or {}
        if scheme_id not in classes:
            raise FixtureError(f"no tax class for {scheme_id!r}")
        return str(classes[scheme_id])

    def _ter_rows(self, scheme_id: str) -> list[dict[str, Any]]:
        return list((self._md.get("ter") or {}).get(scheme_id) or [])

    def ter(self, scheme_id: SchemeId, on: date) -> Decimal:
        for row in self._ter_rows(scheme_id):
            frm = as_date(row["valid_from"])
            to = as_date(row.get("valid_to"))
            assert frm is not None
            if frm <= on and (to is None or on <= to):
                value = as_decimal(row["ter"])
                assert value is not None
                return value
        raise FixtureError(f"no TER for {scheme_id!r} on {on}")

    def exit_load_period(self, scheme_id: SchemeId) -> timedelta:
        days = (self._md.get("exit_load_days") or {}).get(scheme_id)
        if days is None:
            raise FixtureError(f"no exit load period for {scheme_id!r}")
        return timedelta(days=int(days))

    def sibling_plan(self, scheme_id: SchemeId, plan: Plan) -> SchemeId | None:
        siblings = (self._md.get("sibling_plans") or {}).get(scheme_id) or {}
        wanted = plan.value if isinstance(plan, Plan) else str(plan)
        found = siblings.get(wanted)
        return SchemeId(str(found)) if found else None

    # --- benchmark ---------------------------------------------------------

    def benchmark_for(self, scheme_id: SchemeId) -> IndexId | None:
        found = (self._md.get("scheme_benchmarks") or {}).get(scheme_id)
        return IndexId(str(found)) if found else None

    def index_level(self, index_id: IndexId, on: date) -> Decimal | None:
        levels = (self._md.get("index_levels") or {}).get(index_id) or []
        for row in levels:
            if as_date(row["level_date"]) == on:
                return as_decimal(row["level"])
        return None

    def index_series(self, index_id: IndexId, start: date, end: date) -> list[IndexPoint]:
        levels = (self._md.get("index_levels") or {}).get(index_id) or []
        out = []
        for row in levels:
            level_date = as_date(row["level_date"])
            level = as_decimal(row["level"])
            assert level_date is not None and level is not None
            if start <= level_date <= end:
                out.append(
                    IndexPoint(index_id=index_id, level_date=level_date, level=level)
                )
        return sorted(out, key=lambda p: p.level_date)
