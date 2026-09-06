"""Build a warehouse holding NAV history only for the schemes named.

    python -m scripts.thin_warehouse --held --out data/warehouse/thin.db

OPEN-07 asked for full history on held schemes and nothing gratuitous
elsewhere. `jobs/backfill_nav.py` could not deliver that because AMFI's history
export is per AMC code, so the warehouse accumulated 3,118,359 NAV rows to serve
the 5,924 that belong to schemes anyone holds — 0.19%, and 584.9 MB of a 596 MB
file. `jobs/backfill_scheme_nav.py` fixes the *fetching*; this fixes what is
already stored.

**It never touches the source.** A new database is created and rows are copied
into it; the original is opened read-only. Verify the thin copy answers the same
questions, then switch `MF_WAREHOUSE` to it — and keep the fat one until you are
sure, because re-fetching 3.1M NAVs is an overnight job and deleting them is a
keystroke.
"""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from src.common.decimals import connect
from src.m0_data.config import warehouse_path
from src.m0_data.schema.apply import apply_migrations, assert_schema_is_decimal_safe

#: Copied whole. `nav_daily` is the only table filtered — everything else is
#: already small, and pruning the scheme master would break resolution for any
#: fund the user buys next.
FILTERED = "nav_daily"


def scheme_ids_to_keep(conn: sqlite3.Connection, explicit: list[str]) -> list[str]:
    """The named schemes, plus every scheme with a current disclosure loaded."""
    held = [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT scheme_id FROM holding_disclosure WHERE is_current = 1"
        )
    ]
    return sorted(set(explicit) | set(held))


def copyable_tables(conn: sqlite3.Connection) -> list[str]:
    return [
        r[0]
        for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
            " AND name NOT LIKE 'sqlite_%' ORDER BY name"
        )
    ]


def build(source_db: Path, target: Path, keep: list[str]) -> dict[str, int]:
    if target.exists():
        raise SystemExit(f"{target} exists; remove it or choose another --out")
    target.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(str(target))
    assert_schema_is_decimal_safe(str(target))

    out = connect(str(target))
    out.execute("ATTACH DATABASE ? AS src", (str(source_db),))
    counts: dict[str, int] = {}
    try:
        placeholders = ",".join("?" * len(keep))
        for table in copyable_tables(out):
            if table == "schema_migration":
                continue
            columns = [r[1] for r in out.execute(f"PRAGMA table_info({table})")]
            if not columns:
                continue
            names = ", ".join(columns)
            # OR REPLACE because the migrations seed rows of their own —
            # `issuer` carries the nine synthetic issuers from 003 — and the
            # source is authoritative for anything that collides.
            if table == FILTERED:
                cursor = out.execute(
                    f"INSERT OR REPLACE INTO {table} ({names})"
                    f" SELECT {names} FROM src.{table}"
                    f" WHERE scheme_id IN ({placeholders})",
                    keep,
                )
            else:
                cursor = out.execute(
                    f"INSERT OR REPLACE INTO {table} ({names})"
                    f" SELECT {names} FROM src.{table}"
                )
            counts[table] = cursor.rowcount if cursor.rowcount > 0 else 0
        out.commit()
    except Exception:
        # Without this the DETACH below fails too, and the real error is buried
        # under "database src is locked".
        out.rollback()
        raise
    finally:
        out.execute("DETACH DATABASE src")
    # Reclaims the pages the copy never used; without it the file is sized for
    # the inserts rather than the rows.
    out.execute("VACUUM")
    out.close()
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheme", nargs="*", default=[], help="extra scheme_ids")
    parser.add_argument(
        "--held", action="store_true", help="keep every scheme with a disclosure"
    )
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()

    source_db = warehouse_path()
    conn = connect(str(source_db))
    keep = scheme_ids_to_keep(conn, args.scheme) if args.held else sorted(args.scheme)
    before_navs = conn.execute("SELECT count(*) FROM nav_daily").fetchone()[0]
    conn.close()
    if not keep:
        raise SystemExit("nothing to keep: pass --held or --scheme <ISIN>...")

    counts = build(source_db, args.out, keep)

    fat = source_db.stat().st_size
    thin = args.out.stat().st_size
    print(f"\nkeeping NAV history for {len(keep)} schemes:")
    for scheme_id in keep:
        print(f"    {scheme_id}")
    print(f"\n  nav_daily   {before_navs:>10,} -> {counts.get('nav_daily', 0):>10,}")
    for table, n in sorted(counts.items()):
        if table != "nav_daily" and n:
            print(f"  {table:<24} {n:>10,}")
    print(f"\n  {source_db.name:<24} {fat / 1e6:>9.1f} MB")
    print(f"  {args.out.name:<24} {thin / 1e6:>9.1f} MB"
          f"   ({fat / thin:.0f}x smaller)")
    print("\nthe original is untouched. To use the thin one:")
    print(f"    MF_WAREHOUSE={args.out} \\")
    print("        python -m scripts.show_lookthrough --equal 1000000\n")


if __name__ == "__main__":
    main()
