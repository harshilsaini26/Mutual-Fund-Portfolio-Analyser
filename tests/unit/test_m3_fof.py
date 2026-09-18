"""Fund-of-funds expansion. MODULE_3.md §6.

Without this a fund-of-funds reports one opaque `__MFUNIT__` block and the
product does nothing at all for whoever holds one. In this warehouse 39 schemes
carry that bucket, six of them at 99-100%.

The cases that matter are the refusals. Expanding is the easy half; knowing
when NOT to -- a cycle, a missing disclosure, a chain too deep -- is what keeps
the total honest, and §6.3 makes each of them a rule.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.common.types import SchemeId
from src.m3_lookthrough.engine import assert_weights_sum_to_100
from src.m3_lookthrough.weights import (
    MAX_FOF_DEPTH,
    expand_fund_units,
    load_issuer_weights,
    materialise_weights,
)

from tests.conftest import migrated

AS_OF = date(2026, 7, 31)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "fof.db"
    migrated(db)
    return connect(str(db))


def _required(conn: sqlite3.Connection, table: str) -> list[str]:
    return [
        r[1] for r in conn.execute(f"PRAGMA table_info({table})") if r[3] and r[4] is None
    ]


def _insert(conn: sqlite3.Connection, table: str, **given: object) -> None:
    values: dict[str, object] = {c: Decimal(0) for c in _required(conn, table)}
    values.update(given)
    conn.execute(
        f"INSERT INTO {table} ({', '.join(values)})"
        f" VALUES ({','.join('?' * len(values))})",
        tuple(values.values()),
    )


def fund(
    conn: sqlite3.Connection,
    scheme_id: str,
    holdings: list[tuple[str, str, str | None]],
) -> None:
    """A scheme with one current disclosure.

    Each holding is (issuer_id, pct, isin). `isin` is the held fund for a
    `__MFUNIT__` row and None for an ordinary security.
    """
    conn.execute(
        "INSERT INTO scheme (scheme_id, scheme_name, plan, option) VALUES (?,?,?,?)",
        (scheme_id, scheme_id, "direct", "growth"),
    )
    _insert(
        conn,
        "holding_disclosure",
        scheme_id=scheme_id,
        as_of_date=AS_OF,
        revision=1,
        source_file_id=f"f-{scheme_id}",
        row_count=len(holdings),
        is_current=1,
    )
    for n, (issuer_id, pct, isin) in enumerate(holdings, start=1):
        _insert(
            conn,
            "holding",
            scheme_id=scheme_id,
            as_of_date=AS_OF,
            revision=1,
            row_number=n,
            issuer_id=issuer_id,
            instrument_raw_name=issuer_id,
            isin=isin,
            market_value=Decimal("100"),
            pct_normalised=Decimal(pct),
            instrument_class="mfunit" if issuer_id == "__MFUNIT__" else "equity",
        )
    conn.commit()


def weights_of(conn: sqlite3.Connection, scheme_id: str) -> dict[str, Decimal]:
    materialise_weights(conn, SchemeId(scheme_id), AS_OF)
    return {
        str(w.issuer_id): w.weight
        for w in load_issuer_weights(conn, SchemeId(scheme_id), AS_OF)
    }


# --- expanding ---------------------------------------------------------------


def test_a_unit_becomes_the_issuers_the_held_fund_holds(
    conn: sqlite3.Connection,
) -> None:
    """The whole point: a 40% holding in a fund that is 50/50 A and B is 20%
    of each, not 40% of nothing."""
    fund(conn, "INNER", [("A", "50", None), ("B", "50", None)])
    fund(conn, "OUTER", [("C", "60", None), ("__MFUNIT__", "40", "INNER")])

    w = weights_of(conn, "OUTER")
    assert w == {"C": Decimal(60), "A": Decimal(20), "B": Decimal(20)}
    assert "__MFUNIT__" not in w


def test_expansion_preserves_the_total(conn: sqlite3.Connection) -> None:
    """A holding of weight w is replaced by rows summing to w, so closure is
    untouched. A double-counted nested holding shows up as weights over 100 --
    §19's runbook names exactly that symptom."""
    fund(conn, "INNER", [("A", "70", None), ("B", "30", None)])
    fund(conn, "OUTER", [("C", "35", None), ("__MFUNIT__", "65", "INNER")])

    materialise_weights(conn, SchemeId("OUTER"), AS_OF)
    assert_weights_sum_to_100(
        load_issuer_weights(conn, SchemeId("OUTER"), AS_OF), SchemeId("OUTER"), AS_OF
    )


def test_an_issuer_held_directly_and_through_a_fund_is_summed(
    conn: sqlite3.Connection,
) -> None:
    """One issuer, two routes in. The collapse must add them, not let the
    nested row overwrite the direct one."""
    fund(conn, "INNER", [("A", "100", None)])
    fund(conn, "OUTER", [("A", "25", None), ("__MFUNIT__", "75", "INNER")])

    assert weights_of(conn, "OUTER") == {"A": Decimal(100)}


def test_two_levels_expand(conn: sqlite3.Connection) -> None:
    """Depth 2 is allowed, so a fund holding a fund holding a fund resolves."""
    fund(conn, "L2", [("A", "100", None)])
    fund(conn, "L1", [("__MFUNIT__", "100", "L2")])
    fund(conn, "L0", [("__MFUNIT__", "100", "L1")])

    assert weights_of(conn, "L0") == {"A": Decimal(100)}


# --- refusing ----------------------------------------------------------------


def test_a_cycle_buckets_rather_than_recursing_forever(
    conn: sqlite3.Connection,
) -> None:
    """§6.3 rule 2. A malformed mapping must not hang the rebuild."""
    fund(conn, "A1", [("__MFUNIT__", "100", "B1")])
    fund(conn, "B1", [("__MFUNIT__", "100", "A1")])

    assert weights_of(conn, "A1") == {"__MFUNIT__": Decimal(100)}


def test_a_fund_holding_itself_buckets(conn: sqlite3.Connection) -> None:
    fund(conn, "SELF", [("X", "40", None), ("__MFUNIT__", "60", "SELF")])

    w = weights_of(conn, "SELF")
    assert w == {"X": Decimal(40), "__MFUNIT__": Decimal(60)}


def test_a_chain_deeper_than_the_cap_buckets_the_remainder(
    conn: sqlite3.Connection,
) -> None:
    """§6.3 rule 1. Beyond the cap the exposure is shown as units, not guessed
    at -- a longer chain usually means bad data rather than a real holding."""
    assert MAX_FOF_DEPTH == 2
    fund(conn, "D3", [("DEEP", "100", None)])
    fund(conn, "D2", [("__MFUNIT__", "100", "D3")])
    fund(conn, "D1", [("__MFUNIT__", "100", "D2")])
    fund(conn, "D0", [("__MFUNIT__", "100", "D1")])

    w = weights_of(conn, "D0")
    assert w == {"__MFUNIT__": Decimal(100)}
    assert "DEEP" not in w


def test_a_unit_whose_fund_has_no_disclosure_stays_a_unit(
    conn: sqlite3.Connection,
) -> None:
    """Invariant 4, and the common case: 17 of the 53 funds held as units in
    this warehouse have no disclosure of their own. They must stay visible as
    units rather than vanishing or being approximated."""
    conn.execute(
        "INSERT INTO scheme (scheme_id, scheme_name, plan, option)"
        " VALUES ('DARK','DARK','direct','growth')"
    )
    fund(conn, "OUTER", [("C", "30", None), ("__MFUNIT__", "70", "DARK")])

    assert weights_of(conn, "OUTER") == {"C": Decimal(30), "__MFUNIT__": Decimal(70)}


def test_a_unit_with_no_isin_stays_a_unit(conn: sqlite3.Connection) -> None:
    """The aggregator tier stages `isin_raw=None` (§6.3 rule 1 of MODULE_0), so
    a Groww-sourced unit has nothing to resolve against."""
    fund(conn, "OUTER", [("C", "30", None), ("__MFUNIT__", "70", None)])

    assert weights_of(conn, "OUTER") == {"C": Decimal(30), "__MFUNIT__": Decimal(70)}


def test_expansion_drops_the_inner_quantity(conn: sqlite3.Connection) -> None:
    """Units of a fund are not units of its holdings. Carrying the inner count
    up would be a number with no meaning at the outer level."""
    fund(conn, "INNER", [("A", "100", None)])
    fund(conn, "OUTER", [("__MFUNIT__", "100", "INNER")])

    rows = expand_fund_units(conn, SchemeId("OUTER"), AS_OF)
    assert [r[0] for r in rows] == ["A"]
    assert rows[0][3] is None
