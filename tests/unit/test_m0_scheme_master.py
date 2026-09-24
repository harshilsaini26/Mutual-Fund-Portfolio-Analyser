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


HEADER = SAMPLE.read_text(encoding="utf-8").splitlines()[0]


def _master(*rows: str) -> str:
    return "".join(f"{line}\r\n" for line in (HEADER, *rows))


def test_one_unreadable_launch_date_costs_that_fund_not_the_file() -> None:
    parsed = parse_scheme_master(_master(
        "A,1,Good Fund,Open Ended,Cat,Good Fund - Growth,500,02-Jan-2000,02-Jan-2000,"
        "INF000A01011",
        "A,2,Bad Fund,Open Ended,Cat,Bad Fund - Growth,500,31-Sept-2014,,INF000A01029",
    ))
    assert parsed["INF000A01011"] == ("Good Fund", date(2000, 1, 2))
    assert parsed["INF000A01029"] == ("Bad Fund", None)


def test_an_isin_listed_under_two_funds_is_left_out() -> None:
    """Five do in the live file, all old fixed-horizon series: filing one under
    whichever row came last could serve it the other series' portfolio."""
    parsed = parse_scheme_master(_master(
        "A,1,Series 7,Close Ended,Cat,Series 7 - Growth,500,19-Sep-2014,,INF000A01011",
        "A,2,Series 8,Close Ended,Cat,Series 8 - Growth,500,13-Oct-2014,,INF000A01011",
        "A,3,Series 8,Close Ended,Cat,Series 8 - IDCW,500,13-Oct-2014,,INF000A01029",
    ))
    assert "INF000A01011" not in parsed
    assert parsed["INF000A01029"][0] == "Series 8"


def test_a_moved_isin_column_is_refused_not_read_as_nothing() -> None:
    """The ISINs are read by position, so the header is checked through them."""
    moved = HEADER.replace(",ISIN Div Payout", ",Riskometer,ISIN Div Payout")
    with pytest.raises(ParseFailed, match="header"):
        parse_scheme_master(_master().replace(HEADER, moved))


def test_a_blank_in_a_later_master_keeps_what_an_earlier_one_supplied(
    conn: sqlite3.Connection, parsed: dict[str, tuple[str, date | None]]
) -> None:
    load_scheme_master(conn, parsed)
    load_scheme_master(conn, {DIRECT: ("", None)})
    assert conn.execute(
        "SELECT fund_name, inception_date FROM scheme WHERE scheme_id = ?", (DIRECT,)
    ).fetchone() == (FUND, date(2000, 1, 2))


# --- the daily job ------------------------------------------------------------


class _Response:
    def __init__(self, content: bytes, status_code: int = 200) -> None:
        self.status_code, self.content = status_code, content
        self.headers = {"content-type": "text/plain"}

    def raise_for_status(self) -> None:
        pass


def _run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, master_fails: bool,
    nav_unchanged: bool = False,
) -> tuple[dict[str, object], sqlite3.Connection]:
    """`jobs.fetch_nav.run` on a fresh warehouse, the network faked: AMFI's
    NAVAll sample (or a 304 for it), and a master naming one of its schemes'
    fund."""
    import jobs.fetch_nav

    monkeypatch.setenv("MF_DATA_ROOT", str(tmp_path))
    # One clean row: the full sample's own warnings would make every run
    # `partial` and hide whether the master's failure did.
    sample = (FIXTURES / "navall_sample.txt").read_text(encoding="utf-8").splitlines()
    navall = "\n".join([
        sample[0], "", "Open Ended Schemes(Equity Scheme - Flexi Cap Fund)", " ",
        "Axis Mutual Fund", " ", next(line for line in sample if "INF846K01WJ1" in line),
    ]).encode()
    master = _master(
        "Axis,135759,Axis Childrens Fund,Open Ended,Cat,Axis Children's Fund - Regular"
        " Growth,5000,08-Dec-2015,,INF846K01WJ1"
    ).encode()

    def fake_get(url: str, **_: object) -> _Response:
        if "NAVAll" in url:
            return _Response(navall, 304 if nav_unchanged else 200)
        if master_fails:
            raise OSError("portal down")
        return _Response(master)

    monkeypatch.setattr(jobs.fetch_nav, "conditional_get", fake_get)
    summary = jobs.fetch_nav.run()
    return summary, connect(str(tmp_path / "warehouse" / "canonical.db"))


def test_a_scheme_listed_for_the_first_time_today_gets_its_fund_today(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The master is applied AFTER the NAV load: before it, a fresh warehouse's
    scheme table was empty and the first run named no fund at all."""
    summary, wh = _run(tmp_path, monkeypatch, master_fails=False)
    assert summary["scheme_master"] == {"schemes": 1}
    assert wh.execute(
        "SELECT fund_name, inception_date FROM scheme WHERE scheme_id = 'INF846K01WJ1'"
    ).fetchone() == ("Axis Childrens Fund", date(2015, 12, 8))


def test_a_failed_master_leaves_the_prices_loaded_and_says_partial(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    summary, wh = _run(tmp_path, monkeypatch, master_fails=True)
    assert str(summary["scheme_master"]).startswith("failed, prices still loaded")
    assert summary["status"] == "partial"
    assert wh.execute("SELECT count(*) FROM nav_daily").fetchone()[0] > 0


@pytest.mark.parametrize(
    ("master_fails", "status"), [(False, "skipped"), (True, "partial")]
)
def test_a_failed_master_is_in_the_job_status_not_just_the_printout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, master_fails: bool, status: str
) -> None:
    """On an unchanged NAV day (304) nothing else can make the run `partial`,
    so the status is the master's alone -- and a scheduler reads the status."""
    summary, _ = _run(tmp_path, monkeypatch, master_fails, nav_unchanged=True)
    assert summary["status"] == status

