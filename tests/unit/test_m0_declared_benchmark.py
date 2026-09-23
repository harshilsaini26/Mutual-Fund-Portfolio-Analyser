"""Declared benchmarks: what an active fund's own disclosure says it tracks. S12.

An index fund's benchmark is in its name and `resolve_scheme_benchmarks` finds
it there. An active fund's is not -- `Parag Parikh Flexi Cap Fund` is measured
against NIFTY 500 and says so nowhere in its name -- but the monthly disclosure
states it. These cover reading that statement, resolving it, and writing it.

The extraction cases are the layouts and the traps found by scanning every
workbook in the archive. The traps are the point: each is a real cell that
contains the word "benchmark" and is not a benchmark.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.m0_data.load import load_index_catalogue
from src.m0_data.normalise.index_id import index_id_for
from src.m0_data.parse.holdings.benchmark import declared_benchmarks
from src.m0_data.resolve.benchmark import (
    Benchmark,
    BenchmarkMatcher,
    apply_declared,
    resolve_declared,
    resolve_scheme_benchmarks,
)

from tests.conftest import migrated

EN_DASH = chr(0x2013)


# --- reading the statement ----------------------------------------------------


@pytest.mark.parametrize(
    ("cell", "stated"),
    [
        # Kotak, column A of the footer.
        ("Benchmark - Nifty Alpha 50 Index (Total Return Index (TRI))",
         "Nifty Alpha 50 Index (Total Return Index (TRI))"),
        # Kotak again, on some sheets, with the separator doubled.
        ("Benchmark - - Nifty 200 Value 30 Index (TRI)",
         "Nifty 200 Value 30 Index (TRI)"),
        # Nippon, column F.
        ("BENCHMARK NAME - NIFTY MIDCAP 150 TRI", "NIFTY MIDCAP 150 TRI"),
        ("Benchmark: Nifty 500 TRI", "Nifty 500 TRI"),
        (f"Benchmark {EN_DASH} Nifty 50", "Nifty 50"),
    ],
)
def test_each_labelled_form_the_archive_uses(cell: str, stated: str) -> None:
    assert declared_benchmarks([[cell]]) == [stated]


def test_hdfc_names_it_in_quotes_inside_a_riskometer_note() -> None:
    note = (
        '9) Riskometer based on Scheme Portfolio and Portfolio Benchmark'
        ' "NIFTY 500 TRI" as on Jul 31, 2026'
    )
    assert declared_benchmarks([[None, note]]) == ["NIFTY 500 TRI"]


def test_ppfas_states_it_under_a_performance_table_header() -> None:
    """The value is the cell below the header, and the `Index` column beside it
    -- the additional benchmark -- is not taken."""
    rows: list[list[object]] = [
        ["Date", "Scheme", "Benchmark", "Index"],
        ["PPFCF", "PPFCF (Direct Plan)", "Nifty 500 (TRI)", "Nifty 50 (TRI)"],
    ]
    assert declared_benchmarks(rows) == ["Nifty 500 (TRI)"]


def test_a_label_is_found_wherever_the_footer_puts_it() -> None:
    """Nippon's sits at column F, row 185 -- a scan of the first 40 rows, which
    is where a header would be, found almost nothing."""
    rows: list[list[object]] = [[None] * 6 for _ in range(200)]
    rows[184][5] = "BENCHMARK NAME - NIFTY MIDCAP 150 TRI"
    assert declared_benchmarks(rows) == ["NIFTY MIDCAP 150 TRI"]


@pytest.mark.parametrize(
    "trap",
    [
        "Benchmark Riskometer",
        "BENCHMARK RISK-O-METER",
        "Scheme & Benchmark Riskometer(s) mentioned are as per the latest details",
        "Standard Deviation( Benchmark )",
        "2) Risk O Meter and Benchmark are as per last data, refer monthly portfolio",
        "NIPPON INDIA ETF NIFTY 5 YR BENCHMARK G-SEC (An open ended scheme)",
        "Benchmark Computer Solutions Limited",
    ],
)
def test_the_word_is_not_the_label(trap: str) -> None:
    """Every one of these is a real cell in an archived workbook. A search for
    the word finds all seven; none is a statement of a benchmark."""
    assert declared_benchmarks([[trap]]) == []


def test_a_bare_benchmark_cell_needs_a_scheme_header_beside_it() -> None:
    """AMFI's market-cap list has a company whose short name is BENCHMARK, with
    another company's code below it. Without the `Scheme` condition that code
    came back as a benchmark."""
    rows: list[list[object]] = [["BENCHMARK", 18.69], ["HAMPS", 17.2]]
    assert declared_benchmarks(rows) == []


# --- resolving it -------------------------------------------------------------


CATALOGUE = [
    ("Nifty 500", "Nifty 500"),
    ("Nifty Midcap 150", "Nifty Midcap 150"),
    ("Nifty Liquid Index", "Nifty Liquid Index"),
    ("Nifty 50", "Nifty 50"),
]


@pytest.fixture
def matcher() -> BenchmarkMatcher:
    return BenchmarkMatcher([(index_id_for(n), n, t) for n, t in CATALOGUE])


def test_declared_text_is_matched_exactly_not_by_containment(
    matcher: BenchmarkMatcher,
) -> None:
    """`Nifty Liquid Index A-I` contains `Nifty Liquid Index`, a different
    index. Containment is right for a fund's NAME, which embeds an index; a
    declared benchmark IS an index, and only equality is identity."""
    assert matcher.match("Nifty Liquid Index A-I") is not None, "the hazard, shown"
    assert matcher.exact("Nifty Liquid Index A-I") is None

    got = matcher.exact("NIFTY MIDCAP 150 TRI")
    assert got is not None
    assert (got.index_name, got.basis) == ("Nifty Midcap 150", "declared")


@pytest.mark.parametrize(
    ("texts", "why"),
    [
        ([], "none stated"),
        (["85 % Nifty 500 TRI + 15% MSCI ACWI IT INDEX TRI"], "composite"),
        (["CRISIL Medium Duration Debt A-III Index"], "not an NSE index"),
        (["BSE Teck Index (Total Return Index)"], "not an NSE index"),
        (["Price of silver"], "not an NSE index"),
        (["Nifty 500 TRI", "Nifty Midcap 150 TRI"], "states two indices"),
        # One recognised, one not: the sheet has not said which is primary.
        (["Nifty 500 TRI", "CRISIL Composite Bond Index"], "not an NSE index"),
    ],
)
def test_a_sheet_that_cannot_be_resolved_says_why(
    matcher: BenchmarkMatcher, texts: list[str], why: str
) -> None:
    got, reason = resolve_declared(texts, matcher)
    assert got is None
    assert reason == why


def test_one_benchmark_stated_two_ways_is_one(matcher: BenchmarkMatcher) -> None:
    got, reason = resolve_declared(["NIFTY 500 TRI", "Nifty 500 (TRI)"], matcher)
    assert reason == "declared"
    assert got is not None and got.index_name == "Nifty 500"


# --- which statement stands ---------------------------------------------------


A = Benchmark("NSE:A_TRI", "A", "declared")
B = Benchmark("NSE:B_TRI", "B", "declared")


def test_the_newest_disclosure_states_the_benchmark() -> None:
    """SEBI moved many benchmarks in 2021; a 2019 sheet's is not in force."""
    from jobs.fetch_index import newest_per_family

    plan, conflicts = newest_per_family(
        {("kotak", "fam"): [(date(2019, 1, 31), A), (date(2026, 7, 31), B)]}
    )
    assert plan == {("kotak", "fam"): B}
    assert conflicts == []


def test_two_newest_sheets_disagreeing_leave_the_family_alone() -> None:
    from jobs.fetch_index import newest_per_family

    plan, conflicts = newest_per_family(
        {("kotak", "fam"): [(date(2026, 7, 31), A), (date(2026, 7, 31), B)]}
    )
    assert plan == {}
    assert len(conflicts) == 1


# --- writing it ---------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "declared.db"
    migrated(db)
    return connect(str(db))


def scheme(
    conn: sqlite3.Connection,
    scheme_id: str,
    family: str,
    *,
    name: str = "Some Fund",
    benchmark: str | None = None,
    amc: str = "kotak_mahindra",
) -> None:
    conn.execute(
        "INSERT INTO scheme (scheme_id, scheme_name, plan, option, scheme_family,"
        " amc_id, benchmark_id) VALUES (?, ?, 'direct', 'growth', ?, ?, ?)",
        (scheme_id, name, family, amc, benchmark),
    )


NIFTY_500 = Benchmark(index_id_for("Nifty 500"), "Nifty 500", "declared")


def test_a_declared_benchmark_reaches_every_share_class(conn: sqlite3.Connection) -> None:
    """One disclosure describes a scheme, and Direct, Regular and each IDCW
    variant hold the identical portfolio (V1-37). Another family is untouched."""
    for share_class in ("DIRECT_G", "REGULAR_G", "DIRECT_IDCW"):
        scheme(conn, share_class, "kotak_flexicap")
    scheme(conn, "ELSEWHERE", "kotak_other")

    counts = apply_declared(conn, {("kotak_mahindra", "kotak_flexicap"): NIFTY_500})

    got = dict(conn.execute("SELECT scheme_id, benchmark_id FROM scheme").fetchall())
    assert got == {
        "DIRECT_G": NIFTY_500.index_id,
        "REGULAR_G": NIFTY_500.index_id,
        "DIRECT_IDCW": NIFTY_500.index_id,
        "ELSEWHERE": None,
    }
    assert counts == {"families": 1, "schemes": 3, "was_null": 3, "changed": 0,
                      "unchanged": 0}


def test_a_declared_benchmark_replaces_an_inferred_one(conn: sqlite3.Connection) -> None:
    """The AMC's statement is a fact; a benchmark read out of a name is not."""
    scheme(conn, "INFERRED", "fam", benchmark=index_id_for("Nifty 50"))
    scheme(conn, "AGREES", "fam", benchmark=NIFTY_500.index_id)

    counts = apply_declared(conn, {("kotak_mahindra", "fam"): NIFTY_500})

    got = dict(conn.execute("SELECT scheme_id, benchmark_id FROM scheme").fetchall())
    assert got == {"INFERRED": NIFTY_500.index_id, "AGREES": NIFTY_500.index_id}
    assert (counts["changed"], counts["unchanged"]) == (1, 1)


def test_name_inference_afterwards_does_not_undo_it(conn: sqlite3.Connection) -> None:
    """Order-independent: `--resolve` only ever fills a NULL, so running it after
    `--declared` leaves the declared benchmark where it is."""
    load_index_catalogue(conn, [(n, t) for n, t in CATALOGUE])
    scheme(conn, "X", "fam", name="UTI Nifty 50 Index Fund")

    apply_declared(conn, {("kotak_mahindra", "fam"): NIFTY_500})
    resolve_scheme_benchmarks(conn)

    got = conn.execute("SELECT benchmark_id FROM scheme WHERE scheme_id = 'X'").fetchone()
    assert got[0] == NIFTY_500.index_id


def test_the_resolver_itself_uses_equality(matcher: BenchmarkMatcher) -> None:
    """The test above pins `exact`; this pins that `resolve_declared` calls it.
    Swapped for `match`, Nippon's `Nifty Liquid Index A-I` would come back as
    plain `Nifty Liquid Index` -- declared, confident, and a different index."""
    got, why = resolve_declared(["Nifty Liquid Index A-I"], matcher)
    assert got is None
    assert why == "not an NSE index"


def test_a_family_key_is_scoped_to_its_fund_house(conn: sqlite3.Connection) -> None:
    """Family keys come from scheme names with the house stripped, so two AMCs
    can share one. Kotak's disclosure must not set Nippon's benchmark."""
    scheme(conn, "KOTAK", "flexicap", amc="kotak_mahindra")
    scheme(conn, "NIPPON", "flexicap", amc="nippon")

    apply_declared(conn, {("kotak_mahindra", "flexicap"): NIFTY_500})

    got = dict(conn.execute("SELECT scheme_id, benchmark_id FROM scheme").fetchall())
    assert got == {"KOTAK": NIFTY_500.index_id, "NIPPON": None}
