"""The contract M0 owes M1. MODULE_0.md §10.3, asserting §11.4.

Runs after every M0 load and before any M1 rebuild. Its whole value is turning
a class of silent corruption into a loud failure, so the tests are about what it
CATCHES, not about it passing on clean data.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.m0_data.load import load_parse_result
from src.m0_data.parse.nav.amfi import parse_navall
from src.m0_data.schema.apply import apply_migrations
from src.m0_data.validate.integrity import (
    MAX_NAV_GAP_BUSINESS_DAYS,
    assert_m1_contract,
    business_days_between,
    nav_gaps,
)

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "m0" / "navall_sample.txt"
AS_OF = date(2026, 9, 4)
HDFC_DIRECT = "INF179K01UT0"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "canonical.db"
    apply_migrations(str(db))
    connection = connect(str(db))
    parsed = parse_navall(SAMPLE.read_text(encoding="utf-8").splitlines())
    load_parse_result(connection, parsed, "file-1", AS_OF)
    connection.commit()
    yield connection
    connection.close()


def test_weekend_days_do_not_count_towards_a_nav_gap() -> None:
    """A Friday-to-Monday gap is three calendar days and no missing session."""
    assert business_days_between(date(2026, 9, 4), date(2026, 9, 7)) == 0
    assert business_days_between(date(2026, 9, 4), date(2026, 9, 11)) == 4


def test_a_long_gap_in_a_held_scheme_is_a_violation(
    conn: sqlite3.Connection,
) -> None:
    """§11.4: no gaps > 3 business days for held schemes.

    XIRR needs a NAV on every cashflow date, and a position valued at a NAV
    from three weeks earlier is wrong in a way nothing downstream can detect.
    """
    conn.execute(
        "INSERT INTO nav_daily (scheme_id, nav_date, nav) VALUES (?, ?, ?)",
        (HDFC_DIRECT, date(2026, 8, 1), Decimal("2200.000")),
    )
    conn.commit()

    gaps = nav_gaps(conn, HDFC_DIRECT, date(2026, 1, 1), AS_OF)
    assert gaps, "one NAV a month apart from another must register as a gap"
    assert all(span > MAX_NAV_GAP_BUSINESS_DAYS for _a, _b, span in gaps)

    report = assert_m1_contract(conn, [HDFC_DIRECT], date(2026, 1, 1), AS_OF)
    assert not report.passed
    assert any("NAV gap" in v for v in report.violations)


def test_a_held_scheme_missing_from_the_master_is_a_violation(
    conn: sqlite3.Connection,
) -> None:
    report = assert_m1_contract(conn, ["INF000X01ZZ9"], date(2026, 1, 1), AS_OF)
    assert not report.passed
    assert "not in the scheme master" in report.violations[0]


def test_a_pre_2018_holding_without_the_grandfathering_nav_is_a_violation(
    conn: sqlite3.Connection,
) -> None:
    """31-Jan-2018 is not optional. OPEN-07, and MODULE_1.md §7.5.

    Grandfathered cost is `max(actual, min(FMV_31Jan2018, sale_price))`. Without
    that day's NAV `effective_cost` cannot compute it and marks the consumption
    `confidence=low` — so one missing NAV degrades the tax position of every
    pre-2018 lot, which is the holding most likely to carry a large gain.
    """
    report = assert_m1_contract(conn, [HDFC_DIRECT], date(2017, 1, 1), AS_OF)
    assert any("31-Jan-2018" in v for v in report.violations)

    conn.execute(
        "INSERT INTO nav_daily (scheme_id, nav_date, nav) VALUES (?, ?, ?)",
        (HDFC_DIRECT, date(2018, 1, 31), Decimal("500.000")),
    )
    conn.commit()
    later = assert_m1_contract(conn, [HDFC_DIRECT], date(2017, 1, 1), AS_OF)
    assert not any("31-Jan-2018" in v for v in later.violations)


def test_a_cyclic_merger_chain_is_caught_and_does_not_hang(
    conn: sqlite3.Connection,
) -> None:
    """§11.4: merger chains must be complete and acyclic.

    A cycle would make lot carry-forward loop forever, so the check must
    terminate on the bad data rather than reproduce the bug it is detecting.
    """
    other = "INF179K01608"
    update = "UPDATE scheme SET merged_into = ? WHERE scheme_id = ?"
    conn.execute(update, (other, HDFC_DIRECT))
    conn.execute(update, (HDFC_DIRECT, other))
    conn.commit()

    report = assert_m1_contract(conn, [HDFC_DIRECT], date(2026, 1, 1), AS_OF)
    assert any("cyclic merger chain" in v for v in report.violations)


def test_every_violation_is_collected_rather_than_the_first_raising(
    conn: sqlite3.Connection,
) -> None:
    """A departure from §10.3's sketch, and the reason for it.

    §10.3 raises inside the loop, so the first bad scheme ends the check and
    every later one stays hidden until it is fixed. Collecting means one run
    reports everything that is wrong.
    """
    report = assert_m1_contract(
        conn, ["INF000X01ZZ9", "INF000X01YY7"], date(2026, 1, 1), AS_OF
    )
    assert len(report.violations) == 2


def test_the_gate_names_what_it_did_not_check(
    conn: sqlite3.Connection,
) -> None:
    """A gate that quietly checks less than it claims is worse than none.

    `scheme_tax_class` and `scheme_ter` have no source in V0.4, so asserting
    them would be asserting the absence of a table.
    """
    report = assert_m1_contract(conn, [HDFC_DIRECT], date(2026, 1, 1), AS_OF)
    assert any("tax_class" in n for n in report.not_checked)
    assert any("scheme_ter" in n for n in report.not_checked)
