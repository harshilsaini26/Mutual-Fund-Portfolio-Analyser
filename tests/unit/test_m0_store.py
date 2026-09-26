"""The build's store: compact per-fund prices carried in the site. `src/m0_data/store.py`.

The daily build (DECISIONS V1-75) starts from nothing but these files, so the
store must survive a round trip exactly and must refuse -- never half-load -- a
file it cannot trust. A refused fund is fetched again; a half-loaded one would
be a history with a silent hole.
"""

from __future__ import annotations

import gzip
import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.m0_data import store
from src.m0_data.universe import Fund

from tests.conftest import migrated

FUND = Fund("INF179K01UT0", "118955", "HDFC Flexi Cap Fund",
            "Equity Scheme - Flexi Cap Fund", "direct", "growth", "hdfc")
NAVS = [(date(2026, 9, 21), Decimal("2271.324000")),
        (date(2026, 9, 22), Decimal("2275.100000")),
        (date(2026, 9, 23), Decimal("2270.987654"))]


def test_a_series_survives_the_round_trip_exactly() -> None:
    series = store.Series(FUND.scheme_id, "118955", NAVS)
    back = store.decode(store.encode(series))
    assert back == series
    assert all(isinstance(nav, Decimal) for _, nav in back.navs)


def test_an_unchanged_fund_writes_the_same_bytes() -> None:
    """No timestamp in the gzip header, so a day without new prices changes
    nothing in the published site."""
    series = store.Series(FUND.scheme_id, "118955", NAVS)
    assert store.encode(series) == store.encode(series)


def _aggregator_disclosure(conn: sqlite3.Connection, scheme: str, tier: str) -> None:
    conn.execute(
        "INSERT INTO holding_disclosure (scheme_id, as_of_date, revision,"
        " source_file_id, row_count, unresolved_mv_pct, total_mv,"
        " validation_status, ingested_at, is_current, source_tier) VALUES"
        " (?, '2026-08-31', 1, ?, 2, '8.96', '1000.50', 'ok',"
        " '2026-09-25 10:00:00+00:00', 1, ?)",
        (scheme, f"page-{scheme}", tier),
    )
    conn.execute(
        "INSERT INTO raw_file (file_id, source_id, url, fetched_at, byte_size,"
        " storage_path) VALUES (?, 'S7', ?, '2026-09-25', 512000, '/raw/x')",
        (f"page-{scheme}", f"https://groww.in/mutual-funds/{scheme.lower()}"),
    )
    conn.executemany(
        "INSERT INTO holding (scheme_id, as_of_date, revision, row_number,"
        " issuer_id, instrument_raw_name, market_value, pct_normalised,"
        " instrument_class, resolution_method, source_file_id, ingested_at,"
        " is_current) VALUES (?, '2026-08-31', 1, ?, ?, ?, ?, ?, 'equity', ?, ?,"
        " '2026-09-25', 1)",
        [(scheme, 1, "ACME", "Acme Ltd", "900.25", "89.9800", "name",
          f"page-{scheme}"),
         (scheme, 2, "__UNRESOLVED__", "Some Co", "100.25", "10.0200", "unresolved",
          f"page-{scheme}")],
    )


def test_the_aggregators_portfolios_survive_the_round_trip(tmp_path: Path) -> None:
    """V1-79: Groww's pages are read at most 100 a day, so what earlier builds
    read is carried in the site -- exactly, provenance included -- and the fund
    house's own files, which every build fetches again, are not."""
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    migrated(first)
    migrated(second)
    conn = connect(str(first))
    _aggregator_disclosure(conn, "INF000G00001", "aggregator")
    _aggregator_disclosure(conn, "INF000K00001", "amc_direct")
    conn.commit()
    assert store.save_holdings(conn, tmp_path / "site") == 1
    tables = ("raw_file", "holding_disclosure", "holding")
    raw = sqlite3.connect(str(first))  # the stored text, no converters
    wanted = {
        t: raw.execute(f"SELECT * FROM {t} WHERE {c} LIKE '%G00001'").fetchall()
        for t, c in zip(tables, ("file_id", "scheme_id", "scheme_id"), strict=True)
    }
    again = store.save_holdings(conn, tmp_path / "again")
    conn.close()
    raw.close()
    assert again == 1
    for t in tables:  # the same bytes from the same rows
        name = f"{t}.csv.gz"
        assert ((tmp_path / "site" / store.HOLDINGS_DIR / name).read_bytes()
                == (tmp_path / "again" / store.HOLDINGS_DIR / name).read_bytes())

    conn = connect(str(second))
    assert store.restore_holdings(conn, tmp_path / "site") == 1
    raw = sqlite3.connect(str(second))
    for t in tables:
        assert raw.execute(f"SELECT * FROM {t}").fetchall() == wanted[t]
    raw.close()
    # Nothing else came along, and a NULL stayed NULL.
    assert conn.execute("SELECT count(*) FROM holding_disclosure").fetchone()[0] == 1
    null = conn.execute("SELECT reported_sector FROM holding WHERE row_number = 2")
    assert null.fetchone()[0] is None
    conn.close()


def test_a_holdings_file_from_another_schema_is_refused(tmp_path: Path) -> None:
    db = tmp_path / "w.db"
    migrated(db)
    conn = connect(str(db))
    store.save_holdings(conn, tmp_path)
    (tmp_path / store.HOLDINGS_DIR / "holding.csv.gz").write_bytes(
        _gz("scheme_id,as_of_date\n"))
    with pytest.raises(store.StoreError, match="columns"):
        store.restore_holdings(conn, tmp_path)
    assert store.restore_holdings(conn, tmp_path / "nothing") == 0
    conn.close()


def _gz(text: str) -> bytes:
    return gzip.compress(text.encode("utf-8"), mtime=0)


@pytest.mark.parametrize(("content", "why"), [
    (b"not gzip at all", "gzip"),
    (_gz("2026-09-21,1.0\n"), "header"),
    (_gz("# scheme_id=X amfi_code=1\n2026-09-21\n"), "date and a price"),
    (_gz("# scheme_id=X amfi_code=1\n2026-09-21,-4\n"), "not a price"),
    (_gz("# scheme_id=X amfi_code=1\n2026-09-21,NaN\n"), "not a price"),
    (_gz("# scheme_id=X amfi_code=1\n2026-09-22,1\n2026-09-21,1\n"), "does not follow"),
    (_gz("# scheme_id=X amfi_code=1\n"), "no prices"),
])
def test_a_file_it_cannot_trust_is_refused_whole(content: bytes, why: str) -> None:
    with pytest.raises(store.StoreError, match=why):
        store.decode(content)


def _warehouse(path: Path) -> object:
    migrated(path)
    conn = connect(str(path))
    conn.execute(
        "INSERT INTO scheme (scheme_id, amfi_code, scheme_name, plan, option, amc_id,"
        " status) VALUES (?,?,?,'direct','growth','hdfc','active')",
        (FUND.scheme_id, FUND.amfi_code, FUND.name),
    )
    return conn


def test_what_one_build_saves_the_next_restores(tmp_path: Path) -> None:
    before = _warehouse(tmp_path / "a.db")
    before.executemany(  # type: ignore[attr-defined]
        "INSERT INTO nav_daily (scheme_id, nav_date, nav) VALUES (?,?,?)",
        [(FUND.scheme_id, on, nav) for on, nav in NAVS],
    )
    site = tmp_path / "site"
    assert store.save(before, site, [FUND]) == 1  # type: ignore[arg-type]

    after = _warehouse(tmp_path / "b.db")
    # Today's price from AMFI's file is already in; the store never overwrites it.
    after.execute(  # type: ignore[attr-defined]
        "INSERT INTO nav_daily (scheme_id, nav_date, nav) VALUES (?,?,?)",
        (FUND.scheme_id, date(2026, 9, 23), Decimal("2271.000000")),
    )
    done = store.restore(after, site, [FUND])  # type: ignore[arg-type]
    assert (done.funds, done.rows, done.refused) == (1, 2, [])
    rows = after.execute(  # type: ignore[attr-defined]
        "SELECT nav_date, nav FROM nav_daily ORDER BY nav_date"
    ).fetchall()
    assert [str(nav) for _, nav in rows] == ["2271.324000", "2275.100000", "2271.000000"]


def test_a_bad_file_is_reported_and_other_funds_still_load(tmp_path: Path) -> None:
    site = tmp_path / "site"
    folder = site / store.NAV_DIR
    folder.mkdir(parents=True)
    (folder / "118955.csv.gz").write_bytes(b"truncated")
    (folder / "999999.csv.gz").write_bytes(b"a fund no longer live: ignored")
    conn = _warehouse(tmp_path / "w.db")
    done = store.restore(conn, site, [FUND])  # type: ignore[arg-type]
    assert done.funds == 0
    assert done.refused and done.refused[0][0] == FUND.scheme_id


def test_a_file_naming_another_fund_is_refused(tmp_path: Path) -> None:
    site = tmp_path / "site"
    folder = site / store.NAV_DIR
    folder.mkdir(parents=True)
    other = store.Series("INF000000XYZ", "118955", NAVS)
    (folder / "118955.csv.gz").write_bytes(store.encode(other))
    conn = _warehouse(tmp_path / "w.db")
    done = store.restore(conn, site, [FUND])  # type: ignore[arg-type]
    assert done.funds == 0 and "names INF000000XYZ" in done.refused[0][1]


def test_the_last_mfapi_fetch_is_carried_between_builds(tmp_path: Path) -> None:
    first, second = tmp_path / "a.db", tmp_path / "b.db"
    migrated(first)
    migrated(second)
    conn = connect(str(first))
    conn.executemany(
        "INSERT INTO raw_file (file_id, source_id, url, fetched_at, byte_size,"
        " storage_path) VALUES (?, 'S6:118955', 'https://api.mfapi.in/mf/118955',"
        " ?, 1, '/x')",
        [("old", "2026-08-01 10:00:00+00:00"), ("new", "2026-09-25 10:00:00+00:00")],
    )
    conn.commit()
    assert store.save_fetched(conn, tmp_path) == 1
    conn.close()
    conn = connect(str(second))
    assert store.restore_fetched(conn, tmp_path) == 1
    raw = sqlite3.connect(str(second))
    assert raw.execute("SELECT source_id, CAST(fetched_at AS TEXT) FROM raw_file"
                       ).fetchall() == [("S6:118955", "2026-09-25 10:00:00+00:00")]
    raw.close()
    conn.close()
