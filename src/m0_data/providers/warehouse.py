"""`MarketDataProvider`, served from Zone A. MODULE_0.md §11.

This is the swap R1 and R2 exist to make: the Protocol was frozen in Slice Zero
and `FakeMarketDataProvider` has been serving it from YAML ever since, so this
class changes where the data comes from and nothing else. M1 does not import it,
name it, or know it exists — `PLAN.md` §8.2 rule 2, M1 reaches M0 only through
the interface.

Returns the same frozen dataclasses the fake returns. The test that matters
parametrises one body over both implementations, so "they agree" is proven at
the interface rather than asserted in a docstring.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal

from src.common.contracts.entity import MergerLink, SchemeRef
from src.common.contracts.market import IdcwEvent, NavPoint
from src.common.types import Confidence, IndexId, Isin, Plan, SchemeId

#: §11.3 resolution confidence, by the field that matched.
_CONFIDENCE = {
    "isin": Confidence.HIGH,
    "amfi_code": Confidence.MEDIUM,
    "name_exact": Confidence.LOW,
}

UNRESOLVED = SchemeRef(
    scheme_id=None, plan="", option="", confidence=Confidence.UNRESOLVED,
    matched_by="none",
    merged_from=None,
)


class WarehouseMarketDataProvider:
    """The real `MarketDataProvider`. §11.1."""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    # --- scheme resolution -------------------------------------------------

    def resolve_scheme(
        self,
        isin: Isin | None,
        name: str,
        amfi_code: str | None,
        txn_date: date,
    ) -> SchemeRef:
        """ISIN, then AMFI code, then an *unambiguous* name. §11.3, narrowed.

        §11.3's third step is a fuzzy name match flagged low confidence, and it
        is deliberately not implemented: in the live AMFI file **1,467 distinct
        (name, plan, option) triples map to more than one scheme** — one such
        pair being lock-in and non-lock-in variants with NAVs 30.3228 and
        30.9027. A fuzzy match would choose between two real schemes by coin
        flip and label it "low confidence".

        Invariant 5 settles it: a name resolves only when exactly one candidate
        matches, otherwise `unresolved`, and M1 quarantines the row with
        `scheme_raw_*` intact so it resolves later without re-importing (V0-21).

        **Never resolve on name when an ISIN is present** (§11.3): that is the
        path to Direct/Regular confusion and a silent ~1%/year error (V0-05).
        """
        if isin:
            row = self._scheme_row("SELECT * FROM scheme WHERE isin = ?", (str(isin),))
            if row:
                return self._to_ref(row, "isin")
            # An ISIN that resolves to nothing is unresolved, full stop. Falling
            # through to the name would be exactly the substitution §11.3
            # forbids, and the CAS carries ISIN reliably.
            return UNRESOLVED

        if amfi_code:
            rows = self._scheme_rows(
                "SELECT * FROM scheme WHERE amfi_code = ?", (str(amfi_code),)
            )
            # One AMFI code can describe two schemes (payout + reinvestment).
            if len(rows) == 1:
                return self._to_ref(rows[0], "amfi_code")

        rows = self._scheme_rows(
            "SELECT * FROM scheme WHERE lower(scheme_name) = lower(?)", (name.strip(),)
        )
        if len(rows) == 1:
            return self._to_ref(rows[0], "name_exact")
        return UNRESOLVED

    def _to_ref(self, row: sqlite3.Row, matched_by: str) -> SchemeRef:
        return SchemeRef(
            scheme_id=SchemeId(row["scheme_id"]),
            plan=row["plan"],
            option=row["option"],
            confidence=_CONFIDENCE[matched_by],
            matched_by=matched_by,
            merged_from=None,
        )

    def merger_chain(self, scheme_id: SchemeId) -> list[MergerLink]:
        """Follow `merged_into` forward. Empty until mergers are loaded.

        Cycle-guarded: §11.4 requires chains be acyclic and §10.3 asserts it,
        but a cycle in the data must not hang the caller before the gate runs.
        """
        chain: list[MergerLink] = []
        seen = {str(scheme_id)}
        current = str(scheme_id)
        while True:
            row = self._scheme_row(
                "SELECT * FROM scheme WHERE scheme_id = ?", (current,)
            )
            if row is None or row["merged_into"] is None:
                return chain
            successor = row["merged_into"]
            if successor in seen:
                return chain
            seen.add(successor)
            chain.append(
                MergerLink(
                    predecessor_scheme_id=SchemeId(current),
                    successor_scheme_id=SchemeId(successor),
                    merger_date=_as_date(row["merger_date"]),
                    ratio_num=row["merger_ratio_num"] or 1,
                    ratio_den=row["merger_ratio_den"] or 1,
                )
            )
            current = successor

    def inception(self, scheme_id: SchemeId) -> date | None:
        row = self._scheme_row(
            "SELECT inception_date FROM scheme WHERE scheme_id = ?", (str(scheme_id),)
        )
        return _as_date(row["inception_date"]) if row and row["inception_date"] else None

    # --- NAV ---------------------------------------------------------------

    def nav(self, scheme_id: SchemeId, on: date, adjusted: bool = False) -> NavPoint:
        """Latest NAV on or before `on`.

        Funds do not price on weekends or market holidays, so an exact-date
        lookup returns nothing on roughly a third of all dates. Carrying the
        last published NAV forward is what a statement does and what a
        valuation means; the returned `nav_date` says which day it came from,
        so a caller can see the staleness rather than infer it.
        """
        column = "nav_adj" if adjusted else "nav"
        row = self._execute(
            f"SELECT nav_date, {column} AS v FROM nav_daily "
            "WHERE scheme_id = ? AND nav_date <= ? AND v IS NOT NULL "
            "ORDER BY nav_date DESC LIMIT 1",
            (str(scheme_id), on),
        ).fetchone()
        if row is None:
            raise KeyError(f"no {column} for {scheme_id} on or before {on}")
        return NavPoint(
            scheme_id=SchemeId(str(scheme_id)),
            nav_date=_as_date(row["nav_date"]),
            nav=row["v"],
            is_interpolated=False,
        )

    def nav_series(
        self,
        scheme_id: SchemeId,
        start: date,
        end: date,
        adjusted: bool = True,
    ) -> list[NavPoint]:
        """Adjusted by default: return math on raw NAV is wrong for IDCW plans."""
        column = "nav_adj" if adjusted else "nav"
        rows = self._execute(
            f"SELECT nav_date, {column} AS v, is_interpolated FROM nav_daily "
            "WHERE scheme_id = ? AND nav_date BETWEEN ? AND ? AND v IS NOT NULL "
            "ORDER BY nav_date",
            (str(scheme_id), start, end),
        ).fetchall()
        return [
            NavPoint(
                scheme_id=SchemeId(str(scheme_id)),
                nav_date=_as_date(r["nav_date"]),
                nav=r["v"],
                is_interpolated=bool(r["is_interpolated"]),
            )
            for r in rows
        ]

    def idcw_events(
        self, scheme_id: SchemeId, start: date, end: date
    ) -> list[IdcwEvent]:
        rows = self._execute(
            "SELECT record_date, amount_per_unit FROM scheme_idcw "
            "WHERE scheme_id = ? AND record_date BETWEEN ? AND ? ORDER BY record_date",
            (str(scheme_id), start, end),
        ).fetchall()
        return [
            IdcwEvent(
                scheme_id=SchemeId(str(scheme_id)),
                record_date=_as_date(r["record_date"]),
                amount_per_unit=r["amount_per_unit"],
            )
            for r in rows
        ]

    # --- scheme attributes -------------------------------------------------

    def tax_class(self, scheme_id: SchemeId, on: date) -> str:
        """Point-in-time. `CLAUDE.md` invariant 6.

        Not loaded in V0.4 — `scheme_tax_class` has no source yet (V0-03: a
        multi-asset fund's class cannot be read off a fund page). Raises rather
        than defaulting to equity, because MODULE_1.md §10.2 is explicit that
        defaulting understates tax.
        """
        raise KeyError(f"no tax class for {scheme_id} on {on}; not loaded in V0.4")

    def ter(self, scheme_id: SchemeId, on: date) -> Decimal:
        """`scheme_ter` is deferred to V2 — DECISIONS V0-18. No table to read."""
        raise KeyError(f"no TER for {scheme_id} on {on}; deferred to V2 (V0-18)")

    def exit_load_period(self, scheme_id: SchemeId) -> timedelta:
        raise KeyError(f"no exit load period for {scheme_id}; not loaded in V0.4")

    def sibling_plan(self, scheme_id: SchemeId, plan: Plan) -> SchemeId | None:
        """The same fund and option in the other plan.

        This is the V0-05 detector made available as data: given a Direct
        scheme it returns the Regular one, so a caller can compare the two NAV
        series and see the ~1%/year gap rather than mistake one for the other.
        """
        row = self._scheme_row(
            "SELECT scheme_name, option FROM scheme WHERE scheme_id = ?",
            (str(scheme_id),),
        )
        if row is None:
            return None
        # `Plan` is a `(str, Enum)`, and `str(Plan.REGULAR)` is "Plan.REGULAR",
        # not "regular" — the reason pyproject declines ruff's StrEnum fix.
        wanted = plan.value if isinstance(plan, Plan) else str(plan)
        rows = self._scheme_rows(
            "SELECT scheme_id FROM scheme "
            "WHERE scheme_name = ? AND option = ? AND plan = ? AND scheme_id <> ?",
            (row["scheme_name"], row["option"], wanted, str(scheme_id)),
        )
        # Ambiguous exactly where resolve_scheme is: same name, same plan, same
        # option, two schemes. Returning either would be a guess.
        return SchemeId(rows[0]["scheme_id"]) if len(rows) == 1 else None

    # --- benchmark ---------------------------------------------------------

    def benchmark_for(self, scheme_id: SchemeId) -> IndexId | None:
        row = self._scheme_row(
            "SELECT benchmark_id FROM scheme WHERE scheme_id = ?", (str(scheme_id),)
        )
        return IndexId(row["benchmark_id"]) if row and row["benchmark_id"] else None

    def index_level(self, index_id: IndexId, on: date) -> Decimal | None:
        """The level `index_id` published ON `on`, or None. S12.

        Exact date, never carried forward. A benchmark comparison pairs
        same-day prices, and a level carried over a holiday reads as a day the
        index did not move -- tracking error the fund never had.

        None for a price-return index (invariant 7). A comparison against PRI
        understates the benchmark by its dividend yield and hands that to
        alpha, so the level is withheld and the comparison cannot be made.
        """
        row = self._execute(
            "SELECT l.level FROM index_level l"
            " JOIN benchmark_index b ON b.index_id = l.index_id"
            " WHERE l.index_id = ? AND l.level_date = ? AND b.is_total_return = 1",
            (str(index_id), on),
        ).fetchone()
        return Decimal(str(row[0])) if row else None

    # --- internals ---------------------------------------------------------

    def _execute(self, sql: str, params: tuple[object, ...]) -> sqlite3.Cursor:
        """Every query, on a cursor that returns rows by name.

        Rows are read by column name throughout, and the app opens the
        warehouse with plain tuples. Setting it on the cursor rather than the
        connection leaves every other reader of that connection as it was.
        """
        cur = self.conn.cursor()
        cur.row_factory = sqlite3.Row
        return cur.execute(sql, params)

    def _scheme_row(self, sql: str, params: tuple[str, ...]) -> sqlite3.Row | None:
        row: sqlite3.Row | None = self._execute(sql, params).fetchone()
        return row

    def _scheme_rows(self, sql: str, params: tuple[str, ...]) -> list[sqlite3.Row]:
        return self._execute(sql, params).fetchall()


def _as_date(value: object) -> date:
    """SQLite hands dates back as `str` unless the column is declared DATE."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
