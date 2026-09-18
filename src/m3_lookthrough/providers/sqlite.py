"""`LookThroughProvider` over the stored tables. MODULE_3.md §15.1.

The boundary M4, M5 and M6 read M3 through. `MODULE_6.md` §1.3 rule 2: *"M6
reads only through upstream provider interfaces. No direct SQL against analytics
tables."*

**It holds a Zone A connection and a Zone B one, deliberately.** The exposures
are Zone B, this user's; `issuer_name` is Zone A's user-independent entity
master. Something must cross that seam and this is the right place, so the
crossing is explicit rather than incidental (`PLAN.md` §6.3).

**Nothing here computes** — every figure is read back from what
`save_lookthrough` and `persist_metrics` wrote. §2.1 one layer down: a number
derived in the read path is a second source of truth nobody can reconcile
against the first.

Two traps this adapter absorbs:

1. **There are two `Exposure` dataclasses** — `engine.Exposure` and the frozen
   `providers.lookthrough.Exposure` (`BUILD_ORDER.md` R3). This is the only
   place they meet, and it reads the stored columns directly rather than
   back-filling from `persist.load_exposures`, which would have to invent
   `holdings_as_of`.
2. **`ORDER BY` on a `DECIMAL_TEXT` column sorts as text.** §15.3 requires
   descending `exposure_inr` *"Always"*, because M6 caches on payload hashes,
   so every ordering here is done in Python.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal

from src.common.types import (
    ClassificationBasis,
    ExposureScope,
    IssuerId,
    SchemeId,
    UserId,
    WeightBasis,
)
from src.m3_lookthrough.concentration import Concentration as ComputedConcentration
from src.m3_lookthrough.overlap import Overlap as ComputedOverlap
from src.m3_lookthrough.persist_metrics import (
    load_concentration,
    load_duplication,
    load_overlap,
)
from src.m3_lookthrough.providers.lookthrough import (
    Concentration,
    Contribution,
    Exposure,
    LookThroughResult,
    Marginal,
    Overlap,
    PortfolioSummary,
    Redundancy,
    Tilt,
)

#: What each unimplemented method is waiting on. Raised rather than returned
#: empty: a caller that gets `[]` from `tilts()` concludes the portfolio has no
#: sector tilt, which is a much worse answer than "this is not built yet".
#: `PLAN.md` §4.9 degrades a panel rather than a screen, and M6's builders turn
#: these into a `state='empty'` envelope naming the missing input (§3.3).
_NOT_BUILT = {
    "redundancy": (
        "fund redundancy needs 36-month return correlation and style distance "
        "from M2, which is not built. Pairwise overlap is available now via "
        "overlap_matrix()."
    ),
    "marginal": (
        "marginal contribution is computed in m3_lookthrough.marginal, which "
        "takes positions and weights; this provider holds persisted RESULTS "
        "and has no path back to either. Wiring it needs the fund-detail view "
        "that would call it, which is one of the five M6 views not built. "
        "Only style_shift_pp and fee_cost_inr actually need M2."
    ),
    "tilts": (
        "portfolio tilt needs M5's canonical sector taxonomy, deferred in "
        "V1-03, and index_constituent for the benchmark comparison."
    ),
    "sector_exposure": (
        "sector exposure needs M5's canonical sector taxonomy, deferred in "
        "V1-03. Issuer-level exposure is available now via exposures()."
    ),
}


class SqliteLookThroughProvider:
    """§15.1 over `lookthrough_*`, `portfolio_*` (Zone B) and `issuer` (Zone A).

    `ledger` is the SQLCipher connection from `connect_ledger`; `warehouse` is
    the plain SQLite one from `connect`. Both must have the Decimal converters
    registered — V1-16 records what happens when only one does.
    """

    def __init__(
        self, ledger: sqlite3.Connection, warehouse: sqlite3.Connection
    ) -> None:
        self._ledger = ledger
        self._warehouse = warehouse
        self._names: dict[str, str] = {}

    # --- names -------------------------------------------------------------

    def issuer_name(self, issuer_id: IssuerId) -> str:
        """Zone A's display name, falling back to the id — which is never wrong.

        Cached per instance because the Sankey asks for every issuer it renders
        and the answer cannot change inside one request.
        """
        key = str(issuer_id)
        if key not in self._names:
            row = self._warehouse.execute(
                "SELECT canonical_name FROM issuer WHERE issuer_id = ?", (key,)
            ).fetchone()
            self._names[key] = (row[0] if row and row[0] else key)
        return self._names[key]

    # --- §4.2 exposures and contributions ----------------------------------

    def exposures(
        self,
        user_id: UserId,
        as_of: date,
        weight_basis: WeightBasis = WeightBasis.DISCLOSED,
        scope: ExposureScope = "all",
    ) -> list[Exposure]:
        """Descending `exposure_inr`, tie-broken by `issuer_id`. Always.

        `scope` filters by `instrument_class` and, for anything but `all`,
        excludes the synthetics — §8.2's denominator rule. Note that `all` keeps
        them: they are exposure the user has, and hiding `__UNRESOLVED__` from
        the list is exactly the disappearance `MODULE_6.md` Appendix A pins it
        outside `top_n` to prevent.
        """
        rows = self._ledger.execute(
            "SELECT issuer_id, exposure_inr, exposure_pct, via_funds, fund_inr,"
            " direct_inr, instrument_class, is_synthetic, holdings_as_of,"
            " staleness_days FROM lookthrough_exposure"
            " WHERE user_id = ? AND as_of = ? AND weight_basis = ?",
            (str(user_id), as_of.isoformat(), weight_basis.value),
        ).fetchall()
        found = [
            Exposure(
                issuer_id=IssuerId(r[0]),
                issuer_name=self.issuer_name(IssuerId(r[0])),
                exposure_inr=r[1],
                exposure_pct=r[2],
                via_funds=r[3],
                fund_inr=r[4],
                direct_inr=r[5],
                instrument_class=r[6],
                is_synthetic=bool(r[7]),
                holdings_as_of=date.fromisoformat(r[8]) if r[8] else None,
                staleness_days=r[9],
            )
            for r in rows
        ]
        if scope != "all":
            found = [
                e
                for e in found
                if not e.is_synthetic and e.instrument_class == scope
            ]
        found.sort(key=lambda e: (-e.exposure_inr, str(e.issuer_id)))
        return found

    def contributions(
        self,
        user_id: UserId,
        as_of: date,
        issuer_id: IssuerId,
        weight_basis: WeightBasis = WeightBasis.DISCLOSED,
    ) -> list[Contribution]:
        """§4.2's audit trail: which funds give the user this issuer."""
        return self._contributions(
            user_id, as_of, weight_basis, issuer_id=issuer_id
        )

    def _contributions(
        self,
        user_id: UserId,
        as_of: date,
        weight_basis: WeightBasis,
        issuer_id: IssuerId | None = None,
    ) -> list[Contribution]:
        sql = (
            "SELECT issuer_id, scheme_id, depth, weight_in_fund, exposure_inr"
            " FROM lookthrough_contribution"
            " WHERE user_id = ? AND as_of = ? AND weight_basis = ?"
        )
        params: list[object] = [str(user_id), as_of.isoformat(), weight_basis.value]
        if issuer_id is not None:
            sql += " AND issuer_id = ?"
            params.append(str(issuer_id))
        rows = self._ledger.execute(sql, tuple(params)).fetchall()
        found = [
            Contribution(
                issuer_id=IssuerId(r[0]),
                scheme_id=SchemeId(r[1]),
                depth=r[2],
                weight_in_fund=r[3],
                exposure_inr=r[4],
            )
            for r in rows
        ]
        found.sort(key=lambda c: (-c.exposure_inr, str(c.scheme_id)))
        return found

    # --- §4.6 summary ------------------------------------------------------

    def summary(self, user_id: UserId, as_of: date) -> PortfolioSummary:
        """§13. Fields M1 and M2 own come back None — not zero.

        A NULL says "not computed"; a zero says "computed, and it is nothing".
        `portfolio_xirr` is the one that matters: reporting 0% return because
        the tax engine has not run would be a lie with a number on it.
        """
        row = self._ledger.execute(
            "SELECT total_value_inr, fund_value_inr, direct_value_inr,"
            " invested_net_inr, unrealised_pnl_inr, realised_pnl_todate,"
            " portfolio_xirr, portfolio_twrr_ann, blended_ter, annual_fee_inr,"
            " scheme_count, folio_count, issuer_count, direct_issuer_count,"
            " coverage_pct, schemes_covered, schemes_stale, schemes_quarantined,"
            " unresolved_pct, worst_staleness_days, confidence, caveats,"
            " computed_at FROM portfolio_summary WHERE user_id = ? AND as_of = ?",
            (str(user_id), as_of.isoformat()),
        ).fetchone()
        if row is None:
            raise LookupError(
                f"no look-through stored for {user_id} at {as_of}. "
                f"`scripts.show_lookthrough` writes it."
            )
        return PortfolioSummary(
            user_id=user_id,
            as_of=as_of,
            total_value_inr=row[0],
            fund_value_inr=row[1],
            direct_value_inr=row[2],
            invested_net_inr=row[3],
            unrealised_pnl_inr=row[4],
            realised_pnl_todate=row[5],
            portfolio_xirr=row[6],
            portfolio_twrr_ann=row[7],
            blended_ter=row[8],
            annual_fee_inr=row[9],
            scheme_count=row[10],
            folio_count=row[11],
            issuer_count=row[12],
            direct_issuer_count=row[13],
            coverage_pct=row[14],
            schemes_covered=row[15],
            schemes_stale=row[16],
            schemes_quarantined=row[17],
            unresolved_pct=row[18],
            worst_staleness_days=row[19],
            confidence=row[20],
            caveats=json.loads(row[21]) if row[21] else [],
            computed_at=datetime.fromisoformat(row[22]),
        )

    def lookthrough(
        self,
        user_id: UserId,
        as_of: date,
        weight_basis: WeightBasis = WeightBasis.DISCLOSED,
    ) -> LookThroughResult:
        """§5.1, assembled from storage rather than recomputed.

        `holdings_dates` is added per DECISIONS D7 and comes from the per-issuer
        `holdings_as_of` the save recorded — each issuer's EARLIEST contributing
        disclosure (§5.5), so the minimum of this list is the portfolio's
        worst-case staleness and not an average of anything.
        """
        found = self.exposures(user_id, as_of, weight_basis)
        summary = self.summary(user_id, as_of)
        return LookThroughResult(
            exposures=found,
            contributions=self._contributions(user_id, as_of, weight_basis),
            summary=summary,
            caveats=summary.caveats,
            holdings_dates=sorted(
                {e.holdings_as_of for e in found if e.holdings_as_of is not None}
            ),
        )

    # --- §4.3 concentration, overlap, duplication --------------------------

    def concentration(
        self,
        user_id: UserId,
        as_of: date,
        scope: ExposureScope = "equity",
    ) -> Concentration:
        """§8.1. Raises when it was never computed; zero-count when the pool is
        genuinely empty. Those are different facts and M6 says different things
        about them.
        """
        stored = load_concentration(self._ledger, user_id, as_of, scope)
        if stored is None:
            raise LookupError(
                f"no {scope} concentration stored for {user_id} at {as_of}. "
                f"`scripts.show_lookthrough` writes it."
            )
        return _to_contract_concentration(stored)

    def overlap_matrix(self, user_id: UserId, as_of: date) -> list[Overlap]:
        """§9.1. Most overlapping first, already ordered in Python by the load."""
        stored = load_overlap(self._ledger, user_id, as_of)
        return [_to_contract_overlap(o) for o in stored]

    def pairwise_overlap(
        self,
        user_id: UserId,
        as_of: date,
        scheme_a: SchemeId,
        scheme_b: SchemeId,
    ) -> Overlap | None:
        """One pair, in either argument order — the stored row is unordered."""
        wanted = {str(scheme_a), str(scheme_b)}
        for pair in self.overlap_matrix(user_id, as_of):
            if {str(pair.scheme_a), str(pair.scheme_b)} == wanted:
                return pair
        return None

    def max_pairwise_overlap(self, user_id: UserId, as_of: date) -> Decimal | None:
        """DECISIONS D2 — M4's `overlap_max` risk limit delegates to this.

        `None` for a portfolio of fewer than two funds, where the question does
        not arise. Zero would claim the funds were measured and found disjoint.
        """
        pairs = self.overlap_matrix(user_id, as_of)
        return max((p.overlap_pct for p in pairs), default=None)

    def duplication(
        self, user_id: UserId, as_of: date
    ) -> tuple[Decimal | None, Decimal | None, int, int] | None:
        """§9.4's four figures, for M6's `duplication_summary`.

        Not on the `LookThroughProvider` protocol — §15.1 never declared it,
        because §9.4 was written after. Returned as a tuple rather than
        widening the frozen protocol without an ADR.
        """
        stored = load_duplication(self._ledger, user_id, as_of)
        if stored is None:
            return None
        return (
            stored.duplicated_pct,
            stored.duplicated_inr,
            stored.issuers_multi_fund,
            stored.max_funds_per_issuer,
        )

    # --- not built yet -----------------------------------------------------

    def redundancy(self, user_id: UserId, as_of: date) -> list[Redundancy]:
        raise NotImplementedError(_NOT_BUILT["redundancy"])

    def marginal(
        self, user_id: UserId, as_of: date, scheme_id: SchemeId
    ) -> Marginal:
        raise NotImplementedError(_NOT_BUILT["marginal"])

    def tilts(
        self,
        user_id: UserId,
        as_of: date,
        dimension: str,
        basis: ClassificationBasis,
    ) -> list[Tilt]:
        raise NotImplementedError(_NOT_BUILT["tilts"])

    def sector_exposure(
        self, user_id: UserId, as_of: date, basis: ClassificationBasis
    ) -> dict[str, Decimal]:
        raise NotImplementedError(_NOT_BUILT["sector_exposure"])


def _to_contract_concentration(stored: ComputedConcentration) -> Concentration:
    """§15.2's shape from §8.1's.

    `largest_issuer_pct` is `top1_pct` — the largest issuer's share of the
    scoped pool is exactly what `top(1)` computes, and §4.3 declares both
    columns.
    """
    return Concentration(
        scope=stored.scope,
        issuer_count=stored.issuer_count,
        hhi=stored.hhi,
        effective_n=stored.effective_n,
        top1_pct=stored.top1_pct,
        top5_pct=stored.top5_pct,
        top10_pct=stored.top10_pct,
        top20_pct=stored.top20_pct,
        gini=stored.gini,
        largest_issuer_id=stored.largest_issuer_id,
        largest_issuer_pct=stored.top1_pct,
    )


def _to_contract_overlap(stored: ComputedOverlap) -> Overlap:
    """§15.2's shape from §9.1's.

    The two differ only in field order and in which columns are nullable, so
    this is a transcription, not a conversion — no arithmetic happens here, by
    design. A unit conversion hidden in an adapter is the kind of defect that
    survives every test written on either side of it.
    """
    return Overlap(
        scheme_a=stored.scheme_a,
        scheme_b=stored.scheme_b,
        overlap_pct=stored.overlap_pct,
        common_issuers=stored.common_issuers,
        union_issuers=stored.union_issuers,
        as_of_a=stored.as_of_a,
        as_of_b=stored.as_of_b,
        as_of_gap_days=stored.as_of_gap_days,
        aligned=stored.aligned,
        overlap_equity_pct=stored.overlap_equity_pct,
        jaccard=stored.jaccard,
        overlap_value_inr=stored.overlap_value_inr,
    )


__all__ = ["SqliteLookThroughProvider"]
