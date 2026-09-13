"""AMFI's scheme-wise AAUM, and the check it finally lets run. V1-49.

`tests/fixtures/m0/amfi_aaum_2026Q1.json` is the real response for
`April - June 2026`, trimmed from 8,545 share classes to the 16 belonging to
the four funds this project actually holds. Every retained row is verbatim;
nothing was edited to make a number work.

**What §10's V2 is for, and what it could not do.** V2 reconciles a
disclosure's summed market value against `scheme_aum` and QUARANTINES on
failure, because §7.2's 100x unit error fails it by two orders of magnitude.
`scheme_aum` did not exist, so V2 had never run on a single disclosure here —
0 of 205 carried an `aum_reported`. These tests exist to stop it going back.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from jobs.fetch_aum import load_quarter, published
from src.common.decimals import connect
from src.m0_data.fetch.amfi_aum import (
    AAUM_UNIT,
    BASIS,
    AumPayloadError,
    SchemeAaum,
    parse_aaum,
    parse_periods,
    parse_years,
    quarter_end,
)
from src.m0_data.load import WITNESS_MAX_AGE_DAYS, aum_for
from src.m0_data.normalise.units import to_inr
from src.m0_data.validate.checks import (
    AUM_AVERAGE_TOLERANCE_PCT,
    AUM_TOLERANCE_PCT,
    HoldingRow,
    promote_or_quarantine,
    validate_disclosure,
)

FIXTURE = (
    Path(__file__).resolve().parents[1] / "fixtures" / "m0" / "amfi_aaum_2026Q1.json"
)
CRORE = Decimal(10) ** 7


def _rows() -> list[SchemeAaum]:
    return parse_aaum(FIXTURE.read_bytes())


def _family_total(needle: str) -> Decimal:
    """The scheme's AUM: the sum over its share classes, in rupees."""
    return sum(
        (
            to_inr(r.aaum_raw, AAUM_UNIT)
            for r in _rows()
            if needle.lower() in r.scheme_name.lower()
        ),
        Decimal(0),
    )


class TestTheFigureIsPerPlanAndTheSchemeIsTheSum:
    """AMFI publishes one row per share class. V1-37 established that a
    disclosure describes the SCHEME, every plan of which holds one pool of
    assets — so comparing a portfolio against one plan's AUM fails by a factor
    of three, and the sum is the only figure that means the same thing."""

    def test_one_plan_is_a_fraction_of_the_fund(self) -> None:
        direct = [
            r
            for r in _rows()
            if "HDFC Flexi Cap" in r.scheme_name
            and "Direct" in r.scheme_name
            and "Growth" in r.scheme_name
            and "IDCW" not in r.scheme_name
        ]
        assert len(direct) == 1
        one_plan = to_inr(direct[0].aaum_raw, AAUM_UNIT) / CRORE
        assert Decimal(30_000) < one_plan < Decimal(40_000), (
            f"Direct Growth alone is Rs {one_plan:,.0f} Cr"
        )

    @pytest.mark.parametrize(
        ("needle", "low", "high"),
        [
            ("HDFC Flexi Cap", 95_000, 110_000),
            ("Parag Parikh Flexi Cap", 130_000, 150_000),
        ],
    )
    def test_the_family_sum_matches_the_disclosed_portfolio(
        self, needle: str, low: int, high: int
    ) -> None:
        """The units witness. HDFC's portfolio is Rs 113,606 Cr and PPFAS's is
        Rs 147,404 Cr; the family sums land just under both, which they could
        not do if `lakh` were the wrong scale by any factor at all."""
        total = _family_total(needle) / CRORE
        assert Decimal(low) < total < Decimal(high), f"{needle} summed to {total:,.0f} Cr"

    def test_every_held_fund_is_covered(self) -> None:
        for needle in (
            "HDFC Flexi Cap",
            "Parag Parikh Flexi Cap",
            "Kotak Pioneer",
            "Axis Small Cap",
        ):
            assert _family_total(needle) > 0, f"no AAUM for {needle}"


class TestTheKeyIsTheAmfiCode:
    def test_every_row_carries_one(self) -> None:
        """It joins `scheme.amfi_code` directly — 98.9% of the live payload —
        so nothing here matches on a name, which is what V1-43's slug and
        V1-41's issuer naming both had to."""
        rows = _rows()
        assert rows
        assert all(r.amfi_code and r.amfi_code.isdigit() for r in rows)
        assert len({r.amfi_code for r in rows}) == len(rows), "codes are not unique"


class TestQuarterEnd:
    @pytest.mark.parametrize(
        ("label", "expected"),
        [
            ("April - June 2026", "2026-06-30"),
            ("January - March 2026", "2026-03-31"),
            ("October - December 2025", "2025-12-31"),
            ("July - September 2026", "2026-09-30"),
            ("April " + chr(0x2013) + " June 2026", "2026-06-30"),
        ],
    )
    def test_the_window_ends_where_it_ends(self, label: str, expected: str) -> None:
        """The END of the window, not its midpoint: `as_of_date` is what
        `aum_for` compares against a disclosure date, and an average dated at
        its midpoint would look fresher than the data it summarises."""
        assert quarter_end(label) == date.fromisoformat(expected)

    def test_february_is_not_assumed_to_have_thirty_days(self) -> None:
        assert quarter_end("December - February 2028").day == 29

    @pytest.mark.parametrize("label", ["2026", "Q1 2026", "Smarch - June 2026", ""])
    def test_a_label_this_reader_does_not_know_raises(self, label: str) -> None:
        with pytest.raises(AumPayloadError):
            quarter_end(label)


class TestItRaisesRatherThanReturningNothing:
    """ "AMFI published no schemes" and "the endpoint moved" are different facts
    and a caller handed an empty list cannot tell them apart."""

    @pytest.mark.parametrize("parser", [parse_aaum, parse_years, parse_periods])
    def test_a_non_json_response_raises(self, parser) -> None:  # type: ignore[no-untyped-def]
        with pytest.raises(AumPayloadError, match="not JSON"):
            parser(b"<html>maintenance</html>")

    def test_an_empty_payload_raises(self) -> None:
        with pytest.raises(AumPayloadError, match="no fund houses"):
            parse_aaum(b'{"data": []}')

    def test_houses_without_schemes_raise(self) -> None:
        payload = json.dumps({"data": [{"Mfname": "A Fund", "schemes": []}]}).encode()
        with pytest.raises(AumPayloadError, match="no schemes"):
            parse_aaum(payload)


class TestV2FinallyRuns:
    """The point of the whole slice."""

    AUM = Decimal("275350000000")  # Axis Small Cap, Rs 27,535 Cr

    def _v2(self, market_value: Decimal, basis: str) -> tuple[bool | None, str]:
        rows = [HoldingRow(None, "equity", market_value, Decimal(100), "I1")]
        checks = validate_disclosure(
            rows, date(2026, 8, 31), date(2026, 9, 13), self.AUM, basis
        )
        v2 = next(c for c in checks if c.code == "V2")
        return v2.passed, promote_or_quarantine(checks)

    def test_a_hundredfold_units_error_quarantines(self) -> None:
        """§7.2's 100x path. This is the failure V2 exists for, and until
        `scheme_aum` was built it would have loaded clean on either tier."""
        passed, status = self._v2(Decimal("31448000000000"), BASIS)
        assert passed is False
        assert status == "quarantined"

    def test_a_real_portfolio_against_a_quarterly_average_passes(self) -> None:
        """Axis Small Cap's August portfolio against the April-June average:
        14.2% apart through two months of market movement, which is not an
        error and must not quarantine."""
        passed, status = self._v2(Decimal("314480000000"), BASIS)
        assert passed is True
        assert status == "ok"

    def test_that_same_drift_would_fail_a_point_in_time_tolerance(self) -> None:
        """Why `basis` has to travel with the figure. The spec's 3% assumes a
        month-end balance; applied to an average it quarantines a correct
        disclosure, which is how a units check stops being trusted."""
        assert self._v2(Decimal("314480000000"), "point_in_time")[0] is False
        assert AUM_TOLERANCE_PCT < AUM_AVERAGE_TOLERANCE_PCT

    def test_the_wider_tolerance_still_catches_an_order_of_magnitude(self) -> None:
        """25% is chosen against what V2 detects, not against taste: a 10x
        error is 900% off and a 100x one 9,900%."""
        assert self._v2(Decimal("3144800000000"), BASIS)[0] is False

    def test_no_aum_still_records_a_check_that_did_not_run(self) -> None:
        """V1-48. Absent a witness V2 is `None`, not `True`."""
        rows = [HoldingRow(None, "equity", Decimal(100), Decimal(100), "I1")]
        checks = validate_disclosure(rows, date(2026, 8, 31), date(2026, 9, 13), None)
        assert next(c for c in checks if c.code == "V2").passed is None


# --------------------------------------------------------------------------
# The LOAD half. V1-50.
#
# Everything above tests the pure parser. `jobs/fetch_aum.py` -- the family
# grouping, the amfi_code join, the per-member write -- had no test at all, and
# that is exactly why V1-49 shipped a dict keyed on `amfi_code` that silently
# dropped 3,940 schemes. These build a real `scheme` table and load against it.
# --------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE scheme (
  scheme_id TEXT PRIMARY KEY, amfi_code TEXT, amc_id TEXT,
  scheme_name TEXT, scheme_family TEXT
);
CREATE TABLE scheme_aum (
  scheme_id TEXT NOT NULL, as_of_date DATE NOT NULL, aum_inr DECIMAL_TEXT,
  folio_count BIGINT, basis TEXT NOT NULL DEFAULT 'point_in_time',
  period_label TEXT, source_file_id TEXT, ingested_at TIMESTAMP,
  PRIMARY KEY (scheme_id, as_of_date)
);
"""


def _warehouse(
    path: Path, schemes: list[tuple[str, str, str, str | None]]
) -> sqlite3.Connection:
    """`schemes` are (scheme_id, amfi_code, amc_id, scheme_family)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = connect(str(path))
    conn.executescript(SCHEMA)
    for scheme_id, code, amc, family in schemes:
        conn.execute(
            "INSERT INTO scheme VALUES (?,?,?,?,?)",
            (scheme_id, code, amc, scheme_id, family),
        )
    conn.commit()
    return conn


def _aaum(code: str, name: str, lakh: str) -> SchemeAaum:
    return SchemeAaum(
        amfi_code=code, scheme_name=name, amc_name="A Fund", aaum_raw=Decimal(lakh)
    )


class TestOneAmfiCodeCanNameSeveralSchemes:
    """THE defect V1-49 shipped. An AMFI scheme code is not one scheme: 4,592
    of them name two ISINs in the real warehouse, because an IDCW payout and an
    IDCW reinvest share a code. Indexed into a dict keyed on the code, every
    duplicate but the last was discarded and 3,940 schemes lost their witness.
    """

    def test_every_scheme_a_code_names_gets_a_row(self, tmp_path: Path) -> None:
        conn = _warehouse(
            tmp_path / "w.db",
            [
                ("INF209K01157", "100034", "abc", "abc|Large Mid Cap"),
                ("INF209K01CE5", "100034", "abc", "abc|Large Mid Cap"),
            ],
        )
        counts = load_quarter(
            conn, [_aaum("100034", "ABC Large & Mid", "1000")], "April - June 2026", "f1"
        )

        got = {r[0] for r in conn.execute("SELECT scheme_id FROM scheme_aum")}
        assert got == {"INF209K01157", "INF209K01CE5"}, (
            "a code naming two schemes must give both a witness"
        )
        assert counts["scheme_aum_rows"] == 2

    def test_the_money_is_counted_once_and_the_members_severally(
        self, tmp_path: Path
    ) -> None:
        """The distinction the dict lost. One payload row is ONE share class's
        assets however many ISINs it is listed under, so it contributes once --
        but every ISIN is a member and gets the family's total."""
        conn = _warehouse(
            tmp_path / "w.db",
            [
                ("INF001", "100034", "abc", "abc|Fund"),
                ("INF002", "100034", "abc", "abc|Fund"),
                ("INF003", "100099", "abc", "abc|Fund"),
            ],
        )
        load_quarter(
            conn,
            [_aaum("100034", "ABC IDCW", "1000"), _aaum("100099", "ABC Growth", "500")],
            "April - June 2026",
            "f1",
        )
        stored = dict(conn.execute("SELECT scheme_id, aum_inr FROM scheme_aum"))
        assert set(stored) == {"INF001", "INF002", "INF003"}
        # 1500 lakh, counted once, shared by all three members of one family.
        assert {Decimal(v) for v in stored.values()} == {Decimal(1500) * 100000}

    def test_a_code_no_scheme_knows_is_counted_not_dropped(self, tmp_path: Path) -> None:
        conn = _warehouse(tmp_path / "w.db", [("INF001", "100034", "abc", None)])
        counts = load_quarter(
            conn,
            [_aaum("100034", "ABC", "1000"), _aaum("999999", "Not ours", "50")],
            "April - June 2026",
            "f1",
        )
        assert counts["unmatched_amfi_codes"] == 1
        assert counts["scheme_aum_rows"] == 1


class TestTheLoadIsDeterministic:
    """CLAUDE.md invariant 10: a full rebuild must reproduce byte-identical
    output. The first version's unordered SELECT behind a collapsing dict gave
    neither the same rows nor the same choice between them."""

    def test_two_runs_write_the_same_rows(self, tmp_path: Path) -> None:
        schemes = [(f"INF{i:03d}", "100034", "abc", "abc|Fund") for i in range(6)]
        rows = [_aaum("100034", "ABC", "1000")]

        first = _warehouse(tmp_path / "a" / "w.db", list(schemes))
        second = _warehouse(tmp_path / "b" / "w.db", list(reversed(schemes)))

        load_quarter(first, rows, "April - June 2026", "f1")
        load_quarter(second, rows, "April - June 2026", "f1")
        q = "SELECT scheme_id, aum_inr FROM scheme_aum ORDER BY scheme_id"
        assert first.execute(q).fetchall() == second.execute(q).fetchall()

    def test_a_rerun_is_idempotent_and_reports_no_restatement(
        self, tmp_path: Path
    ) -> None:
        conn = _warehouse(tmp_path / "w.db", [("INF001", "100034", "abc", None)])
        rows = [_aaum("100034", "ABC", "1000")]
        load_quarter(conn, rows, "April - June 2026", "f1")
        again = load_quarter(conn, rows, "April - June 2026", "f1")

        assert again["restated"] == 0
        assert conn.execute("SELECT COUNT(*) FROM scheme_aum").fetchone()[0] == 1

    def test_a_changed_figure_is_counted_rather_than_overwritten_in_silence(
        self, tmp_path: Path
    ) -> None:
        """`INSERT OR REPLACE` overwrites in place, which every other fact table
        here refuses to do. MODULE_0 §4 gives `scheme_aum` no revision column so
        the overwrite stands -- but a figure that MOVED is news."""
        conn = _warehouse(tmp_path / "w.db", [("INF001", "100034", "abc", None)])
        load_quarter(conn, [_aaum("100034", "ABC", "1000")], "April - June 2026", "f1")
        counts = load_quarter(
            conn, [_aaum("100034", "ABC", "1200")], "April - June 2026", "f2"
        )
        assert counts["restated"] == 1


class TestTheWitnessIsBounded:
    """V1-50. The first `aum_for` took the newest row on or before the date with
    no floor, so a 2027 disclosure would reconcile against a June 2026 average
    at a tolerance chosen for one quarter of drift."""

    def _with_aum(self, path: Path, as_of: str) -> sqlite3.Connection:
        conn = _warehouse(path, [("INF001", "100034", "abc", None)])
        conn.execute(
            "INSERT INTO scheme_aum VALUES ('INF001',?, '1000', NULL,"
            " 'quarterly_average', 'q', 'f1', 'now')",
            (date.fromisoformat(as_of),),
        )
        conn.commit()
        return conn

    def test_a_recent_witness_is_returned_with_its_date(self, tmp_path: Path) -> None:
        conn = self._with_aum(tmp_path / "w.db", "2026-06-30")
        found = aum_for(conn, "INF001", date(2026, 8, 31))
        assert found is not None
        assert found.basis == "quarterly_average"
        assert found.as_of == date(2026, 6, 30)
        assert found.age_days(date(2026, 8, 31)) == 62

    def test_a_witness_older_than_the_bound_is_refused(self, tmp_path: Path) -> None:
        """Refusing beats passing: V2 records "no AUM on record" and says so,
        where a stale witness silently degrades a check nobody knows about."""
        conn = self._with_aum(tmp_path / "w.db", "2026-06-30")
        edge = date(2026, 6, 30) + timedelta(days=WITNESS_MAX_AGE_DAYS)
        assert aum_for(conn, "INF001", edge) is not None
        assert aum_for(conn, "INF001", edge + timedelta(days=1)) is None

    def test_v2_records_how_old_the_witness_was(self) -> None:
        """`basis` says what KIND of number it is; only the date says whether it
        still describes the fund being checked."""
        checks = validate_disclosure(
            [HoldingRow(None, "equity", Decimal("100000000"), Decimal(100), "I1")],
            date(2026, 8, 31),
            date(2026, 9, 13),
            Decimal("100000000"),
            "quarterly_average",
            date(2026, 6, 30),
        )
        v2 = next(c for c in checks if c.code == "V2")
        assert "2026-06-30" in v2.message
        assert "62d" in v2.message


class TestPublishedWalksYears:
    """A financial year listed before its first quarter is published must not
    kill the run. AAUM arrives ~10 days after a quarter ends, so between April
    and mid-July the newest year can hold nothing -- and the first version took
    `years[0]` unconditionally and raised."""

    class _Client:
        """Serves AMFI's three payloads from memory. No network."""

        def __init__(self, periods: dict[int, list[tuple[int, str]]]) -> None:
            self.periods = periods
            self.calls: list[str] = []

        def request(self, method: str, url: str, **kw: object) -> Any:
            self.calls.append(url)
            body: dict[str, Any]
            if "fyId=" in url:
                fy = int(url.split("fyId=")[1].split("&")[0])
                body = {
                    "type": "periods",
                    "data": {
                        "financial_year": f"FY{fy}",
                        "periods": [
                            {"id": i, "period": label}
                            for i, label in self.periods.get(fy, [])
                        ],
                    },
                }
            else:
                body = {
                    "data": [
                        {"id": fy, "financial_year": f"FY{fy}"}
                        for fy in sorted(self.periods)
                    ]
                }
            return _Response(json.dumps(body).encode())

    def test_an_empty_newest_year_falls_through_to_the_next(self) -> None:
        client = self._Client({1: [], 2: [(1, "January - March 2026")]})
        quarters = published(_CFG, years=1, client=client)
        assert [q.ends for q in quarters] == [date(2026, 3, 31)]

    def test_only_as_many_years_as_asked_for_are_walked(self) -> None:
        client = self._Client(
            {1: [(1, "April - June 2026")], 2: [(1, "January - March 2026")]}
        )
        published(_CFG, years=1, client=client)
        assert sum("fyId=" in c for c in client.calls) == 1, (
            "one year costs two requests and should stop there"
        )

    def test_no_year_with_any_quarter_raises(self) -> None:
        with pytest.raises(AumPayloadError, match="published quarter"):
            published(_CFG, years=1, client=self._Client({1: [], 2: []}))

    def test_a_quarter_is_addressed_by_its_end_not_by_a_period_id(self) -> None:
        """`period_id` restarts at 1 in every financial year, so `--period 1`
        would mean a different quarter depending on when it ran."""
        client = self._Client(
            {1: [(1, "April - June 2026")], 2: [(1, "January - March 2026")]}
        )
        quarters = published(_CFG, years=2, client=client)
        assert [q.period_id for q in quarters] == [1, 1], "ids collide, as expected"
        assert [str(q.ends) for q in quarters] == ["2026-06-30", "2026-03-31"]


@dataclass(frozen=True)
class _Response:
    """What `conditional_get` touches on an httpx response.

    `status_code` matters: the retry loop returns only below 500, so a stub
    without one fails inside the loop rather than in the code under test.
    """

    content: bytes
    status_code: int = 200

    def raise_for_status(self) -> None:
        return None


#: Politeness settings for the in-memory client. No host is contacted.
_CFG: dict[str, Any] = {
    "user_agent": "test",
    "timeout_connect": 1,
    "timeout_read": 1,
    "retries": 0,
    "rate_limit_per_sec": 1000,
    "burst": 1000,
    "respect_robots": False,
}
