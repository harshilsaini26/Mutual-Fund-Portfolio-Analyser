"""`FakeMarketDataProvider` and `FakeFundDataProvider`.

MODULE_0.md §11.5 and §14. Both read YAML fixtures, so M1's entire test suite
runs with zero database — `BUILD_ORDER.md` R1 step 2, and what makes V0.1
(ledger on fixtures) possible before any of M0's ingestion exists.

Lives under `tests/` rather than `src/`: a test double is not library code,
and shipping one means every install carries it. M2, which §5.1 of its own
spec pointed at `FakeFundDataProvider`, was never built.

These fakes are deliberately strict. Where the real provider would raise on a
missing scheme or an absent NAV, so does this one: a fake that returns a
plausible zero teaches tests to pass against data that would crash in
production.
"""

from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from src.common.contracts.entity import Holding, MergerLink, SchemeRef
from src.common.contracts.market import IdcwEvent, IndexPoint, NavPoint
from src.common.contracts.quality import DisclosureQuality
from src.common.contracts.scheme import ManagerRow, McapList, SchemeRow, Tenure, TerPoint
from src.common.types import (
    Confidence,
    IndexId,
    InstrumentClass,
    Isin,
    IssuerId,
    ManagerId,
    McapBucket,
    Plan,
    SchemeId,
    SourceFileId,
    ValidationStatus,
)

from tests.fakes.loader import (
    FixtureError,
    FixtureStore,
    as_date,
    as_decimal,
    default_store,
    fixture_key,
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


class FakeFundDataProvider(FakeMarketDataProvider):
    """The holdings and classification surface, fixture-backed.

    Written for M2, which was never built; it survives because M3's fake reads
    holdings through it. Inherits NAV, TER and benchmark from
    `FakeMarketDataProvider` and adds holdings, classification and manager.
    """

    def __init__(self, store: FixtureStore | None = None) -> None:
        super().__init__(store)
        self._h = self._store.section("holdings")

    # --- holdings ----------------------------------------------------------

    def holdings(self, scheme_id: SchemeId, as_of: date) -> list[Holding]:
        by_scheme = (self._h.get("holdings") or {}).get(scheme_id)
        if by_scheme is None:
            raise FixtureError(f"no holdings block for {scheme_id!r}")
        rows = fixture_key(by_scheme, as_of)
        if rows is None:
            raise FixtureError(f"no holdings for {scheme_id!r} on {as_of}")

        out = []
        for row in rows:
            mv = as_decimal(row["market_value"])
            pct_norm = as_decimal(row["pct_normalised"])
            assert mv is not None and pct_norm is not None
            out.append(
                Holding(
                    scheme_id=SchemeId(scheme_id),
                    as_of_date=as_of,
                    row_number=int(row["row_number"]),
                    issuer_id=IssuerId(str(row["issuer_id"])),
                    instrument_raw_name=str(row["instrument_raw_name"]),
                    market_value=mv,
                    pct_normalised=pct_norm,
                    instrument_class=InstrumentClass(str(row["instrument_class"])),
                    resolution_method=str(row["resolution_method"]),
                    isin=Isin(str(row["isin"])) if row.get("isin") else None,
                    quantity=as_decimal(row.get("quantity")),
                    pct_to_nav=as_decimal(row.get("pct_to_nav")),
                    credit_rating=row.get("credit_rating"),
                    reported_sector=row.get("reported_sector"),
                    yield_pct=as_decimal(row.get("yield_pct")),
                    resolution_conf=as_decimal(row.get("resolution_conf")),
                    revision=int(row.get("revision", 1)),
                    source_file_id=SourceFileId(
                        str(row.get("source_file_id", "fixture"))
                    ),
                )
            )
        return out

    def disclosure_dates(
        self,
        scheme_id: SchemeId,
        start: date | None = None,
        end: date | None = None,
    ) -> list[date]:
        raw = (self._h.get("disclosure_dates") or {}).get(scheme_id) or []
        dates = [d for d in (as_date(v) for v in raw) if d is not None]
        if start:
            dates = [d for d in dates if d >= start]
        if end:
            dates = [d for d in dates if d <= end]
        return sorted(dates)

    def disclosure_quality(self, scheme_id: SchemeId, as_of: date) -> DisclosureQuality:
        by_scheme = (self._h.get("disclosure_quality") or {}).get(scheme_id)
        row = fixture_key(by_scheme, as_of) if by_scheme else None
        if row is None:
            raise FixtureError(f"no disclosure quality for {scheme_id!r} on {as_of}")
        holdings_as_of = as_date(as_of)
        unresolved = as_decimal(row["unresolved_pct"])
        residual = as_decimal(row["weight_residual"])
        assert holdings_as_of is not None
        assert unresolved is not None and residual is not None
        return DisclosureQuality(
            holdings_as_of=holdings_as_of,
            unresolved_pct=unresolved,
            weight_residual=residual,
            validation_status=ValidationStatus(str(row["validation_status"])),
            row_count=int(row["row_count"]),
        )

    def prev_disclosure(self, scheme_id: SchemeId, before: date) -> date | None:
        earlier = [d for d in self.disclosure_dates(scheme_id) if d < before]
        return max(earlier) if earlier else None

    # --- classification ----------------------------------------------------

    def mcap_list_as_of(self, on: date) -> McapList:
        """The AMFI list in force on `on` — never today's list applied backwards.

        Applying a later list retroactively produces phantom drift: the fixture
        promotes one issuer between its two lists precisely so a test can catch
        that.
        """
        candidates = []
        for row in self._store.table("holdings", "mcap_lists"):
            eff = as_date(row["effective_date"])
            assert eff is not None
            if eff <= on:
                candidates.append((eff, row))
        if not candidates:
            raise FixtureError(f"no AMFI mcap list effective on or before {on}")

        eff, row = max(candidates, key=lambda pair: pair[0])
        buckets = {
            IssuerId(k): McapBucket(str(v)) for k, v in (row["buckets"] or {}).items()
        }
        mcaps = {}
        for k, v in (row["mcaps"] or {}).items():
            value = as_decimal(v)
            assert value is not None
            mcaps[IssuerId(k)] = value
        return McapList(effective_date=eff, buckets=buckets, mcaps=mcaps)

    def sector_of(self, issuer_id: IssuerId, on: date) -> str | None:
        found = (self._h.get("sectors") or {}).get(issuer_id)
        return str(found) if found else None

    def adjustment_factor(self, isin: Isin, frm: date, to: date) -> Decimal:
        """Cumulative corporate-action factor. Defaults to 1, never to 0.

        A 0 here would silently zero out a repriced holding.
        """
        rows = (self._h.get("adjustment_factors") or {}).get(isin) or []
        factor = Decimal(1)
        for row in rows:
            row_frm = as_date(row["frm"])
            row_to = as_date(row["to"])
            assert row_frm is not None and row_to is not None
            if frm <= row_frm and row_to <= to:
                value = as_decimal(row["factor"])
                assert value is not None
                factor *= value
        return factor

    # --- index / rates -----------------------------------------------------

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

    def risk_free(self, on: date) -> Decimal | None:
        for row in self._store.table("market_data", "risk_free"):
            if as_date(row["rate_date"]) == on:
                return as_decimal(row["annual_rate"])
        return None

    # --- scheme attributes -------------------------------------------------

    def scheme(self, scheme_id: SchemeId) -> SchemeRow:
        row = self._scheme_row(scheme_id)
        return SchemeRow(
            scheme_id=SchemeId(str(row["scheme_id"])),
            scheme_name=str(row["scheme_name"]),
            plan=str(row["plan"]),
            option=str(row["option"]),
            status=str(row.get("status", "active")),
            amfi_code=row.get("amfi_code"),
            isin=row.get("isin"),
            amc_id=row.get("amc_id"),
            sebi_category=row.get("sebi_category"),
            benchmark_id=row.get("benchmark_id"),
            inception_date=as_date(row.get("inception_date")),
            merged_into=SchemeId(str(row["merged_into"]))
            if row.get("merged_into")
            else None,
            merger_date=as_date(row.get("merger_date")),
            merger_ratio_num=int(row["merger_ratio_num"])
            if row.get("merger_ratio_num")
            else None,
            merger_ratio_den=int(row["merger_ratio_den"])
            if row.get("merger_ratio_den")
            else None,
        )

    def ter_series(self, scheme_id: SchemeId) -> list[TerPoint]:
        out = []
        for row in self._ter_rows(scheme_id):
            frm = as_date(row["valid_from"])
            value = as_decimal(row["ter"])
            assert frm is not None and value is not None
            out.append(
                TerPoint(
                    scheme_id=SchemeId(scheme_id),
                    valid_from=frm,
                    ter=value,
                    valid_to=as_date(row.get("valid_to")),
                )
            )
        return sorted(out, key=lambda t: t.valid_from)

    def aum(self, scheme_id: SchemeId, on: date) -> Decimal | None:
        by_scheme = (self._md.get("aum") or {}).get(scheme_id)
        return as_decimal(fixture_key(by_scheme, on)) if by_scheme else None

    def schemes_in_category(
        self, sebi_category: str, plan: Plan, as_of: date
    ) -> list[SchemeId]:
        """Peer groups never mix plans — Direct and Regular are not comparable."""
        wanted = plan.value if isinstance(plan, Plan) else str(plan)
        return [
            SchemeId(str(row["scheme_id"]))
            for row in self._schemes()
            if row.get("sebi_category") == sebi_category
            and row.get("plan") == wanted
            and row.get("status") == "active"
        ]

    def category_universe_count(self, sebi_category: str, plan: Plan, as_of: date) -> int:
        return len(self.schemes_in_category(sebi_category, plan, as_of))

    # --- managers ----------------------------------------------------------

    def tenures(self, scheme_id: SchemeId) -> list[Tenure]:
        rows = (self._h.get("tenures") or {}).get(scheme_id) or []
        out = []
        for row in rows:
            start = as_date(row["start_date"])
            assert start is not None
            out.append(
                Tenure(
                    scheme_id=SchemeId(scheme_id),
                    manager_id=ManagerId(str(row["manager_id"])),
                    start_date=start,
                    confidence=str(row.get("confidence", "medium")),
                    end_date=as_date(row.get("end_date")),
                    role=row.get("role"),
                )
            )
        return sorted(out, key=lambda t: t.start_date)

    def manager_schemes(self, manager_id: ManagerId) -> list[Tenure]:
        out: list[Tenure] = []
        for scheme_id in self._h.get("tenures") or {}:
            out.extend(
                t for t in self.tenures(SchemeId(scheme_id)) if t.manager_id == manager_id
            )
        return out

    def manager(self, manager_id: ManagerId) -> ManagerRow:
        row = (self._h.get("managers") or {}).get(manager_id)
        if row is None:
            raise FixtureError(f"no manager {manager_id!r}")
        return ManagerRow(
            manager_id=ManagerId(manager_id),
            full_name=str(row["full_name"]),
            name_norm=str(row["name_norm"]),
            qualifications=row.get("qualifications"),
            experience_start_date=as_date(row.get("experience_start_date")),
        )
