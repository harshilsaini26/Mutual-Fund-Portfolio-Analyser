"""Persisting the look-through. MODULE_3.md §4.2, §4.6, §5.5, §14.1.

Written before the implementation, per `CLAUDE.md`'s working agreement for
`src/m3_lookthrough/`.

These are **derived** tables: `CLAUDE.md` invariant 10 says every one must be
droppable and reproduce byte-identical output on a rebuild, and the only input
is a `LookThroughResult` the engine computed from `txn` and `scheme_issuer_weight`.
Nothing here is a fact row, so a rebuild replaces rather than appending — the
opposite of invariant 2, and the distinction is the point.

Zone B, so encrypted, and every Decimal must survive the round trip. That has
now failed twice in this project for two different reasons (SZ-13's affinity,
V1-16's per-driver registry), so it is asserted rather than assumed.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.types import IssuerId, SchemeId, UserId
from src.m1_ledger.db import apply_ledger_schema, connect_ledger
from src.m3_lookthrough.engine import (
    IssuerWeight,
    LookThroughResult,
    Position,
    compute_lookthrough,
)
from src.m3_lookthrough.persist import (
    STALENESS_WARN_DAYS,
    confidence_for,
    drop_lookthrough,
    load_exposures,
    load_summary,
    save_lookthrough,
)

from tests.conftest import reopen_ledger

USER = UserId("USER-01")
AS_OF = date(2026, 9, 4)
JULY = date(2026, 7, 31)
JUNE = date(2026, 6, 30)
KEY = "test-key-not-a-real-secret"


def w(issuer: str, weight: str, klass: str = "equity") -> IssuerWeight:
    return IssuerWeight(IssuerId(issuer), Decimal(weight), klass)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = connect_ledger(str(tmp_path / "personal.db"), key=KEY)
    apply_ledger_schema(db)
    return db


@pytest.fixture
def result():  # type: ignore[no-untyped-def]
    return compute_lookthrough(
        [Position(SchemeId("S1"), Decimal("100000")),
         Position(SchemeId("S2"), Decimal("50000")),
         Position(SchemeId("S_DARK"), Decimal("25000"))],
        {
            SchemeId("S1"): [
                w("ACME", "60"), w("BETA", "35"), w("__CASH__", "5", "cash")
            ],
            SchemeId("S2"): [w("BETA", "100")],
        },
        AS_OF,
    )


DATES = {SchemeId("S1"): JULY, SchemeId("S2"): JUNE}


# --- §4.2 the round trip -----------------------------------------------------


def test_exposures_survive_the_round_trip(conn, result) -> None:  # type: ignore[no-untyped-def]
    """What went in comes back, as `Decimal` and in size order."""
    written = save_lookthrough(conn, USER, AS_OF, result, DATES)
    assert written == len(result.exposures)

    back = load_exposures(conn, USER, AS_OF)
    assert [str(e.issuer_id) for e in back] == [
        str(e.issuer_id) for e in result.exposures
    ]
    for stored, original in zip(back, result.exposures, strict=True):
        assert stored.exposure_inr == original.exposure_inr
        assert isinstance(stored.exposure_inr, Decimal)
        assert stored.pct_of_portfolio == original.pct_of_portfolio
        assert stored.fund_count == original.fund_count
        assert stored.is_synthetic == original.is_synthetic
        assert stored.instrument_class == original.instrument_class


def test_the_stored_exposures_still_close(conn, result) -> None:  # type: ignore[no-untyped-def]
    """§2.2 must survive storage, not merely computation.

    A rounding or truncation on write would break the identity silently, and
    every figure read back afterwards would be a little wrong.
    """
    save_lookthrough(conn, USER, AS_OF, result, DATES)
    back = load_exposures(conn, USER, AS_OF)
    total = sum((e.exposure_inr for e in back), Decimal(0))
    assert total == Decimal("175000")
    assert load_summary(conn, USER, AS_OF).total_value_inr == total


def test_contributions_trace_every_exposure_to_its_funds(conn, result) -> None:  # type: ignore[no-untyped-def]
    """§4.2: *"which of my funds gives me this exposure, and how much?"*

    `PLAN.md` §4.2's audit trail, at portfolio level: each issuer's stored
    exposure equals the sum of its contributions. Aggregated in Python — the
    obvious `SUM(exposure_inr) GROUP BY issuer_id` is what invariant 1 forbids.
    """
    save_lookthrough(conn, USER, AS_OF, result, DATES)
    rows = conn.execute(
        "SELECT issuer_id, scheme_id, exposure_inr FROM lookthrough_contribution"
        " WHERE user_id = ? AND as_of = ?",
        (str(USER), AS_OF.isoformat()),
    ).fetchall()
    assert rows

    per_issuer: dict[str, Decimal] = {}
    for issuer_id, _scheme_id, value in rows:
        per_issuer[issuer_id] = per_issuer.get(issuer_id, Decimal(0)) + value

    for exposure in load_exposures(conn, USER, AS_OF):
        assert per_issuer[str(exposure.issuer_id)] == exposure.exposure_inr

    beta = [r for r in rows if r[0] == "BETA"]
    assert {r[1] for r in beta} == {"S1", "S2"}


# --- §5.5 staleness ----------------------------------------------------------


def test_holdings_as_of_is_the_earliest_contributing_date(conn, result) -> None:  # type: ignore[no-untyped-def]
    """§5.5: *"the EARLIEST contributing date — the worst-case staleness."*

    BETA comes from S1 (July) and S2 (June), so its `holdings_as_of` is June.
    Taking the latest would report the portfolio as fresher than it is, which
    is the one direction this number must never err in.
    """
    save_lookthrough(conn, USER, AS_OF, result, DATES)
    rows = dict(
        conn.execute(
            "SELECT issuer_id, holdings_as_of FROM lookthrough_exposure"
            " WHERE user_id = ? AND as_of = ?",
            (str(USER), AS_OF.isoformat()),
        ).fetchall()
    )
    assert rows["ACME"] == JULY.isoformat()          # S1 only
    assert rows["BETA"] == JUNE.isoformat()          # S1 and S2 -> the earlier
    assert rows["__NO_DISCLOSURE__"] is None         # never disclosed at all

    staleness = dict(
        conn.execute(
            "SELECT issuer_id, staleness_days FROM lookthrough_exposure"
            " WHERE user_id = ? AND as_of = ?",
            (str(USER), AS_OF.isoformat()),
        ).fetchall()
    )
    assert staleness["ACME"] == (AS_OF - JULY).days
    assert staleness["BETA"] == (AS_OF - JUNE).days


def test_worst_staleness_reaches_the_summary(conn, result) -> None:  # type: ignore[no-untyped-def]
    """§5.5: `portfolio_summary.worst_staleness_days` derives from the earliest."""
    save_lookthrough(conn, USER, AS_OF, result, DATES)
    assert load_summary(conn, USER, AS_OF).worst_staleness_days == (AS_OF - JUNE).days


# --- §14.1 confidence --------------------------------------------------------


def test_confidence_follows_the_specified_rule() -> None:
    """§14.1, verbatim. Not a scale invented here."""
    assert confidence_for(Decimal(100), Decimal(0), 10) == "high"
    assert confidence_for(Decimal(98), Decimal(2), STALENESS_WARN_DAYS) == "high"
    # Any single condition failing drops it, because §14.2 rule 3 makes
    # portfolio confidence the weakest link rather than an average.
    assert confidence_for(Decimal(97), Decimal(0), 10) == "medium"
    assert confidence_for(Decimal(100), Decimal("2.1"), 10) == "medium"
    assert confidence_for(Decimal(100), Decimal(0), STALENESS_WARN_DAYS + 1) == "medium"
    assert confidence_for(Decimal(79), Decimal(0), 10) == "low"


def test_a_portfolio_with_an_undisclosed_fund_is_not_high_confidence(  # type: ignore[no-untyped-def]
    conn, result
) -> None:
    """Coverage here is 85.7%, so §14.1 gives `medium` — and it is stored."""
    save_lookthrough(conn, USER, AS_OF, result, DATES)
    summary = load_summary(conn, USER, AS_OF)
    assert summary.coverage_pct < Decimal(98)
    assert summary.confidence == "medium"
    # §14.2 rule 1: every output row carries it, not just the summary.
    stored = {
        r[0]: r[1]
        for r in conn.execute(
            "SELECT issuer_id, confidence FROM lookthrough_exposure"
            " WHERE user_id = ? AND as_of = ?",
            (str(USER), AS_OF.isoformat()),
        )
    }
    assert set(stored.values()) == {"medium"}


def test_caveats_are_stored_for_m6(conn, result) -> None:  # type: ignore[no-untyped-def]
    """§14.2 rule 2: caveats flow into M6 and must be rendered.

    Stored as JSON so they survive as a list rather than a flattened string.
    """
    import json

    save_lookthrough(conn, USER, AS_OF, result, DATES)
    raw = conn.execute(
        "SELECT caveats FROM portfolio_summary WHERE user_id = ? AND as_of = ?",
        (str(USER), AS_OF.isoformat()),
    ).fetchone()[0]
    caveats = json.loads(raw)
    assert isinstance(caveats, list)
    assert caveats == result.caveats
    assert any("disclosure" in c.lower() for c in caveats)


# --- invariant 10: derived, droppable, reproducible ---------------------------


def test_saving_twice_replaces_rather_than_accumulates(conn, result) -> None:  # type: ignore[no-untyped-def]
    """Derived tables are recomputed, not appended to.

    `CLAUDE.md` invariant 2 forbids updating a FACT row; these are not facts.
    Re-running the look-through must leave one row per issuer, or every read
    after the second run double-counts.
    """
    save_lookthrough(conn, USER, AS_OF, result, DATES)
    first = load_exposures(conn, USER, AS_OF)
    save_lookthrough(conn, USER, AS_OF, result, DATES)
    second = load_exposures(conn, USER, AS_OF)

    assert len(second) == len(first)
    assert [e.exposure_inr for e in second] == [e.exposure_inr for e in first]
    assert _count(conn, "portfolio_summary") == 1


def test_invariant_10_drop_and_rebuild_reproduces_the_rows(conn, result) -> None:  # type: ignore[no-untyped-def]
    """A real DROP, then recompute and save, and the content must match.

    Deleting rows would leave the schema behind and prove half of it, which is
    the same argument V1-13 made for Zone B's ledger tables.
    """
    save_lookthrough(conn, USER, AS_OF, result, DATES, computed_at="PINNED")
    before = {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY 1,2,3,4").fetchall()
        for t in ("lookthrough_exposure", "lookthrough_contribution",
                  "portfolio_summary")
    }

    drop_lookthrough(conn)
    for table in before:
        assert not _table_exists(conn, table), f"{table} survived the drop"

    apply_ledger_schema(conn, force=True)
    save_lookthrough(conn, USER, AS_OF, result, DATES, computed_at="PINNED")
    after = {
        t: conn.execute(f"SELECT * FROM {t} ORDER BY 1,2,3,4").fetchall()
        for t in before
    }
    assert after == before


def test_every_decimal_column_reads_back_as_decimal(conn, result) -> None:  # type: ignore[no-untyped-def]
    """SZ-13 and V1-16, on the third set of tables to need the check.

    Both previous failures were silent on the read side. `typeof` confirms
    SQLite stored text; `isinstance` confirms the converter fired.
    """
    save_lookthrough(conn, USER, AS_OF, result, DATES)
    rows = conn.execute(
        "SELECT exposure_inr, exposure_pct, fund_inr, direct_inr, coverage_pct"
        " FROM lookthrough_exposure WHERE user_id = ?", (str(USER),)
    ).fetchall()
    assert rows
    for row in rows:
        for value in row:
            assert isinstance(value, Decimal), f"got {type(value).__name__}"

    kinds = conn.execute(
        "SELECT typeof(exposure_inr), typeof(exposure_pct)"
        " FROM lookthrough_exposure LIMIT 1"
    ).fetchone()
    assert kinds == ("text", "text")


def test_the_illustrative_basis_is_not_silently_storable(conn, result) -> None:  # type: ignore[no-untyped-def]
    """`weight_basis` is recorded, so a stored row says what it was computed on.

    Only `disclosed` exists today; `drift_adjusted` needs `security_price`.
    Storing the basis now means the drift-adjusted rows can land beside these
    rather than overwrite them — the primary key already separates them.
    """
    save_lookthrough(conn, USER, AS_OF, result, DATES)
    bases = {
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT weight_basis FROM lookthrough_exposure"
        )
    }
    assert bases == {"disclosed"}


def _count(conn: sqlite3.Connection, table: str) -> int:
    return int(conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0])


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
    )


def test_ordering_is_numeric_not_lexicographic(conn, result) -> None:  # type: ignore[no-untyped-def]
    """`ORDER BY` on a DECIMAL_TEXT column sorts as TEXT. Found by this failing.

    `"5000"` sorts above `"25000"` because `'5' > '2'`, so a SQL-ordered top-20
    is simply not the top 20 — and the SQL reads as obviously correct. §4.2's
    own `ix_lte_size` index is declared `exposure_inr DESC` and has the same
    problem.

    The fixture has `__CASH__` at 5,000 and `__NO_DISCLOSURE__` at 25,000, which
    is exactly the pair that inverts.
    """
    save_lookthrough(conn, USER, AS_OF, result, DATES)

    values = [e.exposure_inr for e in load_exposures(conn, USER, AS_OF)]
    assert values == sorted(values, reverse=True), "not in descending value order"

    # And the SQL ordering the spec's index suggests really is wrong here, so
    # this test fails if someone "simplifies" the Python sort away.
    sql_ordered = [
        r[0] for r in conn.execute(
            "SELECT exposure_inr FROM lookthrough_exposure"
            " WHERE user_id = ? ORDER BY exposure_inr DESC", (str(USER),)
        )
    ]
    assert sql_ordered != sorted(sql_ordered, reverse=True), (
        "SQL ordering happened to be right; pick fixture values that expose it"
    )


def test_fund_and_direct_split_adds_up_to_the_exposure(conn, result) -> None:  # type: ignore[no-untyped-def]
    """§4.2 stores the split, so the split must reconcile. Found by mutation.

    `fund_inr` stored as zero survived a green suite: the column was written and
    never read back. With no `direct_holding` yet (§7) every rupee arrives
    through a fund, so `fund_inr == exposure_inr` and `direct_inr == 0` — and
    when §7 lands, this is the test that says the two halves still sum.
    """
    save_lookthrough(conn, USER, AS_OF, result, DATES)
    rows = conn.execute(
        "SELECT issuer_id, exposure_inr, fund_inr, direct_inr"
        " FROM lookthrough_exposure WHERE user_id = ?", (str(USER),)
    ).fetchall()
    assert rows
    for issuer_id, exposure, fund, direct in rows:
        assert fund + direct == exposure, f"{issuer_id} split does not reconcile"
        assert direct == Decimal(0)
        assert fund == exposure


def test_weight_in_fund_is_the_share_of_that_fund_not_the_portfolio(  # type: ignore[no-untyped-def]
    conn, result
) -> None:
    """§4.2's `weight_in_fund` answers "how much of THAT fund is this issuer?".

    Found by mutation: dividing by the portfolio total instead survived, because
    nothing read the column. The two coincide only for a single-fund portfolio,
    which is exactly the fixture someone would reach for.

    BETA is 35% of S1 and 100% of S2. Against the portfolio it would read 20%
    and 28.6% — plausible numbers, wrong question.
    """
    save_lookthrough(conn, USER, AS_OF, result, DATES)
    rows = {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT issuer_id, scheme_id, weight_in_fund"
            " FROM lookthrough_contribution WHERE user_id = ?", (str(USER),)
        )
    }
    assert rows[("BETA", "S1")] == Decimal(35)
    assert rows[("BETA", "S2")] == Decimal(100)
    assert rows[("ACME", "S1")] == Decimal(60)
    assert rows[("__NO_DISCLOSURE__", "S_DARK")] == Decimal(100)

    # And each fund's weights still sum to 100 after the round trip.
    for scheme in ("S1", "S2"):
        total = sum(
            (v for (_i, s), v in rows.items() if s == scheme), Decimal(0)
        )
        assert total == Decimal(100), f"{scheme} weights sum to {total}"


# --- regressions from the V1.5 review ----------------------------------------


def test_unknown_staleness_is_not_scored_as_fresh() -> None:
    """`None` means nobody measured, and unmeasured is not strong.

    Before the fix `worst_staleness` used `max(..., default=0)`, so a save with
    no per-scheme dates scored `high` while every stored row's
    `staleness_days` was correctly NULL — the summary contradicting the rows
    beneath it. §14.2 rule 3 makes confidence the weakest link.
    """
    assert confidence_for(Decimal(100), Decimal(0), None) == "medium"
    assert confidence_for(Decimal(70), Decimal(0), None) == "low"
    assert confidence_for(Decimal(100), Decimal(0), 10) == "high"


def test_saving_without_dates_stores_unknown_staleness(conn, result) -> None:  # type: ignore[no-untyped-def]
    """And it reaches the database, rather than being a local nicety."""
    save_lookthrough(conn, USER, AS_OF, result, None)
    summary = load_summary(conn, USER, AS_OF)
    assert summary.worst_staleness_days is None
    assert summary.confidence != "high"


def test_a_disclosure_dated_after_the_as_of_is_refused(conn, result) -> None:  # type: ignore[no-untyped-def]
    """§5.5's "on or before", and `CLAUDE.md` invariant 5: raise, don't clamp.

    A negative age is not a small error — it reads as fresher than fresh and
    passes the staleness arm of the confidence rule outright.
    """
    future = {SchemeId("S1"): date(2027, 1, 1), SchemeId("S2"): JUNE}
    with pytest.raises(ValueError, match="after the look-through date"):
        save_lookthrough(conn, USER, AS_OF, result, future)


def test_weight_in_fund_is_unchanged_by_the_hoist(conn, result) -> None:  # type: ignore[no-untyped-def]
    """The O(n^2) rescan became one pass; the numbers must not move.

    `_position_values` builds the same per-scheme totals `_position_value`
    recomputed per contribution, so every stored weight is identical and each
    fund's weights still sum to 100.
    """
    save_lookthrough(conn, USER, AS_OF, result, DATES)
    rows = {
        (r[0], r[1]): r[2]
        for r in conn.execute(
            "SELECT issuer_id, scheme_id, weight_in_fund"
            " FROM lookthrough_contribution WHERE user_id = ?", (str(USER),)
        )
    }
    assert rows[("BETA", "S1")] == Decimal(35)
    assert rows[("BETA", "S2")] == Decimal(100)
    for scheme in ("S1", "S2"):
        total = sum((v for (_i, s), v in rows.items() if s == scheme), Decimal(0))
        assert total == Decimal(100), f"{scheme} weights sum to {total}"


class TestWhatIsWrittenSurvivesTheConnection:
    """Every assertion here reads through a SECOND connection.

    Measured before these existed: deleting the `conn.commit()` from any of
    twelve functions in `src/` left `tests/unit` green, because a test that
    reads back on the connection that wrote sees uncommitted rows either way.
    The production failure is correct numbers reported and nothing stored.
    DECISIONS V1-58, `tests/conftest.py`.
    """

    def test_save_lookthrough_commits(
        self, conn: sqlite3.Connection, result: LookThroughResult
    ) -> None:
        save_lookthrough(conn, USER, AS_OF, result, DATES)
        reopened = reopen_ledger(conn, KEY)
        counts = {
            t: reopened.execute(f"SELECT count(*) FROM {t}").fetchone()[0]
            for t in ("lookthrough_exposure", "lookthrough_contribution",
                      "portfolio_summary")
        }
        reopened.close()
        assert all(n > 0 for n in counts.values()), counts

    def test_drop_lookthrough_commits(
        self, conn: sqlite3.Connection, result: LookThroughResult
    ) -> None:
        save_lookthrough(conn, USER, AS_OF, result, DATES)
        drop_lookthrough(conn)
        reopened = reopen_ledger(conn, KEY)
        # A DROP, not a DELETE -- the claim is that these are reconstructible.
        survived = reopened.execute(
            "SELECT count(*) FROM sqlite_master"
            " WHERE type='table' AND name='lookthrough_exposure'"
        ).fetchone()[0]
        reopened.close()
        assert survived == 0
