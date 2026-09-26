"""Every live fund's history, fetched only where it is short. `jobs/backfill_scheme_nav`.

The daily build's history step (DECISIONS V1-75) runs this over ~1,860 funds, so
two properties matter more than any single fetch: a fund that fails is recorded
and the run carries on, and a fund whose history is complete is not fetched
again. No network: the HTTP layer is replaced with canned responses.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from jobs import backfill_scheme_nav as job
from src.common.decimals import connect
from src.m0_data.fetch.base import RobotsCache

from tests.conftest import migrated

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "m0"
SAMPLE = FIXTURES / "mfapi_118955_sample.json"
NEWEST = date(2026, 9, 23)


def cov(**kw: Any) -> job.Coverage:
    base: dict[str, Any] = {
        "first": date(2013, 1, 2), "last": NEWEST, "rows": 3300,
        "recent_gap": 1, "inception": date(2013, 1, 1),
    }
    return job.Coverage(**{**base, **kw})


def test_a_complete_history_is_left_alone() -> None:
    assert job.short_reason(cov(), NEWEST) is None


@pytest.mark.parametrize(("change", "reason"), [
    ({"first": None, "last": None, "rows": 0}, "no prices"),
    ({"last": NEWEST - timedelta(days=9)}, "behind"),
    ({"first": date(2018, 1, 31)}, "launch"),
    # Launched this month and priced on two days: the daily file, not a history.
    ({"first": date(2026, 9, 5), "rows": 2, "inception": date(2026, 9, 4)},
     "daily file"),
    ({"recent_gap": 9}, "gap"),
])
def test_each_way_a_history_can_be_short(change: dict[str, Any], reason: str) -> None:
    found = job.short_reason(cov(**change), NEWEST)
    assert found is not None and reason in found


def test_a_fund_launched_after_2013_is_measured_from_its_launch() -> None:
    launched = date(2021, 6, 1)
    # Five years and a quarter of weekdays, less holidays: about 1,300 prices.
    young = cov(first=launched + timedelta(days=3), inception=launched, rows=1300)
    assert job.short_reason(young, NEWEST) is None


class _Response:
    def __init__(self, body: bytes, status: int = 200) -> None:
        self.content = body
        self.status_code = status

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            request = httpx.Request("GET", "https://example.invalid/")
            raise httpx.HTTPStatusError(
                f"{self.status_code} for the fake url", request=request,
                response=httpx.Response(self.status_code, request=request),
            )


@pytest.fixture
def warehouse(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "w.db"
    migrated(db)
    conn = connect(str(db))
    for sid, code in (("INF179K01UT0", "118955"), ("INF000000MIS", "404404"),
                      ("INF000000BAD", "500500")):
        conn.execute(
            "INSERT INTO scheme (scheme_id, amfi_code, scheme_name, plan, option,"
            " amc_id, status, last_seen) VALUES (?,?,?,'direct','growth','amc1',"
            " 'active', ?)", (sid, code, f"Fund {code}", NEWEST),
        )
    conn.commit()
    conn.close()
    monkeypatch.setenv("MF_WAREHOUSE", str(db))
    monkeypatch.setenv("MF_DATA_ROOT", str(tmp_path / "data"))
    monkeypatch.setattr(RobotsCache, "allows", lambda self, url, ua: True)

    def fake_get(url: str, **_: Any) -> _Response:
        if "118955" in url:
            return _Response(SAMPLE.read_bytes())
        if "404404" in url:
            return _Response(b"", 404)
        return _Response(b"not json")

    monkeypatch.setattr(job, "conditional_get", fake_get)
    return db


def test_a_failing_fund_does_not_stop_the_run(warehouse: Path) -> None:
    summaries = job.run(["INF000000MIS", "INF179K01UT0", "INF000000BAD"])
    by_id = {str(s["scheme_id"]): s for s in summaries}
    assert "HTTPStatusError" in str(by_id["INF000000MIS"]["error"])
    assert "MfapiParseError" in str(by_id["INF000000BAD"]["error"])
    assert int(str(by_id["INF179K01UT0"]["added"])) > 0

    conn = sqlite3.connect(warehouse)
    status = conn.execute(
        "SELECT status, error_text FROM job_run ORDER BY started_at DESC"
    ).fetchone()
    assert status[0] == "partial" and "INF000000MIS" in status[1]
    # The fund that loaded has its total-return series, not NULLs waiting for
    # the next daily job.
    nulls = conn.execute(
        "SELECT count(*) FROM nav_daily WHERE scheme_id = 'INF179K01UT0'"
        " AND nav_adj IS NULL"
    ).fetchone()[0]
    assert nulls == 0


def test_the_build_fetches_only_funds_whose_history_is_short(warehouse: Path) -> None:
    conn = connect(str(warehouse))
    # A dense recent history for one fund; the other two have nothing.
    day = date(2013, 1, 2)
    while day <= NEWEST:
        if day.weekday() < 5:
            conn.execute(
                "INSERT INTO nav_daily (scheme_id, nav_date, nav) VALUES (?,?,?)",
                ("INF179K01UT0", day, Decimal("10")),
            )
        day += timedelta(days=1)
    conn.commit()
    short = job.missing(conn, ["INF179K01UT0", "INF000000MIS", "INF000000BAD"])
    conn.close()
    assert short == ["INF000000MIS", "INF000000BAD"]


def test_a_fund_mfapi_answered_this_month_is_not_asked_again(warehouse: Path) -> None:
    """Found by the second full build: 206 funds stayed "short" after a full
    fetch, because mfapi's own history for them starts late or ends early, and
    every night asked for them again. A fetch within 30 days (a `raw_file` row
    'S6:<code>', carried between builds in the store) settles it -- except a
    recent gap, which a missed night makes and one fetch fills."""
    conn = connect(str(warehouse))
    day = date(2018, 1, 31)  # dense, but starting years after Direct plans began
    while day <= NEWEST:
        if day.weekday() < 5:
            conn.execute("INSERT INTO nav_daily (scheme_id, nav_date, nav)"
                         " VALUES ('INF179K01UT0', ?, 10)", (day,))
        day += timedelta(days=1)
    conn.commit()
    assert job.missing(conn, ["INF179K01UT0"], NEWEST) == ["INF179K01UT0"]

    conn.execute(
        "INSERT INTO raw_file (file_id, source_id, url, fetched_at, byte_size,"
        " storage_path) VALUES ('f', 'S6:118955', 'u', ?, 0, '')",
        (NEWEST - timedelta(days=3),),
    )
    conn.commit()
    assert job.missing(conn, ["INF179K01UT0"], NEWEST) == []
    assert job.missing(conn, ["INF179K01UT0"], NEWEST + timedelta(days=40)) == [
        "INF179K01UT0"
    ]
    # A hole in the last month is worth asking for, however recent the fetch.
    conn.execute("DELETE FROM nav_daily WHERE nav_date BETWEEN ? AND ?",
                 (NEWEST - timedelta(days=12), NEWEST - timedelta(days=2)))
    conn.commit()
    assert job.missing(conn, ["INF179K01UT0"], NEWEST) == ["INF179K01UT0"]
    conn.close()
