"""Matching a sheet to the scheme it describes. MODULE_0.md §6, DECISIONS V1-38.

The tests that matter here are the refusals. A wrong match files one fund's
holdings against another fund's ISIN and looks exactly like a correct load —
the portfolio reconciles against its own total, the weights sum to 100, the
charts render, and nothing downstream can tell. So every case below that
*declines* to match is testing the property this module exists for.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.m0_data.resolve.scheme_match import (
    amc_names,
    canonical_scheme,
    detect_amc,
    identify_scheme,
    live_families,
    live_families_by_amc,
    refuse_contested,
)
from src.m0_data.schema.apply import apply_migrations


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "canonical.db"
    apply_migrations(str(db))
    c = connect(str(db))
    c.execute(
        "INSERT INTO amc (amc_id, amc_name) VALUES ('icici_prudential','ICICI')"
    )
    rows = [
        # the live fund, priced up to date
        ("INF109K015K4", "ICICI Prudential Multi Asset Allocation Fund",
         "direct", "growth", "icici prudential multi asset allocation fund"),
        ("INF109K01761", "ICICI Prudential Multi Asset Allocation Fund",
         "regular", "growth", "icici prudential multi asset allocation fund"),
        # AMFI still lists the dead institutional plan, last priced in 2020 —
        # and the AMC's own sheet says "Multi-Asset Fund", so this one is the
        # BETTER string match.
        ("INF109K01TN2", "ICICI Prudential Multi-Asset Fund- Institutional",
         "unknown", "unknown", "icici prudential multi asset fund"),
    ]
    for scheme_id, name, plan, option, family in rows:
        c.execute(
            "INSERT INTO scheme (scheme_id, scheme_name, plan, option, amc_id,"
            " status, scheme_family) VALUES (?,?,?,?,'icici_prudential',"
            " 'active', ?)",
            (scheme_id, name, plan, option, family),
        )
    for scheme_id, nav_date in (
        ("INF109K015K4", "2026-09-04"),
        ("INF109K01761", "2026-09-04"),
        ("INF109K01TN2", "2020-04-24"),
    ):
        c.execute(
            "INSERT INTO nav_daily (scheme_id, nav_date, nav, source_file_id)"
            " VALUES (?,?, '100.0', 'f')",
            (scheme_id, nav_date),
        )
    c.commit()
    yield c
    c.close()


AS_OF = date(2026, 7, 31)


def test_a_scheme_that_stopped_being_priced_is_not_a_candidate(
    conn: sqlite3.Connection,
) -> None:
    """The liveness gate, and the reason it is the first one.

    The trap is not similar names, it is DEAD names. `ICICI Prudential
    Multi-Asset Fund- Institutional` was last priced 2020-04-24 and the AMC's
    2026 sheet says "Multi-Asset Fund" — so the dead scheme is the better
    string match and the live fund is not a string match at all. Without this
    gate the sheet resolves to a six-year-old shell.
    """
    families = live_families(conn, "icici_prudential", AS_OF)
    assert "icici prudential multi asset allocation fund" in families
    assert "icici prudential multi asset fund" not in families


def test_the_live_fund_is_found_even_when_the_amc_spells_it_differently(
    conn: sqlite3.Connection,
) -> None:
    """Containment cannot bridge `Multi-Asset Fund` to `Multi Asset Allocation
    Fund` — the word `allocation` is simply not on the sheet. Fuzzy, through
    V1-02's guard, can."""
    families = live_families(conn, "icici_prudential", AS_OF)
    match = identify_scheme(
        [
            "ICICI Prudential Mutual Fund",
            "ICICI Prudential Multi-Asset Fund",
            "Portfolio as on Jul 31,2026",
        ],
        families,
    )
    assert match.family == "icici prudential multi asset allocation fund"
    assert match.method == "fuzzy"


def test_the_column_titles_do_not_drown_the_scheme_name(
    conn: sqlite3.Connection,
) -> None:
    """Header lines are scored separately, not joined.

    Measured: matching the whole header as one string made ICICI's sheet
    fuzzy-match `icici prudential psu equity fund` at 93, because everything
    matches everything once the haystack is long enough. Here the same header
    is passed with its column titles attached and must still resolve.
    """
    families = live_families(conn, "icici_prudential", AS_OF)
    match = identify_scheme(
        [
            "ICICI Prudential Mutual Fund",
            "ICICI Prudential Multi-Asset Fund",
            "Company/Issuer/Instrument Name", "ISIN", "Coupon",
            "Industry/Rating", "Quantity", "Exposure/Market Value(Rs.Lakh)",
        ],
        families,
    )
    assert match.family == "icici prudential multi asset allocation fund"


def test_a_header_naming_no_known_fund_is_refused(
    conn: sqlite3.Connection,
) -> None:
    """Roughly one sheet in six refuses on the real workbooks, mostly debt and
    index schemes headed by an internal code alone. That is an outcome, not an
    error, and it must never become a guess."""
    families = live_families(conn, "icici_prudential", AS_OF)
    match = identify_scheme(["RLMF001", "Portfolio as on 31-Jul-2026"], families)
    assert not match.matched
    assert match.method in {"unmatched", "ambiguous"}


def test_two_sheets_claiming_one_scheme_are_both_refused() -> None:
    """Within a workbook a scheme appears once, so two claims mean at least one
    is wrong — and nothing says which.

    Both real causes are here. Nippon's `Index` sheet is a contents page naming
    every fund in the workbook; Kotak's Gold Fund-of-fund names the Gold ETF it
    invests in. Neither is a near-miss a similarity threshold would catch, and
    both produce a confident, wrong answer without this rule.
    """
    from src.m0_data.resolve.scheme_match import SchemeMatch

    before = {
        "GTF": SchemeMatch("kotak gold etf", "contained"),
        "GOF": SchemeMatch("kotak gold etf", "contained"),
        "KPF": SchemeMatch("kotak pioneer fund", "contained"),
    }
    after = refuse_contested(before)
    assert not after["GTF"].matched
    assert not after["GOF"].matched
    assert after["KPF"].family == "kotak pioneer fund"


def test_the_scheme_chosen_from_a_family_is_stable(
    conn: sqlite3.Connection,
) -> None:
    """Any member serves the whole family after V1-37, so this only has to be
    deterministic — `holding.scheme_id` is output, and a value that moves
    between rebuilds is indistinguishable from one that is wrong (invariant 10).
    """
    family = "icici prudential multi asset allocation fund"
    first = canonical_scheme(conn, family, "icici_prudential")
    assert first == "INF109K015K4"          # direct + growth wins
    assert canonical_scheme(conn, family, "icici_prudential") == first
    assert canonical_scheme(conn, "no such family", "icici_prudential") is None

# --- V1-39, whose workbook is this ------------------------------------------


def test_the_house_is_read_off_the_funds_it_names(conn: sqlite3.Connection) -> None:
    """An AMFI scheme name begins with its fund house, so the family keys are
    naturally house-scoped and a workbook full of them votes for one AMC."""
    families = live_families_by_amc(conn, AS_OF)
    sheets = [
        ["ICICI Prudential Multi Asset Allocation Fund", "Portfolio as on"],
        ["ICICI Prudential Multi Asset Allocation Fund"],
    ]
    amc_id, tally = detect_amc(sheets, families, {})
    assert amc_id == "icici_prudential"
    assert tally["icici_prudential"] == 2


def test_the_house_s_own_name_rescues_a_file_naming_no_family(
    conn: sqlite3.Connection,
) -> None:
    """Fund names carry most files and fail on ICICI's, which says
    `Multi-Asset Fund` where AMFI says `Multi Asset Allocation Fund`. No family
    is contained, the tally is empty, and the file is skipped entirely.

    Its first header line is literally `ICICI Prudential Mutual Fund` — verbatim
    what the `amc` table holds. An exact containment of a registered fund-house
    name is not a similarity judgement and needs no guard.
    """
    families = live_families_by_amc(conn, AS_OF)
    sheets = [["ICICI Prudential Mutual Fund", "ICICI Prudential Multi-Asset Fund"]]

    without, _ = detect_amc(sheets, families, {})
    assert without is None, "the fund-name signal alone cannot see this file"

    with_houses, tally = detect_amc(sheets, families, amc_names(conn))
    assert with_houses == "icici_prudential"
    assert tally["icici_prudential"] == 1


def test_a_file_naming_two_houses_equally_is_refused(
    conn: sqlite3.Connection,
) -> None:
    """A workbook is one house's monthly disclosure. Where two are equally
    represented there is no majority, and guessing which half to believe is the
    confident wrong answer this module exists to avoid.
    """
    conn.execute("INSERT INTO amc (amc_id, amc_name) VALUES ('hdfc','HDFC Mutual Fund')")
    conn.commit()
    houses = amc_names(conn)
    families = live_families_by_amc(conn, AS_OF)
    sheets = [["ICICI Prudential Mutual Fund"], ["HDFC Mutual Fund"]]
    amc_id, tally = detect_amc(sheets, families, houses)
    assert amc_id is None
    assert tally == {"hdfc": 1, "icici_prudential": 1}


def test_a_sheet_naming_two_houses_votes_for_neither(
    conn: sqlite3.Connection,
) -> None:
    """One sheet mentioning two houses is a contents page or a fund-of-fund,
    not evidence. It abstains rather than splitting its vote."""
    conn.execute("INSERT INTO amc (amc_id, amc_name) VALUES ('hdfc','HDFC Mutual Fund')")
    conn.commit()
    sheets = [["ICICI Prudential Mutual Fund and HDFC Mutual Fund"]]
    amc_id, tally = detect_amc(sheets, live_families_by_amc(conn, AS_OF), amc_names(conn))
    assert amc_id is None
    assert tally == {}
