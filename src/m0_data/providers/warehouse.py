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
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal

from src.common.contracts.entity import MergerLink, SchemeRef
from src.common.contracts.market import IdcwEvent, IndexPoint, NavPoint
from src.common.types import Confidence, IndexId, Isin, Plan, SchemeId
from src.m0_data.universe import Fund, live_funds

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
        self._live: list[Fund] | None = None

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
        """The total TER in force on `on`, percent a year (DECISIONS V1-78)."""
        found = self.ters([str(scheme_id)], on).get(str(scheme_id))
        if found is None:
            raise KeyError(f"no TER for {scheme_id} on or before {on}")
        return found.total

    def fund_sizes(self, scheme_ids: list[str]) -> dict[str, Decimal]:
        """Each fund's newest size on record, in rupees (`scheme_aum`), for the
        peers' bubble chart. Funds with none are absent, not zero."""
        found: dict[str, Decimal] = {}
        for start in range(0, len(scheme_ids), 500):
            chunk = scheme_ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            for sid, aum in self.conn.execute(
                "SELECT scheme_id, aum_inr FROM scheme_aum"
                f" WHERE scheme_id IN ({marks}) AND aum_inr IS NOT NULL"
                " ORDER BY scheme_id, as_of_date",
                chunk,
            ):
                found[str(sid)] = Decimal(str(aum))  # the newest date wins
        return found

    def ters(self, scheme_ids: list[str], on: date) -> dict[str, Ter]:
        """Each fund's TER in force on `on`: its newest `valid_from` on or before
        that day, at its latest revision. Empty before migration 017."""
        found: dict[str, Ter] = {}
        for start in range(0, len(scheme_ids), 500):
            chunk = scheme_ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            try:
                rows = self.conn.execute(
                    "SELECT scheme_id, valid_from, total_ter, base_ter FROM scheme_ter"
                    f" WHERE scheme_id IN ({marks}) AND valid_from <= ?"
                    " ORDER BY scheme_id, valid_from, revision",
                    (*chunk, on),
                ).fetchall()
            except sqlite3.OperationalError:
                return {}
            for sid, day, total, base in rows:  # the last row per fund wins
                found[str(sid)] = Ter(
                    str(sid), _as_date(day), Decimal(str(total)),
                    None if base is None else Decimal(str(base)),
                )
        return found

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

    def index_series(
        self, index_id: IndexId, start: date, end: date
    ) -> list[IndexPoint]:
        """Every level `index_id` published from `start` to `end`, in order.

        One range read for what `index_level` answers a date at a time: the
        fund page pairs thousands of NAV dates with levels, and a query per
        date is what made that slow. Same TRI rule (invariant 7).
        """
        rows = self._execute(
            "SELECT l.level_date, l.level FROM index_level l"
            " JOIN benchmark_index b ON b.index_id = l.index_id"
            " WHERE l.index_id = ? AND l.level_date BETWEEN ? AND ?"
            " AND b.is_total_return = 1 ORDER BY l.level_date",
            (str(index_id), start, end),
        ).fetchall()
        return [
            IndexPoint(index_id, _as_date(r[0]), Decimal(str(r[1]))) for r in rows
        ]

    # --- facts and search, for the fund page ---------------------------------

    def scheme_facts(self, scheme_id: SchemeId) -> SchemeFacts | None:
        """What a reader needs to recognise a fund, in words.

        The AUM is the newest on record and says what kind of figure it is: AMFI
        publishes a quarterly AVERAGE (V1-50), not a month-end balance.
        """
        row = self._execute(
            "SELECT s.scheme_id, s.scheme_name, s.fund_name, s.sebi_category,"
            " s.plan, s.option, s.status, a.amc_name, s.benchmark_id,"
            " b.index_name, s.inception_date"
            " FROM scheme s LEFT JOIN amc a ON a.amc_id = s.amc_id"
            " LEFT JOIN benchmark_index b ON b.index_id = s.benchmark_id"
            " AND b.is_total_return = 1"
            " WHERE s.scheme_id = ?",
            (str(scheme_id),),
        ).fetchone()
        if row is None:
            return None
        aum = self._execute(
            "SELECT aum_inr, as_of_date, basis FROM scheme_aum WHERE scheme_id = ?"
            " ORDER BY as_of_date DESC LIMIT 1",
            (str(scheme_id),),
        ).fetchone()
        # The newest on record, as for the size.
        ter = self.ters([str(scheme_id)], date.max).get(str(scheme_id))
        return SchemeFacts(
            scheme_id=SchemeId(str(row["scheme_id"])),
            name=str(row["fund_name"] or row["scheme_name"]),
            scheme_name=str(row["scheme_name"]),
            amc_name=row["amc_name"],
            category=row["sebi_category"],
            plan=row["plan"],
            option=row["option"],
            status=row["status"],
            benchmark_id=row["benchmark_id"],
            benchmark_name=row["index_name"],
            inception=_as_date(row["inception_date"]) if row["inception_date"] else None,
            aum_inr=Decimal(str(aum["aum_inr"])) if aum else None,
            aum_as_of=_as_date(aum["as_of_date"]) if aum else None,
            aum_basis=aum["basis"] if aum else None,
            ter=ter.total if ter else None,
            ter_as_of=ter.valid_from if ter else None,
        )

    def live_funds(self) -> list[Fund]:
        """Every live fund with a Direct plan (`m0_data.universe`, V1-75).

        Kept for the provider's life -- one request on the server, one build for
        the public copy -- since a fund page asks twice (its header's rank tile
        and its peer panel) and the answer does not change within either.
        """
        if self._live is None:
            self._live = live_funds(self.conn)
        return self._live

    def window_stats(self, scheme_ids: list[str]) -> list[WindowStat]:
        """The stored per-window figures for these funds (DECISIONS V1-77).

        Empty when the table is not there yet -- a warehouse from before
        migration 016 -- so a peer panel says its figures are not computed
        rather than failing.
        """
        found: list[WindowStat] = []
        for start in range(0, len(scheme_ids), 500):
            chunk = scheme_ids[start:start + 500]
            marks = ",".join("?" * len(chunk))
            try:
                rows = self.conn.execute(
                    "SELECT scheme_id, window_key, as_of, obs_days, spans, return_ann,"
                    " volatility_ann, max_dd, sharpe FROM fund_window_stat"
                    f" WHERE scheme_id IN ({marks})",
                    chunk,
                ).fetchall()
            except sqlite3.OperationalError:
                return []
            found += [
                WindowStat(
                    str(r[0]), str(r[1]), _as_date(r[2]), int(r[3]), bool(r[4]),
                    *(None if v is None else Decimal(str(v)) for v in r[5:9]),
                )
                for r in rows
            ]
        return found

    def search_schemes(self, query: str, limit: int = 10) -> list[SchemeHit]:
        """Funds whose name holds every word typed, one row per fund.

        A fund has up to eight share classes and a list of eight near-identical
        names is not an answer, so each fund appears once, as the share class a
        reader is likeliest to mean: Direct before Regular, Growth before IDCW.
        """
        words = [w for w in "".join(
            ch if ch.isalnum() else " " for ch in query.lower()
        ).split()][:SEARCH_MAX_WORDS]
        if not words:
            return []
        clause = " AND ".join(
            "lower(coalesce(s.fund_name, '') || ' ' || s.scheme_name) LIKE ?"
            for _ in words
        )
        rows = self._execute(
            "SELECT s.scheme_id, s.scheme_name, s.fund_name, s.plan, s.option,"
            " s.sebi_category, s.amc_id, s.scheme_family FROM scheme s"
            f" WHERE s.status = 'active' AND {clause} LIMIT {SEARCH_SCAN_ROWS}",
            tuple(f"%{w}%" for w in words),
        ).fetchall()
        best: dict[str, sqlite3.Row] = {}
        for r in rows:
            key = f"{r['amc_id']}|{r['scheme_family'] or r['scheme_id']}"
            if key not in best or _preference(r) < _preference(best[key]):
                best[key] = r
        # The words as typed, in order, first; then names that start with the
        # first word; then shorter names, which are closer to what was typed.
        phrase = " ".join(words)
        ranked = sorted(
            best.values(),
            key=lambda r: (
                phrase not in (name := str(r["fund_name"] or r["scheme_name"]).lower()),
                not name.startswith(words[0]),
                len(name),
                name,
            ),
        )
        return [
            SchemeHit(
                scheme_id=SchemeId(str(r["scheme_id"])),
                name=str(r["fund_name"] or r["scheme_name"]),
                plan=r["plan"],
                option=r["option"],
                category=r["sebi_category"],
            )
            for r in ranked[:limit]
        ]

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


#: A search is a few words. Beyond this it is not a query anyone typed, and
#: each word is a LIKE over every scheme name.
SEARCH_MAX_WORDS = 6
#: Share-class rows read before collapsing to one per fund.
SEARCH_SCAN_ROWS = 400


@dataclass(frozen=True)
class WindowStat:
    """One fund's stored figures for one window (`fund_window_stat`, V1-77)."""

    scheme_id: str
    window_key: str
    as_of: date
    obs_days: int
    spans: bool
    return_ann: Decimal | None
    volatility_ann: Decimal | None
    max_dd: Decimal | None
    sharpe: Decimal | None


@dataclass(frozen=True)
class Ter:
    """A share class's expense ratio, percent a year (`scheme_ter`, V1-78)."""

    scheme_id: str
    valid_from: date
    total: Decimal
    base: Decimal | None


@dataclass(frozen=True)
class SchemeFacts:
    scheme_id: SchemeId
    name: str  # AMFI's fund-level name where the scheme master gives one
    scheme_name: str
    amc_name: str | None
    category: str | None
    plan: str | None
    option: str | None
    status: str | None
    benchmark_id: str | None
    benchmark_name: str | None  # None when the index is not total-return
    inception: date | None
    aum_inr: Decimal | None
    aum_as_of: date | None
    aum_basis: str | None
    #: Total TER, percent a year, and the day it was published for (V1-78).
    ter: Decimal | None = None
    ter_as_of: date | None = None


@dataclass(frozen=True)
class SchemeHit:
    scheme_id: SchemeId
    name: str
    plan: str | None
    option: str | None
    category: str | None


def _preference(row: sqlite3.Row) -> tuple[bool, bool, str]:
    """Direct before Regular, Growth before IDCW, then a stable id."""
    return (row["plan"] != "direct", row["option"] != "growth", str(row["scheme_id"]))


def _as_date(value: object) -> date:
    """SQLite hands dates back as `str` unless the column is declared DATE."""
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))
