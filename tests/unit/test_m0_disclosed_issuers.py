"""Issuers named by the disclosures. MODULE_0.md §8.1, DECISIONS V1-41.

The identity comes from the ISIN and only the label comes from the name. That
split is the safety argument: a wrong name is cosmetic, a wrong grouping would
be real, and the grouping is not a judgement.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.m0_data.derive.disclosed_issuers import (
    clean_name,
    derive_disclosed_issuers,
    is_informative,
    issuer_segment,
)
from src.m0_data.resolve.synthetic import match_synthetic

from tests.conftest import migrated


def test_an_isin_says_what_kind_of_thing_it_is() -> None:
    """Segments are declined where the answer belongs to someone else.

    Government paper is V1-30's decision — a state is a real borrower and
    naming it needs a choice this module should not make alone. Fund units are
    `__MFUNIT__`'s, and saying so here rather than relying on the cascade
    having run first means the order of the two can never matter.
    """
    assert issuer_segment("INE261F14PQ6") == "INE261F"     # NABARD's paper
    assert issuer_segment("US02079K3059") == "US02079K"    # Alphabet
    assert issuer_segment("IN2220240435") is None          # Maharashtra SDL
    assert issuer_segment("INF879O01068") is None          # a fund's units
    assert issuer_segment("TOO SHORT") is None


def test_the_instrument_s_details_come_off_the_borrower_s_name() -> None:
    """Every one of these is a name observed in the warehouse."""
    assert clean_name("7.45% Bharti Telecom Limited**") == "Bharti Telecom Limited"
    assert clean_name("Export Import Bank of India (21/08/2026) #") == (
        "Export Import Bank of India"
    )
    assert clean_name("ICICI Securities Limited (28/09/2026)") == (
        "ICICI Securities Limited"
    )
    assert clean_name("Reliance Retail Ventures Limited**") == (
        "Reliance Retail Ventures Limited"
    )


def test_a_name_that_describes_the_instrument_is_not_a_name() -> None:
    """An AMC writing `CP` in the name column is describing the paper, not the
    borrower. 16 segments have nothing else anywhere across 183 disclosures,
    and they stay unresolved — an issuer called `CP` is worse than none, because
    every unrelated borrower's paper would pile into it.
    """
    for useless in ("CP", "CD", "NCD", "8.11", "7.59%", "Commercial Paper",
                    "State Government Securities"):
        assert not is_informative(useless), useless
    for real in ("Bharti Telecom Limited", "Alphabet Inc A",
                 "SMALL INDUSTRIES DEVELOPMENT BANK OF INDIA(^)"):
        assert is_informative(real), real


def test_a_fund_unit_is_never_an_issuer() -> None:
    """`INF` is India's numbering for a mutual fund, so a fund inside a fund
    resolves structurally — which V1-34's name rule could not do. `Kotak
    Arbitrage Fund Direct Plan Growth` ends in `Growth`, not `Fund`, and 34
    ISINs worth Rs 17,063 Cr sat unresolved for want of a naming convention.
    """
    assert match_synthetic("Kotak Arbitrage Fund Direct Plan Growth",
                           "equity", "INF174K01LC6") == "__MFUNIT__"
    assert match_synthetic("Nippon India Money Market Fund Dir Pl-Growth",
                           "debt", "INF204K01ZP3") == "__MFUNIT__"
    # A company keeps its INE.
    assert match_synthetic("HDFC Bank Ltd.", "equity", "INE040A01034") is None


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "canonical.db"
    migrated(db)
    c = connect(str(db))
    c.execute("INSERT INTO scheme (scheme_id, scheme_name, plan, option)"
              " VALUES ('S1','A Fund','direct','growth')")
    c.execute("INSERT INTO raw_file (file_id, source_id, url, fetched_at,"
              " byte_size, storage_path, parse_status)"
              " VALUES ('f','S5:x','file://x','2026-09-13',1,'/x','ok')")
    c.execute("INSERT INTO holding_disclosure (scheme_id, as_of_date, revision,"
              " source_file_id, row_count, unresolved_mv_pct, total_mv,"
              " validation_status, ingested_at)"
              " VALUES ('S1','2026-07-31',1,'f',3,'0','0','ok','2026-09-13')")
    for n, (isin, name) in enumerate([
        # one publisher abbreviates, another names the borrower in full
        ("INE556F14MM5", "CP"),
        ("INE556F16BX0", "Small Industries Development Bank Of India"),
        # a segment with nothing usable anywhere
        ("INE824H14AB1", "CP"),
    ], start=1):
        c.execute(
            "INSERT INTO holding (scheme_id, as_of_date, revision, row_number,"
            " isin, issuer_id, instrument_raw_name, market_value,"
            " pct_normalised, instrument_class, resolution_method,"
            " source_file_id, ingested_at, is_current)"
            " VALUES ('S1','2026-07-31',1,?,?,'__UNRESOLVED__',?,'100','1',"
            " 'debt','provisional','f','2026-09-13',1)",
            (n, isin, name),
        )
    c.commit()
    yield c
    c.close()


def test_one_publisher_s_full_name_rescues_another_s_abbreviation(
    conn: sqlite3.Connection,
) -> None:
    """183 disclosures is a lot of chances for someone to write it out.

    `INE556F14MM5` is disclosed as `CP` and `INE556F16BX0` as
    `Small Industries Development Bank Of India`. Same segment, so one issuer —
    and the informative name wins for both.
    """
    summary = derive_disclosed_issuers(conn)
    assert summary["issuers"] == 1
    assert summary["unnamed_segments"] == 1

    issuer = conn.execute(
        "SELECT issuer_id, canonical_name, is_synthetic, country FROM issuer"
        " WHERE issuer_id = 'DISC:INE556F'"
    ).fetchone()
    assert issuer is not None
    assert issuer[1] == "Small Industries Development Bank Of India"
    assert issuer[2] == 0        # a real borrower, not a bucket
    assert issuer[3] == "IN"

    # Both of that borrower's instruments now point at it, including the `CP`.
    owned = {
        r[0] for r in conn.execute(
            "SELECT isin FROM instrument WHERE issuer_id = 'DISC:INE556F'"
        )
    }
    assert owned == {"INE556F14MM5", "INE556F16BX0"}
    # And the segment nobody named gained nothing.
    assert not conn.execute(
        "SELECT 1 FROM instrument WHERE isin = 'INE824H14AB1'"
    ).fetchone()


def test_deriving_twice_changes_nothing(conn: sqlite3.Connection) -> None:
    """It runs at both ends of every load, so it has to be idempotent — and
    `canonical_name` is output, which invariant 10 wants byte-identical across
    a rebuild."""
    first = derive_disclosed_issuers(conn)
    conn.commit()
    before = conn.execute(
        "SELECT issuer_id, canonical_name FROM issuer ORDER BY issuer_id"
    ).fetchall()
    second = derive_disclosed_issuers(conn)
    conn.commit()
    after = conn.execute(
        "SELECT issuer_id, canonical_name FROM issuer ORDER BY issuer_id"
    ).fetchall()
    assert before == after
    assert first["issuers"] == second["issuers"]


# --- state development loans (V1-71) -----------------------------------------


def _hold(conn: sqlite3.Connection, row: int, isin: str, name: str) -> None:
    conn.execute(
        "INSERT INTO holding (scheme_id, as_of_date, revision, row_number,"
        " isin, issuer_id, instrument_raw_name, market_value,"
        " pct_normalised, instrument_class, resolution_method,"
        " source_file_id, ingested_at, is_current)"
        " VALUES ('S1','2026-07-31',1,?,?,'__UNRESOLVED__',?,'100','1',"
        " 'debt','unresolved','f','2026-09-13',1)",
        (row, isin, name),
    )


def _issuer_of(conn: sqlite3.Connection, isin: str) -> tuple[str, str] | None:
    row = conn.execute(
        "SELECT i.issuer_id, i.canonical_name FROM instrument n"
        " JOIN issuer i ON i.issuer_id = n.issuer_id WHERE n.isin = ?",
        (isin,),
    ).fetchone()
    return (row[0], row[1]) if row else None


def test_a_state_loan_is_owed_by_that_state_s_government(
    conn: sqlite3.Connection,
) -> None:
    """The ISIN's two digits name the borrower whatever the row calls itself:
    ICICI writes `7.5% State Government Securities` and Nippon only the coupon,
    and both are Maharashtra's once the code is evidenced."""
    _hold(conn, 10, "IN2220240435", "7.5% State Government Securities")
    _hold(conn, 11, "IN2220250012", "0.0718")
    _hold(conn, 12, "IN2220230204", "State Government of Maharashtra")
    summary = derive_disclosed_issuers(conn, {"22": "Maharashtra"})
    for isin in ("IN2220240435", "IN2220250012", "IN2220230204"):
        assert _issuer_of(conn, isin) == ("STATE:IN22", "Government of Maharashtra")
    assert summary["state_issuers"] == 1
    kind = conn.execute(
        "SELECT instrument_type FROM instrument WHERE isin = 'IN2220240435'"
    ).fetchone()
    assert kind == ("sdl",)


def test_an_unevidenced_state_code_is_left_unresolved(conn: sqlite3.Connection) -> None:
    _hold(conn, 10, "IN3620180023", "8.2% State Government Securities")
    derive_disclosed_issuers(conn, {"22": "Maharashtra"})
    assert _issuer_of(conn, "IN3620180023") is None


def test_a_disclosure_naming_another_state_refuses_the_code(
    conn: sqlite3.Connection,
) -> None:
    """The evidence is re-checked on every load. A fund house naming a
    different state for the same code means one of the two is wrong, and
    neither is trusted."""
    _hold(conn, 10, "IN2220240435", "State Government of Karnataka")
    summary = derive_disclosed_issuers(
        conn, {"22": "Maharashtra", "19": "Karnataka"}
    )
    assert _issuer_of(conn, "IN2220240435") is None
    assert summary["state_conflicts"] == 1


def test_central_government_paper_is_not_a_state(conn: sqlite3.Connection) -> None:
    _hold(conn, 10, "IN0020230085", "7.18% Government of India")
    derive_disclosed_issuers(conn, {"22": "Maharashtra"})
    assert _issuer_of(conn, "IN0020230085") is None


def test_every_state_code_carries_evidence_and_names_one_state() -> None:
    from src.m0_data.config import state_isin_codes

    codes = state_isin_codes()
    assert codes["22"] == "Maharashtra" and codes["31"] == "Tamil Nadu"
    assert "00" not in codes
    assert len(set(codes.values())) == len(codes), "two codes name one state"
