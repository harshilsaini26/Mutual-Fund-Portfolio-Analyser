"""mfapi scheme NAV history. MODULE_0.md §2.2, DECISIONS V1-19.

`mfapi_118955_sample.json` is HDFC Flexi Cap's real payload, trimmed to six
dates with `meta` kept verbatim — the three most recent and the three oldest, so
the fixture spans 2013 to 2026 without carrying 3,367 rows.

The tests that matter are the identity ones. This source is keyed on the AMFI
scheme *code* while the warehouse is keyed on the ISIN, and getting that mapping
wrong files one fund's NAV history under another fund's identity. Between a
Direct and a Regular plan that is worth about 10%/year (V0-05) and it is
invisible downstream: the NAVs are real, the dates are real, the fund is wrong.
"""

from __future__ import annotations

import json
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from src.m0_data.parse.nav.mfapi import (
    MfapiParseError,
    parse_mfapi,
)

from tests.conftest import migrated

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "m0"
SAMPLE = FIXTURES / "mfapi_118955_sample.json"
SCHEME = "INF179K01UT0"
CODE = "118955"


def payload() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads(SAMPLE.read_text(encoding="utf-8"))
    return loaded


def as_bytes(obj: object) -> bytes:
    return json.dumps(obj).encode("utf-8")


# --- the golden fixture ------------------------------------------------------


def test_the_real_payload_parses() -> None:
    result = parse_mfapi(SAMPLE.read_bytes(), SCHEME, CODE)
    assert len(result.navs) == 6
    assert result.scheme_name.startswith("HDFC Flexi Cap Fund")
    assert result.fund_house == "HDFC Mutual Fund"
    assert result.first == date(2013, 1, 1)
    assert result.last == date(2026, 9, 4)
    assert not result.warnings


def test_navs_are_decimal_and_keep_their_scale() -> None:
    """The payload sends NAVs as strings; they must stay strings into `Decimal`.

    `Decimal(float("2271.324"))` captures the binary error; `Decimal("2271.32400")`
    keeps both the value and the five decimals the publisher quoted. Scale is
    information about precision, not decoration.
    """
    result = parse_mfapi(SAMPLE.read_bytes(), SCHEME, CODE)
    latest = next(n for n in result.navs if n.nav_date == date(2026, 9, 4))
    assert isinstance(latest.nav, Decimal)
    assert latest.nav == Decimal("2271.324")
    assert str(latest.nav) == "2271.32400"


def test_rows_come_back_in_date_order() -> None:
    """mfapi sends newest first; the loader and every series want oldest first."""
    result = parse_mfapi(SAMPLE.read_bytes(), SCHEME, CODE)
    dates = [n.nav_date for n in result.navs]
    assert dates == sorted(dates)


# --- identity: the V0-05 error class -----------------------------------------


def test_a_payload_for_another_isin_is_refused() -> None:
    """The strongest check, because ISIN is the key we actually store under.

    Asking for one scheme and filing the answer under another's ISIN produces a
    NAV series that is entirely real and entirely the wrong fund.
    """
    with pytest.raises(MfapiParseError, match="refusing to file"):
        parse_mfapi(SAMPLE.read_bytes(), "INF109K01761", CODE)


def test_a_payload_for_another_scheme_code_is_refused() -> None:
    """The second check, for entries whose meta carries no ISIN."""
    body = payload()
    body["meta"]["scheme_code"] = 999999
    with pytest.raises(MfapiParseError, match="payload says"):
        parse_mfapi(as_bytes(body), SCHEME, CODE)


def test_the_isin_check_is_skipped_when_the_payload_omits_it() -> None:
    """Older entries carry no ISIN. Absent is not a mismatch.

    Refusing here would make the source unusable for exactly the old schemes
    whose history is the reason to fetch it.
    """
    body = payload()
    body["meta"]["isin_growth"] = None
    body["meta"]["isin_div_reinvestment"] = None
    result = parse_mfapi(as_bytes(body), SCHEME, CODE)
    assert len(result.navs) == 6


def test_an_idcw_isin_matches_too() -> None:
    """A scheme may be keyed on its reinvestment ISIN rather than its growth one."""
    body = payload()
    body["meta"]["isin_growth"] = "INF179K01AA1"
    body["meta"]["isin_div_reinvestment"] = SCHEME
    assert parse_mfapi(as_bytes(body), SCHEME, CODE).navs


# --- malformed input raises rather than returning a partial result -----------


@pytest.mark.parametrize(
    ("mutate", "match"),
    [
        (lambda b: b.update({"status": "FAILURE"}), "status"),
        (lambda b: b.pop("meta"), "no meta"),
        (lambda b: b.pop("data"), "no data"),
        (lambda b: b.update({"data": []}), "no usable NAV"),
    ],
)
def test_a_broken_payload_raises(mutate, match: str) -> None:  # type: ignore[no-untyped-def]
    """§6.3 rule 3. A partial NAV series is a silently short return calculation."""
    body = payload()
    mutate(body)
    with pytest.raises(MfapiParseError, match=match):
        parse_mfapi(as_bytes(body), SCHEME, CODE)


def test_not_json_raises() -> None:
    with pytest.raises(MfapiParseError, match="not JSON"):
        parse_mfapi(b"<html>rate limited</html>", SCHEME, CODE)


# --- rows that are not prices ------------------------------------------------


def test_a_zero_nav_is_dropped_and_reported() -> None:
    """A zero is not a price, and it is the first term of a division.

    Dropped rather than stored, and warned about rather than dropped silently —
    §6.4's rule that no row disappears without saying so.
    """
    body = payload()
    body["data"].append({"date": "01-01-2012", "nav": "0.00000"})
    result = parse_mfapi(as_bytes(body), SCHEME, CODE)
    assert all(n.nav > 0 for n in result.navs)
    assert any("non-positive" in message for _i, message in result.warnings)


def test_an_unparseable_row_is_reported_not_silently_skipped() -> None:
    body = payload()
    body["data"].append({"date": "not-a-date", "nav": "1.0"})
    body["data"].append({"date": "01-01-2011", "nav": "N.A."})
    result = parse_mfapi(as_bytes(body), SCHEME, CODE)
    assert len(result.warnings) == 2
    assert any("date" in m for _i, m in result.warnings)
    assert any("nav" in m for _i, m in result.warnings)


def test_a_duplicate_date_is_kept_once() -> None:
    """`nav_daily` is keyed on (scheme, date); two rows would be an upsert race."""
    body = payload()
    body["data"].append(dict(body["data"][0]))
    result = parse_mfapi(as_bytes(body), SCHEME, CODE)
    assert len(result.navs) == 6
    assert any("duplicate" in m for _i, m in result.warnings)


# --- the mirror never overwrites the publisher -------------------------------


def test_a_mirror_row_never_overwrites_an_amfi_row(tmp_path: Path) -> None:
    """DECISIONS V1-19, and the reason `load_navs_where_absent` exists.

    AMFI is the source of record (V1-02) and mfapi is a mirror. Measured on
    HDFC Flexi Cap they agree on 2,116 of 2,117 overlapping dates and differ on
    2026-03-12 — `2111.846` against `2111.779`. One in two thousand, and exactly
    the size of error that moves an XIRR without moving anything a reader would
    notice. So the mirror fills gaps and nothing else.
    """
    from src.common.decimals import connect
    from src.m0_data.load import load_navs, load_navs_where_absent
    from src.m0_data.parse.nav.amfi import StagedNav

    db = tmp_path / "w.db"
    migrated(db)
    conn = connect(str(db))
    conn.execute(
        "INSERT INTO scheme (scheme_id, scheme_name, plan, option)"
        " VALUES (?,?,?,?)", (SCHEME, "HDFC Flexi Cap", "direct", "growth")
    )

    contested = date(2026, 3, 12)
    load_navs(conn, [StagedNav(SCHEME, contested, Decimal("2111.779"))], "amfi-file")
    conn.commit()

    added = load_navs_where_absent(
        conn,
        [
            StagedNav(SCHEME, contested, Decimal("2111.846")),      # disagrees
            StagedNav(SCHEME, date(2013, 1, 1), Decimal("100.0")),  # a gap
        ],
        "mfapi-file",
    )
    conn.commit()

    assert added == 1, "only the gap should have been filled"

    kept = conn.execute(
        "SELECT nav, source_file_id FROM nav_daily WHERE nav_date = ?", (contested,)
    ).fetchone()
    assert kept[0] == Decimal("2111.779"), "the mirror overwrote the publisher"
    assert kept[1] == "amfi-file"

    filled = conn.execute(
        "SELECT nav, source_file_id FROM nav_daily WHERE nav_date = ?",
        (date(2013, 1, 1),),
    ).fetchone()
    assert filled[0] == Decimal("100.0")
    assert filled[1] == "mfapi-file", "a gap-filled row must cite the mirror"


def test_load_navs_still_upserts_for_the_publisher(tmp_path: Path) -> None:
    """The contrast, so the two loaders cannot be confused.

    AMFI restates, and the later publisher file is the correction — keeping the
    first value would make the warehouse disagree with the source it cites.
    """
    from src.common.decimals import connect
    from src.m0_data.load import load_navs
    from src.m0_data.parse.nav.amfi import StagedNav

    db = tmp_path / "w.db"
    migrated(db)
    conn = connect(str(db))
    conn.execute(
        "INSERT INTO scheme (scheme_id, scheme_name, plan, option)"
        " VALUES (?,?,?,?)", (SCHEME, "HDFC Flexi Cap", "direct", "growth")
    )
    on = date(2026, 3, 12)
    load_navs(conn, [StagedNav(SCHEME, on, Decimal("1.0"))], "first")
    load_navs(conn, [StagedNav(SCHEME, on, Decimal("2.0"))], "restated")
    conn.commit()
    row = conn.execute(
        "SELECT nav, source_file_id FROM nav_daily WHERE nav_date = ?", (on,)
    ).fetchone()
    assert row == (Decimal("2.0"), "restated")
