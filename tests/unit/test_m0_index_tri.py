"""S12: index identity, the TRI parser, its chunking, and the load. MODULE_0.md §2.1.

`scheme.benchmark_id` has been populated on 0 of 19,598 schemes since 002
created the column, because nothing existed to point it at. These are the parts
that make pointing possible.

The normalisation tests carry real strings out of the workbooks in this
archive, not invented ones. Six spellings of one NSE index is what the data
actually contains, and a matcher that handles five of them silently loses a
fund house.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.m0_data.fetch.nifty_tri import TriChunk, query_date, year_chunks
from src.m0_data.load import (
    MixedIndexResponse,
    load_index_catalogue,
    load_index_levels,
)
from src.m0_data.normalise.index_id import index_id_for, index_key, is_composite
from src.m0_data.parse.base import ParseFailed
from src.m0_data.parse.index.nifty import StagedIndexLevel, parse_date, parse_tri
from src.m0_data.resolve.benchmark import (
    BenchmarkMatcher,
    resolve_scheme_benchmarks,
)

from tests.conftest import migrated


def response(rows: list[dict[str, str]], *, envelope: str = "bare") -> bytes:
    """The three shapes this endpoint answers in. See the parser's docstring."""
    if envelope == "bare":
        return json.dumps(rows).encode()
    if envelope == "d_list":
        return json.dumps({"d": rows}).encode()
    return json.dumps({"d": json.dumps(rows)}).encode()


def row(
    day: str, level: str, *, name: str = "Nifty 50", net: str | None = None
) -> dict[str, str]:
    out = {"RequestNumber": "TRI1", "Index Name": name, "Date": day,
           "TotalReturnsIndex": level}
    if net is not None:
        out["NTR_Value"] = net
    return out


# --- index identity ----------------------------------------------------------


@pytest.mark.parametrize(
    "written",
    [
        "NIFTY MIDCAP 150 TRI",
        "Nifty Midcap 150 (TRI)",
        "Nifty Midcap150 - TRI",
        "NIFTY MIDCAP 150 Total Return Index",
        "Nifty Midcap 150 Index",
        "NIFTY MIDCAP150",
        "Nifty Midcap 150",
    ],
)
def test_every_spelling_of_one_index_agrees(written: str) -> None:
    """All seven are in this archive or this warehouse. NSE calls it
    `NIFTY MIDCAP 150`, and every fund benchmarked to it must reach that."""
    assert index_key(written) == index_key("NIFTY MIDCAP 150")


def test_a_tri_marker_inside_brackets_still_strips() -> None:
    """Kotak writes `Nifty Alpha 50 Index (Total Return Index (TRI))`. Removing
    the marker leaves `nifty alpha 50 index ( )`, so a trailing-INDEX rule
    anchored on the end of the string never fires and the key keeps a word NSE
    does not use. That one mismatch would cost every Kotak index fund its
    benchmark."""
    assert index_key("Nifty Alpha 50 Index (Total Return Index (TRI))") == index_key(
        "NIFTY ALPHA 50"
    )


@pytest.mark.parametrize(
    "name",
    [
        "NIFTY INDIA FPI 150",              # 'INDIA' must survive
        "NIFTY500 MULTICAP 50:25:25",       # no space between letters and digits
        "NIFTY SMALLCAP 250",
    ],
)
def test_real_nse_names_survive_normalisation(name: str) -> None:
    """The stripping rules must not eat a word that belongs to the index. Each
    of these is a live NSE index whose name defeats an obvious shortcut."""
    assert index_key(name) == name.replace(" ", "").replace(":", "")


def test_a_middle_index_word_is_not_stripped() -> None:
    """Only a TRAILING 'Index' is decoration. CRISIL puts it mid-name."""
    assert "INDEX" in index_key("CRISIL-IBX AAA Financial Services Index - Sep 2027")


def test_the_id_records_total_return_and_the_key_ignores_it() -> None:
    """§9.4 needs `is_total_return` to survive into the id, because a PRI series
    must never be compared by accident. It must NOT survive into the matching
    key: a fund written against "Nifty 50" and one against "Nifty 50 TRI" are
    pointing at the same index."""
    assert index_id_for("Nifty 50") == "NSE:NIFTY_50_TRI"
    assert index_id_for("Nifty 50", is_total_return=False) == "NSE:NIFTY_50_PRI"
    assert index_key("Nifty 50") == index_key("Nifty 50 TRI")


@pytest.mark.parametrize(
    ("text", "composite"),
    [
        ("85 % Nifty 500 TRI + 15% MSCI ACWI INFORMATION TECHNOLOGY INDEX TRI", True),
        ("NIFTY 500 TRI", False),
        # A single NSE index whose own name contains "Plus" and a ratio.
        ("Nifty SDL Plus AAA PSU Bond Jul 2028 60:40 Index", False),
        ("NIFTY500 MULTICAP 50:25:25", False),
    ],
)
def test_composites_are_refused_not_resolved(text: str, composite: bool) -> None:
    """Matching `85% Nifty 500 + 15% MSCI ...` to NIFTY 500 because that is the
    leg we recognise gives an alpha measured against the wrong thing, silently.
    Invariant 5: refuse."""
    assert is_composite(text) is composite


# --- the parser --------------------------------------------------------------


@pytest.mark.parametrize("envelope", ["bare", "d_list", "d_string"])
def test_all_three_envelopes_parse(envelope: str) -> None:
    """The endpoint answers in whichever of these it feels like."""
    got = parse_tri(response([row("28 Mar 2024", "32867.23")], envelope=envelope))
    assert len(got) == 1
    assert got[0].level == Decimal("32867.23")
    assert got[0].level_date == date(2024, 3, 28)


def test_rows_come_back_oldest_first() -> None:
    """NSE answers newest-first and every consumer wants the other order."""
    got = parse_tri(response([row("28 Mar 2024", "3"), row("26 Mar 2024", "1"),
                              row("27 Mar 2024", "2")]))
    assert [p.level_date.day for p in got] == [26, 27, 28]


def test_the_net_value_is_staged_even_though_nothing_stores_it() -> None:
    """§6.3 rule 1: a parser reports what the file said. The loader drops it;
    that way the day someone wants NTR only the schema has to move."""
    got = parse_tri(response([row("28 Mar 2024", "32867.23", net="29763.42")]))
    assert got[0].net_level == Decimal("29763.42")


def test_a_missing_net_value_is_none_not_a_failure() -> None:
    assert parse_tri(response([row("28 Mar 2024", "1")]))[0].net_level is None


def test_a_row_missing_a_required_key_raises() -> None:
    """§6.3 rule 3. A shape change in the endpoint is not a gap in the data,
    and 249 days that parsed as 248 is the hole invariant 4 forbids."""
    bad = [{"Index Name": "Nifty 50", "Date": "28 Mar 2024"}]   # no level
    with pytest.raises(ParseFailed, match="missing"):
        parse_tri(response(bad))


def test_an_unreadable_level_raises() -> None:
    with pytest.raises(ParseFailed, match="unreadable"):
        parse_tri(response([row("28 Mar 2024", "not a number")]))


def test_a_non_json_response_raises() -> None:
    with pytest.raises(ParseFailed, match="not JSON"):
        parse_tri(b"<html>Error 404</html>")


@pytest.mark.parametrize("text", ["28 Foo 2024", "28-03-2024", "2024-03-28", ""])
def test_unreadable_dates_raise(text: str) -> None:
    with pytest.raises(ParseFailed):
        parse_date(text)


def test_the_month_table_is_used_not_strftime() -> None:
    """`%b` renders through LC_TIME, so a German locale would make the same
    archived bytes unparseable. Invariant 10 wants a rebuild that does not
    depend on where it runs."""
    assert parse_date("28 Mar 2024") == date(2024, 3, 28)
    assert parse_date("01 DEC 2011") == date(2011, 12, 1)


# --- chunking ----------------------------------------------------------------


def test_a_full_calendar_year_is_one_chunk() -> None:
    got = list(year_chunks("NIFTY 50", date(2024, 1, 1), date(2024, 12, 31)))
    assert len(got) == 1
    assert (got[0].start, got[0].end) == (date(2024, 1, 1), date(2024, 12, 31))


def test_a_range_splits_on_calendar_years_not_a_rolling_window() -> None:
    """A rolling 365-day window would shift with the run date, so every chunk
    would archive as a new file and the warehouse would stop rebuilding
    byte-identical (invariant 10)."""
    got = list(year_chunks("NIFTY 50", date(2022, 6, 1), date(2024, 3, 15)))
    assert [(c.start, c.end) for c in got] == [
        (date(2022, 6, 1), date(2022, 12, 31)),
        (date(2023, 1, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 3, 15)),
    ]
    assert all((c.end - c.start).days <= 365 for c in got), "wider than the server takes"


def test_a_backwards_range_raises() -> None:
    with pytest.raises(ValueError, match="precedes"):
        list(year_chunks("NIFTY 50", date(2024, 1, 1), date(2023, 1, 1)))


def test_the_query_date_does_not_go_through_the_locale() -> None:
    assert query_date(date(2024, 1, 1)) == "01-Jan-2024"
    assert query_date(date(2011, 12, 31)) == "31-Dec-2011"


def test_the_body_is_the_envelope_the_server_wants() -> None:
    """Single-quoted pseudo-JSON inside a JSON string. Proper nested JSON comes
    back EMPTY rather than erroring, so this shape is load-bearing."""
    body = TriChunk("Nifty 50", date(2024, 1, 1), date(2024, 12, 31)).body
    assert body["cinfo"] == (
        "{'name':'NIFTY 50','startDate':'01-Jan-2024'"
        ",'endDate':'31-Dec-2024','indexName':'NIFTY 50'}"
    )


# --- the load ----------------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "idx.db"
    migrated(db)
    return connect(str(db))


def staged(name: str, days: list[tuple[str, str]]) -> list[StagedIndexLevel]:
    return [
        StagedIndexLevel(
            index_name=name,
            level_date=date.fromisoformat(d),
            level=Decimal(v),
            net_level=None,
            file_id="f1",
            row_number=i,
        )
        for i, (d, v) in enumerate(days, start=1)
    ]


def test_a_load_creates_the_index_and_its_levels(conn: sqlite3.Connection) -> None:
    index_id, n = load_index_levels(
        conn, staged("Nifty 50", [("2024-01-01", "100"), ("2024-01-02", "101")]), "f1"
    )
    assert (index_id, n) == ("NSE:NIFTY_50_TRI", 2)

    # Named, not positional: a column added to the middle of the table should
    # not be able to make this assert something else and still pass.
    name, total_return = conn.execute(
        "SELECT index_name, is_total_return FROM benchmark_index"
    ).fetchone()
    assert name == "Nifty 50"             # verbatim, as the provider printed it
    assert total_return == 1
    levels = conn.execute(
        "SELECT level_date, level FROM index_level ORDER BY level_date"
    ).fetchall()
    assert [r[1] for r in levels] == [Decimal("100"), Decimal("101")]


def test_reloading_the_same_year_restates_rather_than_duplicating(
    conn: sqlite3.Connection,
) -> None:
    """NSE restates a level occasionally and the later file is the correction —
    the same rule `load_navs` follows for a restated NAV."""
    load_index_levels(conn, staged("Nifty 50", [("2024-01-01", "100")]), "f1")
    load_index_levels(conn, staged("Nifty 50", [("2024-01-01", "999")]), "f2")

    rows = conn.execute("SELECT level, source_file_id FROM index_level").fetchall()
    assert rows == [(Decimal("999"), "f2")]


def test_the_seen_span_widens_in_both_directions(conn: sqlite3.Connection) -> None:
    """Backfilling an older year must not narrow the span to that year."""
    load_index_levels(conn, staged("Nifty 50", [("2024-01-01", "1")]), "f1")
    load_index_levels(conn, staged("Nifty 50", [("2011-04-06", "2")]), "f2")
    load_index_levels(conn, staged("Nifty 50", [("2026-09-01", "3")]), "f3")

    first, last = conn.execute(
        "SELECT first_seen, last_seen FROM benchmark_index"
    ).fetchone()
    assert (str(first), str(last)) == ("2011-04-06", "2026-09-01")


def test_a_response_carrying_two_indices_raises(conn: sqlite3.Connection) -> None:
    """Filing one index's levels under another's id never surfaces: the series
    stays dense and the dates stay plausible, and a fund's tracking error is
    computed against the wrong market. Invariant 5."""
    mixed = staged("Nifty 50", [("2024-01-01", "1")]) + staged(
        "Nifty Midcap 150", [("2024-01-01", "2")]
    )
    with pytest.raises(MixedIndexResponse, match="2 indices"):
        load_index_levels(conn, mixed, "f1")


def test_an_empty_response_loads_nothing_and_does_not_invent_an_index(
    conn: sqlite3.Connection,
) -> None:
    """A year before an index existed returns no rows. That must not create a
    `benchmark_index` row with no levels behind it."""
    assert load_index_levels(conn, [], "f1") == (None, 0)
    assert conn.execute("SELECT COUNT(*) FROM benchmark_index").fetchone()[0] == 0


def test_a_price_series_cannot_land_on_the_total_return_id(
    conn: sqlite3.Connection,
) -> None:
    """The two endpoints both print the index as "Nifty 50". If the flag came
    from the name they would collide on one id and a PRI level would overwrite
    a TRI one — §9.4's exact prohibition, arriving as a silent overwrite."""
    tri, _ = load_index_levels(conn, staged("Nifty 50", [("2024-01-01", "32867")]), "f1")
    pri, _ = load_index_levels(
        conn, staged("Nifty 50", [("2024-01-01", "21725")]), "f2",
        is_total_return=False,
    )
    assert tri != pri
    assert conn.execute("SELECT COUNT(*) FROM benchmark_index").fetchone()[0] == 2
    assert conn.execute("SELECT COUNT(*) FROM index_level").fetchone()[0] == 2


# --- resolving a fund to an index --------------------------------------------


CATALOGUE = [
    ("Nifty 50", "Nifty 50"),
    ("Nifty 500", "Nifty 500"),
    ("Nifty Midcap 150", "Nifty Midcap 150"),
    ("Nifty Private Bank", "Nifty Pvt Bank"),
    ("Nifty Bank", "Nifty Bank"),
    ("Nifty500 Momentum 50", "Nifty500 Momentum 50"),
]


@pytest.fixture
def matcher() -> BenchmarkMatcher:
    return BenchmarkMatcher(
        [(index_id_for(long), long, trading) for long, trading in CATALOGUE]
    )


def test_the_longest_index_wins(matcher: BenchmarkMatcher) -> None:
    """`NIFTY50` is a substring of `NIFTY500`. Shortest-first, or any order at
    all, files every Nifty 500 fund under Nifty 50 — two different markets, and
    the error is invisible in every number downstream."""
    def named(text: str) -> str:
        got = matcher.match(text)
        assert got is not None, text
        return got.index_name

    assert named("UTI Nifty 500 Index Fund") == "Nifty 500"
    assert named("UTI Nifty 50 Index Fund") == "Nifty 50"
    assert named("Mirae Nifty500 Momentum 50 ETF") == "Nifty500 Momentum 50"


def test_a_trading_name_resolves_to_the_long_name(matcher: BenchmarkMatcher) -> None:
    """NSE publishes `Nifty Private Bank` as `Nifty Pvt Bank`, and funds use
    both. `index_key` normalises punctuation, not vocabulary, so the second
    spelling has to be carried rather than derived."""
    got = matcher.match("ICICI Prudential Nifty Pvt Bank ETF")
    assert got is not None
    assert got.index_name == "Nifty Private Bank"
    assert got.index_id == index_id_for("Nifty Private Bank")


def test_an_active_fund_matches_nothing(matcher: BenchmarkMatcher) -> None:
    """An active fund's name does not contain its benchmark, and inventing one
    from the nearest word is how `Nifty Bank` claims every banking fund."""
    for name in (
        "SBI Banking & Financial Services Fund",
        "Parag Parikh Flexi Cap Fund",
        "HDFC Balanced Advantage Fund",
    ):
        assert matcher.match(name) is None, name


def test_a_composite_benchmark_resolves_to_nothing(matcher: BenchmarkMatcher) -> None:
    """Its first recognisable leg is not its benchmark. Invariant 5."""
    assert matcher.match("85 % Nifty 500 TRI + 15% MSCI ACWI IT INDEX TRI") is None


def test_the_basis_travels_with_the_match(matcher: BenchmarkMatcher) -> None:
    """A reader deciding how much to trust an alpha wants to know whether the
    benchmark was declared by the AMC or inferred from a name."""
    inferred = matcher.match("UTI Nifty 50 Index Fund")
    declared = matcher.match("Nifty 50", basis="declared")
    assert inferred is not None and declared is not None
    assert inferred.basis == "name"
    assert declared.basis == "declared"


def test_resolve_fills_only_the_empty_ones(conn: sqlite3.Connection) -> None:
    """A benchmark an AMC declared is a fact; one inferred from a name is not.
    The inference must never overwrite the fact — the precedence
    `load_navs_where_absent` uses between a publisher and a mirror."""
    load_index_catalogue(conn, CATALOGUE)
    conn.executemany(
        "INSERT INTO scheme (scheme_id, scheme_name, plan, option, benchmark_id)"
        " VALUES (?,?,'direct','growth',?)",
        [
            ("A", "UTI Nifty 50 Index Fund", None),
            ("B", "Parag Parikh Flexi Cap Fund", None),
            ("C", "Some Fund", "NSE:NIFTY_500_TRI"),   # already declared
        ],
    )

    counts = resolve_scheme_benchmarks(conn)
    assert counts == {"considered": 2, "filled": 1}

    got = dict(conn.execute("SELECT scheme_id, benchmark_id FROM scheme").fetchall())
    assert got["A"] == "NSE:NIFTY_50_TRI"
    assert got["B"] is None, "an active fund keeps NULL rather than a guess"
    assert got["C"] == "NSE:NIFTY_500_TRI", "a declared benchmark was overwritten"


def test_the_catalogue_registers_indices_without_levels(conn: sqlite3.Connection) -> None:
    """259 registered against a handful backfilled is the normal state: a row
    says the index EXISTS, `first_seen` says whether we hold any of it."""
    assert load_index_catalogue(conn, CATALOGUE) == len(CATALOGUE)
    rows = conn.execute(
        "SELECT index_name, trading_name, is_total_return, first_seen"
        " FROM benchmark_index ORDER BY index_name"
    ).fetchall()
    assert len(rows) == len(CATALOGUE)
    assert all(r[2] == 1 for r in rows)
    assert all(r[3] is None for r in rows), "no levels fetched, so no seen span"
    assert conn.execute("SELECT COUNT(*) FROM index_level").fetchone()[0] == 0


def test_refreshing_the_catalogue_does_not_erase_a_backfill(
    conn: sqlite3.Connection,
) -> None:
    """The catalogue refresh runs on a different cadence from the backfill, so
    it must not reset the span the backfill recorded."""
    load_index_catalogue(conn, CATALOGUE)
    load_index_levels(conn, staged("Nifty 50", [("2024-01-01", "100")]), "f1")
    load_index_catalogue(conn, CATALOGUE)

    first, last = conn.execute(
        "SELECT first_seen, last_seen FROM benchmark_index WHERE index_name = 'Nifty 50'"
    ).fetchone()
    assert (str(first), str(last)) == ("2024-01-01", "2024-01-01")


def test_one_index_spelled_two_ways_is_not_two_indices(conn: sqlite3.Connection) -> None:
    """NSE mixes casing inside a single year's response: a 2011-2026 backfill of
    Nifty 50 returns both `NIFTY 50` and `Nifty 50` in one payload. A guard that
    compared raw strings refused that fetch outright — which is how this was
    found. One index, two spellings, one key."""
    mixed = staged("NIFTY 50", [("2024-01-01", "1")]) + staged(
        "Nifty 50", [("2024-01-02", "2"), ("2024-01-03", "3")]
    )
    index_id, n = load_index_levels(conn, mixed, "f1")

    assert (index_id, n) == ("NSE:NIFTY_50_TRI", 3)
    # The majority spelling is stored, so the same bytes always give one name.
    stored = conn.execute("SELECT index_name FROM benchmark_index").fetchone()[0]
    assert stored == "Nifty 50"


def test_two_real_indices_in_one_response_still_raise(conn: sqlite3.Connection) -> None:
    """The guard must keep doing its job after being taught about casing."""
    mixed = staged("Nifty 50", [("2024-01-01", "1")]) + staged(
        "NIFTY MIDCAP 150", [("2024-01-01", "2")]
    )
    with pytest.raises(MixedIndexResponse, match="2 indices"):
        load_index_levels(conn, mixed, "f1")
