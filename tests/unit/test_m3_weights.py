"""`scheme_issuer_weight` materialisation. MODULE_0.md §9.3.

Regression tests from the V1.5 review. Each one covers a defect that the
existing suite did not reach, and each is a case the real data produces:
V1-07 recorded that HDFC discloses a short leg with a negative weight, and
HDFC's own disclosure already carries two revisions for one as-of date.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.common.types import SchemeId
from src.m0_data.schema.apply import apply_migrations
from src.m3_lookthrough.engine import assert_weights_sum_to_100
from src.m3_lookthrough.weights import (
    latest_as_of,
    latest_disclosure,
    load_issuer_weights,
    materialise_weights,
)

SCHEME = SchemeId("S1")
AS_OF = date(2026, 7, 31)


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    db = tmp_path / "w.db"
    apply_migrations(str(db))
    c = connect(str(db))
    c.execute(
        "INSERT INTO scheme (scheme_id, scheme_name, plan, option)"
        " VALUES ('S1','Test','direct','growth')"
    )
    return c


def _required(conn: sqlite3.Connection, table: str) -> list[str]:
    """NOT NULL columns with no default — everything an insert must supply."""
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


def _issuer(conn: sqlite3.Connection, issuer_id: str) -> None:
    conn.execute(
        "INSERT INTO issuer (issuer_id, canonical_name, is_synthetic) VALUES (?,?,0)",
        (issuer_id, issuer_id),
    )


def _disclose(
    conn: sqlite3.Connection, revision: int, holdings: list[tuple[str, str, str]]
) -> None:
    """One revision, and it becomes the current one."""
    conn.execute("UPDATE holding_disclosure SET is_current = 0 WHERE scheme_id = 'S1'")
    _insert(
        conn,
        "holding_disclosure",
        scheme_id="S1",
        as_of_date=AS_OF,
        revision=revision,
        source_file_id=f"f{revision}",
        row_count=len(holdings),
        is_current=1,
    )
    for n, (issuer_id, pct, klass) in enumerate(holdings, start=1):
        _insert(
            conn,
            "holding",
            scheme_id="S1",
            as_of_date=AS_OF,
            revision=revision,
            row_number=n,
            issuer_id=issuer_id,
            instrument_raw_name=issuer_id,
            market_value=Decimal("100"),
            pct_normalised=Decimal(pct),
            instrument_class=klass,
        )
    conn.commit()


def test_an_issuer_held_only_short_does_not_crash(conn: sqlite3.Connection) -> None:
    """A negative weight is a real position, not a missing one.

    `largest` used to default to zero, so an issuer whose only holding was a
    short leg never cleared `pct >= largest[...]`, never got an
    `instrument_class`, and raised `KeyError` on the way to the insert. V1-07
    recorded that HDFC writes exactly such a row (Eternal Limited at -0.001%);
    it survived only because that fund also holds the issuer long.
    """
    _issuer(conn, "SHORTONLY")
    _disclose(conn, 1, [("SHORTONLY", "-0.001", "derivative")])

    assert materialise_weights(conn, SCHEME, AS_OF) == 1
    got = load_issuer_weights(conn, SCHEME, AS_OF)
    assert [str(x.issuer_id) for x in got] == ["SHORTONLY"]
    assert got[0].weight == Decimal("-0.001")
    assert got[0].instrument_class == "derivative"


def test_the_dominant_class_is_the_largest_by_absolute_weight(
    conn: sqlite3.Connection,
) -> None:
    """A big short is the dominant position in that issuer, not the smallest.

    Comparing signed weights would let a token long holding outrank a large
    short and mislabel the issuer's class.
    """
    _issuer(conn, "BOTH")
    _disclose(conn, 1, [("BOTH", "-30", "derivative"), ("BOTH", "5", "equity")])

    materialise_weights(conn, SCHEME, AS_OF)
    got = load_issuer_weights(conn, SCHEME, AS_OF)
    assert len(got) == 1
    assert got[0].weight == Decimal("-25")  # the two net off
    assert got[0].instrument_class == "derivative"  # the larger leg names it


def test_a_restatement_removes_issuers_it_dropped(conn: sqlite3.Connection) -> None:
    """Derived rows are replaced as a set, not merged into.

    `INSERT OR REPLACE` alone updated the issuers the new revision still had and
    left behind the one it dropped: the scheme then carried three issuers
    summing to 120, and `assert_weights_sum_to_100` blamed MODULE_0.
    """
    for issuer_id in ("A", "B", "GONE"):
        _issuer(conn, issuer_id)
    _disclose(
        conn, 1, [("A", "50", "equity"), ("B", "30", "equity"), ("GONE", "20", "equity")]
    )
    materialise_weights(conn, SCHEME, AS_OF)
    assert len(load_issuer_weights(conn, SCHEME, AS_OF)) == 3

    _disclose(conn, 2, [("A", "60", "equity"), ("B", "40", "equity")])
    materialise_weights(conn, SCHEME, AS_OF)

    got = load_issuer_weights(conn, SCHEME, AS_OF)
    assert sorted(str(x.issuer_id) for x in got) == ["A", "B"]
    assert sum((x.weight for x in got), Decimal(0)) == Decimal(100)
    assert_weights_sum_to_100(got, SCHEME, AS_OF)


def test_latest_as_of_honours_the_on_or_before_bound(
    conn: sqlite3.Connection,
) -> None:
    """§5.5: a scheme contributes from its latest disclosure ON OR BEFORE as_of.

    Without the bound this returned the newest disclosure whatever its date, so
    a look-through for an earlier date used holdings from the future and stored
    a negative `staleness_days` that reads as fresher than fresh.
    """
    _issuer(conn, "A")
    _disclose(conn, 1, [("A", "100", "equity")])

    assert latest_as_of(conn, SCHEME) == AS_OF
    assert latest_as_of(conn, SCHEME, on_or_before=date(2026, 12, 31)) == AS_OF
    assert latest_as_of(conn, SCHEME, on_or_before=AS_OF) == AS_OF
    assert latest_as_of(conn, SCHEME, on_or_before=date(2026, 6, 30)) is None


def _disclose_at(
    conn: sqlite3.Connection,
    as_of: date,
    tier: str,
    revision: int = 1,
    scheme_id: str = "S1",
) -> None:
    """One current disclosure at a date, from a tier. Holdings are beside the
    point here — what is under test is which disclosure gets chosen."""
    conn.execute(
        "UPDATE holding_disclosure SET is_current = 0"
        " WHERE scheme_id = ? AND as_of_date = ?",
        (scheme_id, as_of),
    )
    _insert(
        conn,
        "holding_disclosure",
        scheme_id=scheme_id,
        as_of_date=as_of,
        revision=revision,
        source_file_id=f"{tier}{as_of}",
        row_count=1,
        source_tier=tier,
        is_current=1,
    )
    conn.commit()


class TestTheSourceOfRecordIsNotOutrankedSilently:
    """V1-43 and V1-46. The coverage tier carries no ISIN column, and four of
    the cascade's rules key on ISIN structure — measured on PPFAS Flexi Cap,
    0.00% unresolved from the AMC's workbook against 22.63% from the page.

    `max(as_of_date)` alone moved a scheme onto the worse data the moment a
    newer page was loaded beside an older workbook, and nothing downstream
    could see it happen. The guard lived in the job that wrote the data, which
    is a guard the next writer has to remember.
    """

    JULY = date(2026, 7, 31)
    AUGUST = date(2026, 8, 31)

    def test_a_newer_aggregator_does_not_displace_a_recent_amc_file(
        self, conn: sqlite3.Connection
    ) -> None:
        """The exact case that was loaded and had to be deleted by hand."""
        _disclose_at(conn, self.JULY, "amc_direct")
        _disclose_at(conn, self.AUGUST, "aggregator")

        assert latest_disclosure(conn, SCHEME, on_or_before=self.AUGUST) == (
            self.JULY,
            "amc_direct",
        )
        assert latest_as_of(conn, SCHEME, on_or_before=self.AUGUST) == self.JULY

    def test_a_stale_amc_file_does_yield_to_the_aggregator(
        self, conn: sqlite3.Connection
    ) -> None:
        """The other half, and the reason this is not "prefer amc_direct always".

        Past `STALENESS_WARN_DAYS` age is the larger problem, and it is one
        `confidence_for` already prices. Holding a two-year-old workbook over a
        current page would trade a known weakness for a worse one.
        """
        _disclose_at(conn, date(2024, 1, 31), "amc_direct")
        _disclose_at(conn, self.AUGUST, "aggregator")

        assert latest_disclosure(conn, SCHEME, on_or_before=self.AUGUST) == (
            self.AUGUST,
            "aggregator",
        )

    def test_the_boundary_is_the_systems_own_staleness_threshold(
        self, conn: sqlite3.Connection
    ) -> None:
        """No new number was invented. `STALENESS_WARN_DAYS` is the threshold
        `confidence_for` already uses to call a disclosure too old."""
        from src.m3_lookthrough.persist import STALENESS_WARN_DAYS

        newest = date(2026, 8, 31)
        inside = date.fromordinal(newest.toordinal() - STALENESS_WARN_DAYS)
        outside = date.fromordinal(newest.toordinal() - STALENESS_WARN_DAYS - 1)

        _disclose_at(conn, inside, "amc_direct")
        _disclose_at(conn, newest, "aggregator")
        assert latest_as_of(conn, SCHEME, on_or_before=newest) == inside

        conn.execute("DELETE FROM holding_disclosure WHERE as_of_date = ?", (inside,))
        _disclose_at(conn, outside, "amc_direct")
        assert latest_as_of(conn, SCHEME, on_or_before=newest) == newest

    def test_an_aggregator_only_scheme_is_untouched(
        self, conn: sqlite3.Connection
    ) -> None:
        """The coverage tier's whole point is funds with no AMC parser. A
        preference for a file that does not exist must not hide them."""
        _disclose_at(conn, self.AUGUST, "aggregator")
        assert latest_disclosure(conn, SCHEME, on_or_before=self.AUGUST) == (
            self.AUGUST,
            "aggregator",
        )

    def test_two_amc_files_still_take_the_newer(self, conn: sqlite3.Connection) -> None:
        """§5.5 is unchanged where no tier is in question."""
        _disclose_at(conn, self.JULY, "amc_direct")
        _disclose_at(conn, self.AUGUST, "amc_direct")
        assert latest_as_of(conn, SCHEME, on_or_before=self.AUGUST) == self.AUGUST

    def test_the_on_or_before_bound_still_binds(self, conn: sqlite3.Connection) -> None:
        """A preference that reached past the bound would resurrect the
        negative-staleness bug §5.5's bound exists to prevent."""
        _disclose_at(conn, self.JULY, "amc_direct")
        _disclose_at(conn, self.AUGUST, "aggregator")

        assert latest_as_of(conn, SCHEME, on_or_before=date(2026, 6, 30)) is None
        assert latest_as_of(conn, SCHEME, on_or_before=self.JULY) == self.JULY


def test_the_tier_choice_does_not_move_with_the_report_date(
    conn: sqlite3.Connection,
) -> None:
    """V1-47. The first version measured the AMC file's staleness from the
    CALLER'S BOUND, so the same warehouse answered differently depending on
    when you asked: the workbook at 0.00% unresolved on 2026-09-13 and the page
    at ~19% two days later, with nothing changed underneath.

    Worse, the two call forms disagreed — the unbounded one already used the
    newest candidate as its reference — so `scripts/show_lookthrough.py` and
    the persisted look-through could report different portfolios for one scheme
    on one day. How late someone runs a report is what `staleness_days` is for.
    """
    july, august = date(2026, 7, 31), date(2026, 8, 31)
    _disclose_at(conn, july, "amc_direct")
    _disclose_at(conn, august, "aggregator")

    answers = {
        latest_disclosure(conn, SCHEME, bound)
        for bound in (
            None,
            august,
            date(2026, 9, 13),
            date(2026, 9, 15),
            date(2026, 12, 31),
        )
    }
    assert answers == {(july, "amc_direct")}, (
        f"the tier choice moved with the report date: {sorted(map(str, answers))}"
    )
