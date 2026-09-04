"""`FakeSchemeCharacteristics` — the ONLY M2 surface M3 consumes, fixture-backed.

MODULE_2.md §14.1. Lets M3 be built and tested before M2 exists.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from typing import Any

from src.common.contracts.quality import DataQuality
from src.common.fixtures import (
    FixtureError,
    FixtureStore,
    as_date,
    as_decimal,
    default_store,
    fixture_key,
)
from src.common.types import (
    PeerGroupId,
    SchemeId,
    UserId,
    ValidationStatus,
)
from src.m2_fund.providers.characteristics import FeeDrag, PeerContext, StyleSnapshot


class FakeSchemeCharacteristics:
    """Satisfies `SchemeCharacteristics`."""

    def __init__(self, store: FixtureStore | None = None) -> None:
        self._store = store or default_store()
        self._d = self._store.section("derived")

    def _scoped(self, section: str, scheme_id: str, as_of: date) -> dict[str, Any]:
        by_scheme = (self._d.get(section) or {}).get(scheme_id)
        row = fixture_key(by_scheme, as_of) if by_scheme else None
        if row is None:
            raise FixtureError(f"no derived.{section} for {scheme_id!r} on {as_of}")
        return dict(row)

    def style(
        self,
        scheme_id: SchemeId,
        as_of: date,
        mcap_basis: date | None = None,
    ) -> StyleSnapshot:
        """§14.2 — the shared-basis requirement.

        When `mcap_basis` is given and differs from the cached snapshot's, the
        real implementation recomputes. The fake refuses instead: silently
        returning a snapshot on the wrong basis is exactly the bug the parameter
        exists to prevent, and a fake that hides it makes the test useless.
        """
        row = self._scoped("style", scheme_id, as_of)
        cached_basis = as_date(row["mcap_basis"])
        if mcap_basis is not None and cached_basis != mcap_basis:
            raise FixtureError(
                f"style({scheme_id!r}) is on basis {cached_basis}, "
                f"but {mcap_basis} was requested; fixture has no recompute path"
            )

        def d(key: str) -> Decimal | None:
            return as_decimal(row.get(key))

        def i(key: str) -> int | None:
            v = row.get(key)
            return int(v) if v is not None else None

        unresolved = d("unresolved_pct")
        assert cached_basis is not None and unresolved is not None
        return StyleSnapshot(
            scheme_id=SchemeId(scheme_id),
            as_of_date=as_of,
            mcap_basis=cached_basis,
            unresolved_pct=unresolved,
            pct_large=d("pct_large"),
            pct_mid=d("pct_mid"),
            pct_small=d("pct_small"),
            pct_unclassified=d("pct_unclassified"),
            pct_equity=d("pct_equity"),
            pct_debt=d("pct_debt"),
            pct_cash=d("pct_cash"),
            pct_derivative=d("pct_derivative"),
            pct_mfunit=d("pct_mfunit"),
            top5_weight=d("top5_weight"),
            top10_weight=d("top10_weight"),
            hhi=d("hhi"),
            effective_n=d("effective_n"),
            holding_count=i("holding_count"),
            equity_count=i("equity_count"),
            sector_hhi=d("sector_hhi"),
            sector_count=i("sector_count"),
            wtd_avg_mcap_inr=d("wtd_avg_mcap_inr"),
            median_mcap_inr=d("median_mcap_inr"),
        )

    def quality(self, scheme_id: SchemeId, as_of: date) -> DataQuality:
        """§14.3 — the gating contract M3 keys its inclusion decisions off."""
        row = self._scoped("quality", scheme_id, as_of)
        holdings_as_of = as_date(row["holdings_as_of"])
        unresolved = as_decimal(row["unresolved_pct"])
        residual = as_decimal(row["weight_residual"])
        assert holdings_as_of is not None
        assert unresolved is not None and residual is not None
        return DataQuality(
            holdings_as_of=holdings_as_of,
            staleness_days=int(row["staleness_days"]),
            unresolved_pct=unresolved,
            weight_residual=residual,
            validation_status=ValidationStatus(str(row["validation_status"])),
            usable_for_lookthrough=bool(row["usable_for_lookthrough"]),
            confidence=str(row["confidence"]),
        )

    def peer_context(self, scheme_id: SchemeId) -> PeerContext | None:
        row = (self._d.get("peer_context") or {}).get(scheme_id)
        if row is None:
            return None
        as_of = as_date(row["as_of"])
        coverage = as_decimal(row["coverage_pct"])
        assert as_of is not None and coverage is not None
        return PeerContext(
            peer_group_id=PeerGroupId(str(row["peer_group_id"])),
            basis=str(row["basis"]),
            plan=str(row["plan"]),
            as_of=as_of,
            member_count=int(row["member_count"]),
            coverage_pct=coverage,
            sebi_category=row.get("sebi_category"),
            universe_count=int(row["universe_count"])
            if row.get("universe_count")
            else None,
            percentile=as_decimal(row.get("percentile")),
            rank=int(row["rank"]) if row.get("rank") else None,
        )

    def return_correlation(
        self, scheme_a: SchemeId, scheme_b: SchemeId, months: int = 36
    ) -> Decimal | None:
        """Keyed on the sorted pair, so the answer cannot depend on argument order."""
        key = "|".join(sorted([str(scheme_a), str(scheme_b)]))
        return as_decimal((self._d.get("return_correlation") or {}).get(key))

    def fee_drag(
        self, user_id: UserId, scheme_id: SchemeId, as_of: date
    ) -> FeeDrag | None:
        by_scheme = (self._d.get("fee_drag") or {}).get(scheme_id)
        row = fixture_key(by_scheme, as_of) if by_scheme else None
        if row is None:
            return None
        return FeeDrag(
            user_id=UserId(user_id),
            scheme_id=SchemeId(scheme_id),
            as_of=as_of,
            fee_paid_inr=as_decimal(row.get("fee_paid_inr")),
            fee_pct_of_gain=as_decimal(row.get("fee_pct_of_gain")),
            blended_ter=as_decimal(row.get("blended_ter")),
            regular_penalty_inr=as_decimal(row.get("regular_penalty_inr")),
        )
