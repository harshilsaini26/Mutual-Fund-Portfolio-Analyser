"""AMFI's scheme master: one family per fund, and launch dates.

`amfi_scheme_master_sample.csv` is 13 rows cut from the real download
(`portal.amfiindia.com/DownloadSchemeData_Po.aspx?mf=0`, 2026-09-23), unedited
but for git normalising its CRLF line endings, which the parser does not care
about: both split Kotak funds, a row with no launch date and a row with no ISIN.

Kotak Banking and PSU Debt is the case this exists for. Its Direct plan is named
`Kotak Banking and PSU Debt Direct - Growth`, which `family_key` cannot reduce to
the Regular plan's family, so the Direct holder was served no portfolio while
the Regular plan's disclosure sat in the warehouse. AMFI's own "Scheme Name"
column names the fund once for every plan.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.m0_data.derive.scheme_family import (
    derive_scheme_families,
    disclosure_scheme_for,
)
from src.m0_data.load import load_scheme_master
from src.m0_data.parse.base import ParseFailed
from src.m0_data.parse.scheme_master import parse_scheme_master

from tests.conftest import migrated
from tests.unit.test_m3_weights import _insert

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "m0"
SAMPLE = FIXTURES / "amfi_scheme_master_sample.csv"
FUND = "Kotak Banking and PSU Debt Fund"
REGULAR, DIRECT = "INF174K01FO3", "INF174K01KH7"
DEBT = "Debt Scheme - Banking and PSU Fund"


@pytest.fixture(scope="module")
def parsed() -> dict[str, tuple[str, date | None]]:
    return parse_scheme_master(SAMPLE.read_text(encoding="utf-8"))


def test_every_isin_in_a_joined_field_is_read(
    parsed: dict[str, tuple[str, date | None]],
) -> None:
    """AMFI runs the payout and reinvestment ISINs together in one field."""
    assert parsed["INF174K01KI5"] == parsed["INF174K01KJ3"] == (FUND, date(2000, 1, 2))
    assert sum(1 for fund, _ in parsed.values() if fund == FUND) == 8


def test_a_missing_launch_date_is_none_and_a_row_with_no_isin_is_skipped(
    parsed: dict[str, tuple[str, date | None]],
) -> None:
    assert parsed["INF247L01EV3"][1] is None
    # 8 Banking & PSU; 6 Infrastructure (two of its rows carry no ISIN); 1 ETF.
    assert len(parsed) == 8 + 6 + 1


def test_an_unexpected_header_is_refused() -> None:
    """sources.yaml: a changed file fails loudly, never parses as a guess."""
    with pytest.raises(ParseFailed, match="header"):
        parse_scheme_master("Scheme,Name,Date\nx,y,z\n")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "w.db"
    migrated(db)
    c: sqlite3.Connection = connect(str(db))
    # Named as NAVAll names them, which is what split the family.
    for scheme_id, name, plan, category in (
        (REGULAR, "Kotak Banking and PSU Debt - Growth", "regular", DEBT),
        (DIRECT, "Kotak Banking and PSU Debt Direct - Growth", "direct", DEBT),
        ("INF178L01095", "Kotak Infrastructure & Economic Reform Fund - Standard"
         " Plan-Growth", "regular", "Equity Scheme - Sectoral/ Thematic"),
        ("INF178L01AL9", "Kotak Infrastructure & Economic Reform Fund- Direct"
         " Plan- Growth Option", "direct", "Equity Scheme - Thematic"),
    ):
        c.execute(
            "INSERT INTO scheme (scheme_id, scheme_name, plan, option, amc_id,"
            " sebi_category) VALUES (?,?,?,'growth','kotak_mahindra',?)",
            (scheme_id, name, plan, category),
        )
    _insert(c, "raw_file", file_id="f1", source_id="S5", fetched_at="2026-09-01",
            storage_path="/x")
    _insert(c, "holding_disclosure", scheme_id=REGULAR, as_of_date=date(2026, 7, 31),
            revision=1, source_file_id="f1", row_count=1, validation_status="ok",
            ingested_at="2026-09-01", is_current=1)
    c.commit()
    return c


def test_a_direct_plan_is_served_the_regular_plans_disclosure(
    conn: sqlite3.Connection, parsed: dict[str, tuple[str, date | None]]
) -> None:
    assert disclosure_scheme_for(conn, DIRECT) == DIRECT  # split: served nothing

    assert load_scheme_master(conn, parsed) == 4
    derive_scheme_families(conn)

    families = dict(conn.execute("SELECT scheme_id, scheme_family FROM scheme"))
    assert families[DIRECT] == families[REGULAR]
    assert disclosure_scheme_for(conn, DIRECT) == REGULAR
    launched = conn.execute(
        "SELECT inception_date FROM scheme WHERE scheme_id = ?", (DIRECT,)
    ).fetchone()[0]
    assert str(launched) == "2000-01-02"


def test_a_fund_whose_plans_disagree_on_category_still_does_not_merge(
    conn: sqlite3.Connection, parsed: dict[str, tuple[str, date | None]]
) -> None:
    """The coherence guard is unchanged: one name, two SEBI categories, and the
    family is refused rather than trusted -- here the Infrastructure fund,
    given two categories in this fixture's `scheme` rows."""
    load_scheme_master(conn, parsed)
    derive_scheme_families(conn)

    families = dict(conn.execute("SELECT scheme_id, scheme_family FROM scheme"))
    assert families["INF178L01095"] is None and families["INF178L01AL9"] is None
