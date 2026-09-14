"""What a migration does to a warehouse that already holds rows.

`apply_migrations` runs in most fixtures in this suite, and always against a
fresh empty database -- the one path where a table rebuild has nothing to copy
and every statement trivially succeeds. So the suite stayed green through two
wrong answers about `013_scheme_aum_basis.sql`: first that it was safe to
re-run (killed between its INSERT and its DROP it wedged the whole chain), then
that a leading `DROP TABLE IF EXISTS` fixed that -- it did, and it turned the
next window along from a wedged chain into 12,388 rows destroyed.

Both windows are marked in the migration itself, and the tests below cut the
script at those markers rather than at its prose. The third failure mode needs
no crash at all: a row the new constraint will not take.
"""

from __future__ import annotations

import re
import shutil
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.m0_data.schema.apply import (
    MIGRATIONS,
    MigrationError,
    apply_migrations,
    unsafe_decimal_columns,
)

M013 = MIGRATIONS / "013_scheme_aum_basis.sql"


@contextmanager
def _open(db: Path, *, decimals: bool = False) -> Iterator[sqlite3.Connection]:
    """A connection that closes itself.

    On Windows an open SQLite file cannot be unlinked, so a handle left behind
    here does not fail this test -- it fails pytest's `tmp_path` reaper three
    runs later, in a session with nothing to do with migrations.
    `jobs/fetch_aum.py`'s `run` got a `try/finally` for exactly this, and the
    first version of this file then leaked six of them.
    """
    conn = connect(str(db)) if decimals else sqlite3.connect(str(db))
    try:
        yield conn
    finally:
        conn.close()


def _cut(marker: str) -> str:
    """The migration up to one of its crash markers.

    By MARKER, not by searching the prose. The first version cut on
    `split("DROP TABLE scheme_aum;")` and `index("ALTER TABLE scheme_aum_next")`
    against a file whose comment block discusses both statements in English --
    it worked only because those sentences happened not to carry a semicolon.
    """
    sql = M013.read_text(encoding="utf-8")
    assert sql.count(marker) == 1, f"{marker!r} must appear exactly once"
    return sql[: sql.index(marker)]


def _loaded(tmp_path: Path, rows: int = 3) -> Path:
    """A warehouse whose `scheme_aum` is populated, every migration applied."""
    db = tmp_path / "canonical.db"
    apply_migrations(str(db))
    with _open(db) as conn:
        conn.executemany(
            "INSERT INTO scheme_aum (scheme_id, as_of_date, aum_inr, basis)"
            " VALUES (?,?,?,'quarterly_average')",
            [(f"INF{i}", "2026-06-30", "1000") for i in range(rows)],
        )
        conn.commit()
    return db


def _at_012(tmp_path: Path) -> Path:
    """A warehouse as it stood BEFORE 013, `basis` still a bare TEXT column.

    `migration_files` requires contiguous numbering, so stopping short of 013
    means copying 001..012 into a directory of their own. `schema_migration`
    records the same names either way, so the real directory then runs 013 and
    only 013.
    """
    staged = tmp_path / "migrations_to_012"
    staged.mkdir()
    for src in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
        if int(src.name[:3]) <= 12:
            shutil.copy(src, staged / src.name)
    db = tmp_path / "canonical.db"
    apply_migrations(str(db), staged)
    return db


class TestTheBasisVocabularyLivesOnTheColumn:
    """`012` declared `point_in_time | quarterly_average` in a COMMENT and the
    column took any TEXT. §10's V2 raises `UnknownAumBasis` on anything else,
    so one unreadable row stops V2 for every disclosure that reads it."""

    def test_an_unknown_basis_is_refused_by_the_database(self, tmp_path: Path) -> None:
        with (
            _open(_loaded(tmp_path)) as conn,
            pytest.raises(sqlite3.IntegrityError, match="basis"),
        ):
            conn.execute(
                "INSERT INTO scheme_aum (scheme_id, as_of_date, basis)"
                " VALUES ('INFX','2026-06-30','quaterly_average')"
            )

    def test_both_spellings_the_readers_know_are_accepted(
        self, tmp_path: Path
    ) -> None:
        """The constraint has to admit exactly what `TOLERANCE_BY_BASIS` holds,
        or it trades one unreadable row for a load that cannot happen."""
        with _open(_loaded(tmp_path)) as conn:
            for i, basis in enumerate(("point_in_time", "quarterly_average")):
                conn.execute(
                    "INSERT INTO scheme_aum (scheme_id, as_of_date, basis)"
                    " VALUES (?,'2026-03-31',?)",
                    (f"INFOK{i}", basis),
                )
            conn.commit()


class TestARowTheConstraintWillNotTake:
    """The one way 013 can fail against a real warehouse, and the only one that
    needs no crash: `basis` was a bare TEXT column until now, so whatever is in
    it is whatever was written."""

    def _with_a_bad_basis(self, tmp_path: Path) -> Path:
        db = _at_012(tmp_path)
        with _open(db) as conn:
            conn.executemany(
                "INSERT INTO scheme_aum (scheme_id, as_of_date, basis) VALUES (?,?,?)",
                [
                    ("INF1", "2026-06-30", "quarterly_average"),
                    ("INF2", "2026-06-30", "quaterly_average"),  # V1-51's typo
                ],
            )
            conn.commit()
        return db

    def test_the_failure_names_the_migration(self, tmp_path: Path) -> None:
        """Every job calls `apply_migrations` at startup, so this stops the
        whole system. The raw sqlite error named neither the file nor the table
        -- it named `scheme_aum_next`, a scratch table the rollback had already
        removed by the time anyone went looking for it."""
        db = self._with_a_bad_basis(tmp_path)
        with pytest.raises(MigrationError, match=re.escape(M013.name)):
            apply_migrations(str(db))

    def test_the_failure_leaves_the_warehouse_exactly_as_it_was(
        self, tmp_path: Path
    ) -> None:
        db = self._with_a_bad_basis(tmp_path)
        with pytest.raises(MigrationError):
            apply_migrations(str(db))
        with _open(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM scheme_aum").fetchone()[0] == 2
            assert not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'scheme_aum_next'"
            ).fetchone()
            assert not conn.execute(
                "SELECT 1 FROM schema_migration WHERE name = ?", (M013.name,)
            ).fetchone(), "a migration that failed must not count as applied"

    def test_removing_the_row_is_all_it_takes(self, tmp_path: Path) -> None:
        """The recovery the migration's comment documents. It has to work, or
        the note is the same unexecuted prose this whole file exists over."""
        db = self._with_a_bad_basis(tmp_path)
        with pytest.raises(MigrationError):
            apply_migrations(str(db))
        with _open(db) as conn:
            bad = conn.execute(
                "SELECT scheme_id FROM scheme_aum"
                " WHERE basis NOT IN ('point_in_time', 'quarterly_average')"
            ).fetchall()
            assert [r[0] for r in bad] == ["INF2"]
            conn.execute("DELETE FROM scheme_aum WHERE scheme_id = 'INF2'")
            conn.commit()
        assert apply_migrations(str(db)) == [M013.name]


class TestTheRebuildSurvivesAWarehouseWithRowsInIt:
    """SQLite cannot ALTER a CHECK onto a column, so 013 is a table rebuild.
    Every property below is one the empty-database fixtures cannot observe."""

    def test_every_row_survives_the_rebuild(self, tmp_path: Path) -> None:
        db = _at_012(tmp_path)
        with _open(db) as conn:
            conn.executemany(
                "INSERT INTO scheme_aum (scheme_id, as_of_date, aum_inr, folio_count,"
                " basis, period_label) VALUES (?,?,?,?,'quarterly_average','Apr - Jun')",
                [(f"INF{i}", "2026-06-30", "1000.5000", i) for i in range(5)],
            )
            conn.commit()

        assert apply_migrations(str(db)) == [M013.name]
        with _open(db, decimals=True) as conn:
            kept = conn.execute(
                "SELECT scheme_id, aum_inr, folio_count, period_label FROM scheme_aum"
                " ORDER BY scheme_id"
            ).fetchall()
        assert len(kept) == 5
        assert str(kept[0][1]) == "1000.5000", "the Decimal text must survive verbatim"
        assert kept[0][3] == "Apr - Jun"

    def test_the_index_is_recreated_with_the_table(self, tmp_path: Path) -> None:
        """`DROP TABLE` takes the table's indexes with it, so a rebuild that
        forgets to recreate one silently costs every later reader a scan."""
        with _open(_loaded(tmp_path)) as conn:
            assert conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='index'"
                " AND name='ix_scheme_aum_date'"
            ).fetchone()

    def test_the_rebuilt_table_is_still_decimal_safe(self, tmp_path: Path) -> None:
        """SZ-13: a `DECIMAL` column gets NUMERIC affinity and rewrites a
        decimal string as a REAL. Retyping a table by hand is exactly where
        that gets lost."""
        with _open(_loaded(tmp_path), decimals=True) as conn:
            assert unsafe_decimal_columns(conn) == []

    def test_a_scratch_table_left_behind_does_not_wedge_the_chain(
        self, tmp_path: Path
    ) -> None:
        """A populated `scheme_aum_next` with no `schema_migration` row, from
        whatever cause -- a hand-run, or a copy of this file that predates its
        transaction. Without the leading `DROP TABLE IF EXISTS` the next run
        re-inserts the same rows into it: `UNIQUE constraint failed`, on that
        run and every run after it, blocking 014 and everything past it behind
        a table only manual surgery could clear."""
        db = _loaded(tmp_path)
        with _open(db) as conn:
            conn.execute("DELETE FROM schema_migration WHERE name = ?", (M013.name,))
            conn.commit()
            conn.executescript(_cut("-- CRASH WINDOW 1 --"))
            conn.commit()
            left = conn.execute("SELECT COUNT(*) FROM scheme_aum_next").fetchone()[0]
        assert left == 3

        assert apply_migrations(str(db)) == [M013.name]
        with _open(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM scheme_aum").fetchone()[0] == 3
            assert not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'scheme_aum_next'"
            ).fetchone(), "the scratch table must not outlive the rebuild"
        # And it stays clean: the second application is a no-op, not a failure.
        assert apply_migrations(str(db)) == []

    def test_a_crash_after_the_old_table_is_dropped_loses_nothing(
        self, tmp_path: Path
    ) -> None:
        """The window the leading DROP opened rather than closed. Killed
        between `DROP TABLE scheme_aum` and the RENAME, the ONLY copy of the
        data sits in `scheme_aum_next` -- which the next run's
        `DROP TABLE IF EXISTS` then destroys. Statement order cannot close both
        windows at once; a transaction can, and SQLite has transactional DDL.
        """
        db = _loaded(tmp_path)
        with _open(db) as conn:
            conn.execute("DELETE FROM schema_migration WHERE name = ?", (M013.name,))
            conn.commit()
            conn.executescript(_cut("-- CRASH WINDOW 2 --"))
            atomic = conn.in_transaction
            # Leaving this block without committing is the process dying.

        # Unwrapped, `scheme_aum` is already gone by now and the only copy of
        # the rows is in a scratch table the next run drops on sight.
        with _open(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM scheme_aum").fetchone()[0] == 3, (
                "the rollback must leave the source table exactly as it was"
            )
            assert not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'scheme_aum_next'"
            ).fetchone()

        assert apply_migrations(str(db)) == [M013.name]
        with _open(db) as conn:
            assert conn.execute("SELECT COUNT(*) FROM scheme_aum").fetchone()[0] == 3
        assert atomic, "the rebuild has to be one unit, not five"


class TestAnAppliedMigrationIsMarkedWithIt:
    """`schema_migration` rows were inserted in the loop and committed only
    once every file had run, so one failure rolled back the markers for all the
    migrations before it while their DDL stood."""

    def test_a_later_failure_does_not_unmark_an_earlier_success(
        self, tmp_path: Path
    ) -> None:
        """014 here is a migration that cannot run. 013 has already rebuilt the
        table by then, and re-applying a rebuild against a schema a later file
        has changed is how a column gets silently dropped -- 013's own column
        list is 012's."""
        staged = tmp_path / "migrations_plus_broken"
        staged.mkdir()
        for src in sorted(MIGRATIONS.glob("[0-9][0-9][0-9]_*.sql")):
            shutil.copy(src, staged / src.name)
        (staged / "014_broken.sql").write_text(
            "SELECT * FROM a_table_that_does_not_exist;", encoding="utf-8"
        )

        db = tmp_path / "canonical.db"
        with pytest.raises(MigrationError, match=re.escape("014_broken.sql")):
            apply_migrations(str(db), staged)

        with _open(db) as conn:
            marked = {
                str(r[0]) for r in conn.execute("SELECT name FROM schema_migration")
            }
        assert M013.name in marked, "013 ran; it must not be a candidate to re-run"
        assert "014_broken.sql" not in marked
