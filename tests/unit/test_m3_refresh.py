"""The look-through, computed and stored in one place. `src/m3_lookthrough/refresh.py`.

Until DECISIONS V1-74 only `scripts.show_lookthrough` stored it, so importing a
statement left every portfolio page empty: the pages read tables nothing but a
terminal report wrote. These pin what the one shared path promises -- the date a
portfolio is filed under, and that everything the portfolio views read is there.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal

from src.common.types import IssuerId, SchemeId, UserId
from src.m1_ledger.db import apply_ledger_schema, connect_ledger
from src.m3_lookthrough.engine import IssuerWeight, Position
from src.m3_lookthrough.persist_metrics import (
    load_duplication,
    load_marginals,
    load_overlap,
)
from src.m3_lookthrough.refresh import analyse, store

USER = UserId("USER-01")
A = SchemeId("INF000000A01")
B = SchemeId("INF000000B01")
C = SchemeId("INF000000C01")

WEIGHTS = {
    A: [IssuerWeight(IssuerId("ISS-1"), Decimal("60"), "equity"),
        IssuerWeight(IssuerId("ISS-2"), Decimal("40"), "equity")],
    B: [IssuerWeight(IssuerId("ISS-1"), Decimal("50"), "equity"),
        IssuerWeight(IssuerId("ISS-3"), Decimal("50"), "equity")],
    C: [IssuerWeight(IssuerId("ISS-4"), Decimal("100"), "equity")],
}
AS_OFS = {A: date(2026, 7, 31), B: date(2026, 8, 31), C: date(2026, 9, 30)}
HELD = [Position(A, Decimal("100000")), Position(B, Decimal("50000"))]


def _ledger() -> sqlite3.Connection:
    conn = connect_ledger(":memory:", allow_unencrypted=True)
    apply_ledger_schema(conn)
    return conn


def test_the_date_is_the_newest_disclosure_behind_what_is_held() -> None:
    """C filed a month later, but it is not held: its file must not date this
    portfolio, or a page would claim holdings are fresher than they are."""
    assert analyse(HELD, WEIGHTS, AS_OFS).as_of == date(2026, 8, 31)


def test_with_no_disclosure_behind_any_holding_the_date_is_today() -> None:
    held = [Position(SchemeId("INF000000Z01"), Decimal("1000"))]
    today = date(2026, 9, 24)
    assert analyse(held, WEIGHTS, AS_OFS, today=today).as_of == today


def test_everything_the_portfolio_views_read_is_stored() -> None:
    ledger = _ledger()
    analysis = analyse(HELD, WEIGHTS, AS_OFS)
    stored = store(ledger, USER, analysis, HELD, WEIGHTS, AS_OFS)
    on = analysis.as_of

    total = ledger.execute(
        "SELECT total_value_inr FROM portfolio_summary WHERE user_id = ? AND as_of = ?",
        (str(USER), on.isoformat()),
    ).fetchone()
    assert total is not None and Decimal(str(total[0])) == Decimal("150000")
    assert stored["exposures"] == 3  # ISS-1, ISS-2, ISS-3: C is not held
    assert len(load_overlap(ledger, USER, on)) == 1  # one pair: A x B
    assert load_duplication(ledger, USER, on) is not None
    assert len(load_marginals(ledger, USER, on)) == 2  # one per held fund


def test_storing_again_replaces_rather_than_adds() -> None:
    """A re-import recomputes; a second copy of every row would double every
    exposure on the next read."""
    ledger = _ledger()
    analysis = analyse(HELD, WEIGHTS, AS_OFS)
    store(ledger, USER, analysis, HELD, WEIGHTS, AS_OFS)
    first = ledger.execute("SELECT count(*) FROM lookthrough_exposure").fetchone()[0]
    store(ledger, USER, analysis, HELD, WEIGHTS, AS_OFS)
    again = ledger.execute("SELECT count(*) FROM lookthrough_exposure").fetchone()[0]
    assert again == first
