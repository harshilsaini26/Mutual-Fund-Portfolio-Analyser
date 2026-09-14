"""What a migration does to a warehouse that already holds rows.

`apply_migrations` runs in most fixtures in this suite, and always against a
fresh empty database -- the one path where a table rebuild has nothing to copy
and every statement trivially succeeds. So the suite was green while
`013_scheme_aum_basis.sql` shipped with a comment claiming it was safe to
re-run: killed between its INSERT and its DROP it wedged the whole chain, and
nothing here would have noticed.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.m0_data.schema.apply import apply_migrations, unsafe_decimal_columns

M013 = Path(__file__).resolve().parents[2] / "migrations" / "013_scheme_aum_basis.sql"


def _loaded(tmp_path: Path, rows: int = 3) -> Path:
    """A warehouse whose `scheme_aum` is populated."""
    db = tmp_path / "canonical.db"
    apply_migrations(str(db))
    conn = sqlite3.connect(str(db))
    conn.executemany(
        "INSERT INTO scheme_aum (scheme_id, as_of_date, aum_inr, basis)"
        " VALUES (?,?,?,'quarterly_average')",
        [(f"INF{i}", "2026-06-30", "1000") for i in range(rows)],
    )
    conn.commit()
    conn.close()
    return db


class TestTheBasisVocabularyLivesOnTheColumn:
    """`012` declared `point_in_time | quarterly_average` in a COMMENT and the
    column took any TEXT. §10's V2 raises `UnknownAumBasis` on anything else,
    so one unreadable row stops V2 for every disclosure that reads it."""

    def test_an_unknown_basis_is_refused_by_the_database(self, tmp_path: Path) -> None:
        conn = sqlite3.connect(str(_loaded(tmp_path)))
        with pytest.raises(sqlite3.IntegrityError, match="basis"):
            conn.execute(
                "INSERT INTO scheme_aum (scheme_id, as_of_date, basis)"
                " VALUES ('INFX','2026-06-30','quaterly_average')"
            )

    def test_both_spellings_the_readers_know_are_accepted(
        self, tmp_path: Path
    ) -> None:
        """The constraint has to admit exactly what `TOLERANCE_BY_BASIS` holds,
        or it trades one unreadable row for a load that cannot happen."""
        conn = sqlite3.connect(str(_loaded(tmp_path)))
        for i, basis in enumerate(("point_in_time", "quarterly_average")):
            conn.execute(
                "INSERT INTO scheme_aum (scheme_id, as_of_date, basis)"
                " VALUES (?,'2026-03-31',?)",
                (f"INFOK{i}", basis),
            )
        conn.commit()


class TestTheRebuildSurvivesAWarehouseWithRowsInIt:
    """SQLite cannot ALTER a CHECK onto a column, so 013 is a table rebuild.
    Every property below is one the empty-database fixtures cannot observe."""

    def test_every_row_survives_the_rebuild(self, tmp_path: Path) -> None:
        db = tmp_path / "canonical.db"
        apply_migrations(str(db))
        conn = sqlite3.connect(str(db))
        conn.executemany(
            "INSERT INTO scheme_aum (scheme_id, as_of_date, aum_inr, folio_count,"
            " basis, period_label) VALUES (?,?,?,?,'quarterly_average','Apr - Jun')",
            [(f"INF{i}", "2026-06-30", "1000.5000", i) for i in range(5)],
        )
        conn.commit()
        # Re-run the rebuild over populated data, which is what applying 013 to
        # the live warehouse does and what applying it to a fixture never does.
        conn.execute("DELETE FROM schema_migration WHERE name = ?", (M013.name,))
        conn.commit()
        conn.close()

        apply_migrations(str(db))
        conn = connect(str(db))
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
        conn = sqlite3.connect(str(_loaded(tmp_path)))
        assert conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index'"
            " AND name='ix_scheme_aum_date'"
        ).fetchone()

    def test_the_rebuilt_table_is_still_decimal_safe(self, tmp_path: Path) -> None:
        """SZ-13: a `DECIMAL` column gets NUMERIC affinity and rewrites a
        decimal string as a REAL. Retyping a table by hand is exactly where
        that gets lost."""
        conn = connect(str(_loaded(tmp_path)))
        assert unsafe_decimal_columns(conn) == []

    def test_an_interrupted_rebuild_does_not_wedge_the_chain(
        self, tmp_path: Path
    ) -> None:
        """`executescript` is autocommit, so a process killed between the
        INSERT and the DROP leaves `scheme_aum_next` populated with no
        `schema_migration` row. Without the leading `DROP TABLE IF EXISTS` the
        next run re-inserted into it -- `UNIQUE constraint failed`, on that run
        and every run after it, blocking 014 and everything past it behind a
        table only manual surgery could clear."""
        db = _loaded(tmp_path)
        conn = sqlite3.connect(str(db))
        conn.execute("DELETE FROM schema_migration WHERE name = ?", (M013.name,))
        conn.commit()
        # Everything up to the DROP: the copy is made, nothing is swapped.
        half = M013.read_text(encoding="utf-8").split("DROP TABLE scheme_aum;")[0]
        conn.executescript(half)
        conn.commit()
        assert conn.execute("SELECT COUNT(*) FROM scheme_aum_next").fetchone()[0] == 3
        conn.close()

        assert apply_migrations(str(db)) == [M013.name]
        conn = sqlite3.connect(str(db))
        assert conn.execute("SELECT COUNT(*) FROM scheme_aum").fetchone()[0] == 3
        assert not conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'scheme_aum_next'"
        ).fetchone(), "the scratch table must not outlive the rebuild"
        # And it stays clean: the second application is a no-op, not a failure.
        conn.close()
        assert apply_migrations(str(db)) == []
