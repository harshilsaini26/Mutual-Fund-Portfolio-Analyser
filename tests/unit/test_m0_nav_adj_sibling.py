"""`nav_adj` for an IDCW plan with no declarations on record. MODULE_0.md §9.1.

`scheme_idcw` holds 0 rows against 9,187 IDCW-option schemes, so the
reinvestment factor stayed 1 and `nav_adj` came out equal to raw NAV -- a
total-return column in name only, and every return computed from it short by
the whole distributed amount. For a daily-IDCW plan that is the entire return,
which is how a liquid fund came to report 0.00%.

A Growth option and an IDCW option of one plan hold ONE portfolio at one TER.
Their returns are identical; only the payout differs. So the Growth series is
already a complete record of what the IDCW plan earned, and 4,468 of the 4,595
IDCW schemes with NAV have such a sibling.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.m0_data.derive.nav_adj import build_nav_adj, growth_sibling

from tests.conftest import migrated

START = date(2024, 1, 1)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "nav.db"
    migrated(db)
    return connect(str(db))


def scheme(conn: sqlite3.Connection, scheme_id: str, option: str, family: str) -> None:
    conn.execute(
        "INSERT INTO scheme (scheme_id, scheme_name, plan, option, scheme_family)"
        " VALUES (?,?,?,?,?)",
        (scheme_id, scheme_id, "direct", option, family),
    )


def navs(conn: sqlite3.Connection, scheme_id: str, values: list[str]) -> None:
    conn.executemany(
        "INSERT INTO nav_daily (scheme_id, nav_date, nav) VALUES (?,?,?)",
        [
            (scheme_id, START + timedelta(days=i), Decimal(v))
            for i, v in enumerate(values)
        ],
    )
    conn.commit()


def adj(conn: sqlite3.Connection, scheme_id: str) -> list[Decimal]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT nav_adj FROM nav_daily WHERE scheme_id = ? ORDER BY nav_date",
            (scheme_id,),
        )
    ]


def test_a_flat_idcw_series_recovers_its_siblings_return(
    conn: sqlite3.Connection,
) -> None:
    """The case that produced 0.00%.

    The IDCW plan pays out everything it earns, so its NAV never moves. The
    Growth plan rose 20%. `nav_adj` must show 20%, not nothing.
    """
    scheme(conn, "G", "growth", "fam")
    scheme(conn, "D", "idcw_payout", "fam")
    navs(conn, "G", ["100", "110", "120"])
    navs(conn, "D", ["50", "50", "50"])

    build_nav_adj(conn, "D")

    # anchored on the IDCW plan's own scale, tracking the sibling's ratio
    assert adj(conn, "D") == [Decimal("50.000000"), Decimal("55.000000"),
                              Decimal("60.000000")]


def test_the_growth_plan_itself_is_untouched(conn: sqlite3.Connection) -> None:
    """§9.1: for a Growth option with no events `nav_adj == nav`. It must not
    go looking for a sibling of its own."""
    scheme(conn, "G", "growth", "fam")
    scheme(conn, "D", "idcw_payout", "fam")
    navs(conn, "G", ["100", "110"])
    navs(conn, "D", ["50", "50"])

    build_nav_adj(conn, "G")
    assert adj(conn, "G") == [Decimal("100.000000"), Decimal("110.000000")]


def test_declarations_on_record_still_win(conn: sqlite3.Connection) -> None:
    """The sibling is the fallback, not the preference. A real declaration is
    a fact about this plan; the sibling is an inference from another one."""
    scheme(conn, "G", "growth", "fam")
    scheme(conn, "D", "idcw_payout", "fam")
    navs(conn, "G", ["100", "200"])
    navs(conn, "D", ["100", "90"])
    conn.execute(
        "INSERT INTO scheme_idcw (scheme_id, record_date, amount_per_unit)"
        " VALUES ('D', ?, ?)",
        (START + timedelta(days=1), Decimal("10")),
    )
    conn.commit()

    build_nav_adj(conn, "D")
    # 100 -> 90 with a 10 payout: factor 100/90, so 90 * 100/90 = 100.
    # The sibling would have said 200. The declaration wins.
    assert adj(conn, "D")[1] == Decimal("100.000000")


def test_an_idcw_plan_with_no_sibling_is_left_alone(
    conn: sqlite3.Connection,
) -> None:
    """127 of the 4,595 are in this state. `nav_adj == nav` is wrong for them,
    but it is the old wrongness, not a new invention -- these are what a real
    S14 would still be needed for."""
    scheme(conn, "D", "idcw_payout", "lonely")
    navs(conn, "D", ["50", "50"])

    build_nav_adj(conn, "D")
    assert adj(conn, "D") == [Decimal("50.000000"), Decimal("50.000000")]
    assert growth_sibling(conn, "D") is None


def test_a_sibling_with_no_nav_does_not_count(conn: sqlite3.Connection) -> None:
    """A Growth plan in the master with no series behind it cannot settle
    anything."""
    scheme(conn, "G", "growth", "fam")
    scheme(conn, "D", "idcw_payout", "fam")
    navs(conn, "D", ["50", "50"])

    assert growth_sibling(conn, "D") is None
    build_nav_adj(conn, "D")
    assert adj(conn, "D") == [Decimal("50.000000"), Decimal("50.000000")]


def test_rows_before_the_siblings_series_keep_their_raw_nav(
    conn: sqlite3.Connection,
) -> None:
    """No sibling NAV to scale against. Saying nothing beats inventing a ratio.

    The anchor is the first date BOTH carry, so the adjusted series starts
    from the IDCW plan's own NAV there rather than from its first ever row.
    """
    scheme(conn, "G", "growth", "fam")
    scheme(conn, "D", "idcw_payout", "fam")
    conn.executemany(
        "INSERT INTO nav_daily (scheme_id, nav_date, nav) VALUES (?,?,?)",
        [("D", START, Decimal("50")), ("D", START + timedelta(days=1), Decimal("50")),
         ("D", START + timedelta(days=2), Decimal("50"))],
    )
    conn.executemany(
        "INSERT INTO nav_daily (scheme_id, nav_date, nav) VALUES (?,?,?)",
        [("G", START + timedelta(days=1), Decimal("100")),
         ("G", START + timedelta(days=2), Decimal("130"))],
    )
    conn.commit()

    build_nav_adj(conn, "D")
    got = adj(conn, "D")
    assert got[0] == Decimal("50.000000")   # before the sibling starts
    assert got[1] == Decimal("50.000000")   # the anchor
    assert got[2] == Decimal("65.000000")   # +30%, from the sibling


def test_a_gap_in_the_siblings_series_carries_the_last_ratio(
    conn: sqlite3.Connection,
) -> None:
    """The sibling does not price every day the IDCW plan does. The ratio only
    moves on a distribution, so the last known one is the right answer for a
    day the sibling missed -- not a hole."""
    scheme(conn, "G", "growth", "fam")
    scheme(conn, "D", "idcw_payout", "fam")
    navs(conn, "D", ["50", "50", "50"])
    conn.executemany(
        "INSERT INTO nav_daily (scheme_id, nav_date, nav) VALUES (?,?,?)",
        [("G", START, Decimal("100")), ("G", START + timedelta(days=2), Decimal("120"))],
    )
    conn.commit()

    build_nav_adj(conn, "D")
    assert adj(conn, "D") == [Decimal("50.000000"), Decimal("50.000000"),
                              Decimal("60.000000")]
