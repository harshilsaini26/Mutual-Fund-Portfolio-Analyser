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
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
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
