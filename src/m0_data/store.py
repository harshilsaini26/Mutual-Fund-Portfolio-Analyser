"""The build's store: every live fund's prices, compact, carried inside the site.

DECISIONS V1-75. The daily build runs in a fresh machine each time, with no
warehouse and no archive. What it keeps between runs is this: one small file per
fund, published with the site under `data/nav/`, read back by the next build so
that only today's prices (and any fund still short) need fetching.

    data/nav/118955.csv.gz
      # scheme_id=INF179K01UT0 amfi_code=118955
      2013-01-01,100.000000
      ...

Named by AMFI code, which every scheme has; a scheme listed without an ISIN is
keyed `AMFI:<code>:...`, no file name on Windows. Published prices only -- an
interpolated row is the build's own and is rebuilt, not kept.

**A cache, not a source of record.** AMFI's daily file and mfapi remain the
sources; a file that fails any check here is refused whole, and the build fetches
that fund again. Written deterministically (gzip with no timestamp), so a fund
whose prices did not change writes the same bytes.

**When each fund was last fetched from mfapi** (`data/nav/fetched.csv`, a URL
and a date per fund) is carried too: without it every build would ask again for
the ~200 funds whose history mfapi itself starts late or ends early.

**The aggregator's holdings** (DECISIONS V1-79) are the other thing a build
cannot fetch again in full each day: Groww's pages are read at most 100 a day.
`data/holdings/` carries each current aggregator disclosure, its rows, and the
`raw_file` rows they cite -- URL, date and SHA-256, not the page -- one gzipped
CSV per table, restored as they were written. A file whose columns are not the
table's today is refused and those pages are read again as the crawl reaches them.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import io
import re
import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from src.m0_data.universe import Fund

#: Where the store sits inside the site.
NAV_DIR = Path("data") / "nav"
HOLDINGS_DIR = Path("data") / "holdings"
#: Each table carried, and which of its rows: current aggregator disclosures.
_AGGREGATOR = ("SELECT 1 FROM holding_disclosure d WHERE d.source_tier = 'aggregator'"
               " AND d.is_current = 1")
HOLDINGS = {
    "raw_file": (f"EXISTS ({_AGGREGATOR} AND d.source_file_id = t.file_id)",
                 "file_id"),
    "holding_disclosure": ("t.source_tier = 'aggregator' AND t.is_current = 1",
                           "scheme_id, as_of_date, revision"),
    "holding": (f"t.is_current = 1 AND EXISTS ({_AGGREGATOR} AND d.scheme_id ="
                " t.scheme_id AND d.as_of_date = t.as_of_date AND d.revision ="
                " t.revision)", "scheme_id, as_of_date, revision, row_number"),
}
SOURCE_ID = "store"
_HEADER = re.compile(r"# scheme_id=(\S+) amfi_code=(\d+)")


class StoreError(ValueError):
    """A store file that cannot be trusted; its fund is fetched again."""


@dataclass(frozen=True)
class Series:
    scheme_id: str
    amfi_code: str
    navs: list[tuple[date, Decimal]]


@dataclass
class Restored:
    funds: int = 0
    rows: int = 0
    refused: list[tuple[str, str]] = field(default_factory=list)


def encode(series: Series) -> bytes:
    lines = [f"# scheme_id={series.scheme_id} amfi_code={series.amfi_code}"]
    lines += [f"{on.isoformat()},{nav}" for on, nav in series.navs]
    return gzip.compress(("\n".join(lines) + "\n").encode("utf-8"), mtime=0)


def decode(content: bytes) -> Series:
    """One file back to a series, or `StoreError` naming the first thing wrong."""
    try:
        text = gzip.decompress(content).decode("utf-8")
    except (OSError, EOFError, UnicodeDecodeError) as exc:
        raise StoreError(f"not a readable gzip file ({exc})") from exc
    lines = text.splitlines()
    head = _HEADER.fullmatch(lines[0]) if lines else None
    if head is None:
        raise StoreError("no header line naming the fund")
    navs: list[tuple[date, Decimal]] = []
    for number, line in enumerate(lines[1:], start=2):
        try:
            raw_date, raw_nav = line.split(",")
            on, nav = date.fromisoformat(raw_date), Decimal(raw_nav)
        except (ValueError, InvalidOperation) as exc:
            raise StoreError(
                f"line {number} is not a date and a price: {line!r}"
            ) from exc
        if not nav.is_finite() or nav <= 0:
            raise StoreError(f"line {number}: {nav} is not a price")
        if navs and on <= navs[-1][0]:
            raise StoreError(f"line {number}: {on} does not follow {navs[-1][0]}")
        navs.append((on, nav))
    if not navs:
        raise StoreError("no prices")
    return Series(head.group(1), head.group(2), navs)


def save(conn: sqlite3.Connection, root: Path, funds: list[Fund]) -> int:
    """Write every fund's published prices under `root/data/nav/`. Returns files."""
    folder = root / NAV_DIR
    folder.mkdir(parents=True, exist_ok=True)
    written = 0
    for fund in funds:
        if not fund.amfi_code:
            continue
        navs = [
            (date.fromisoformat(str(on)[:10]), Decimal(str(nav)))
            for on, nav in conn.execute(
                "SELECT nav_date, nav FROM nav_daily WHERE scheme_id = ?"
                " AND is_interpolated = 0 ORDER BY nav_date",
                (fund.scheme_id,),
            )
        ]
        if navs:
            (folder / f"{fund.amfi_code}.csv.gz").write_bytes(
                encode(Series(fund.scheme_id, fund.amfi_code, navs))
            )
            written += 1
    return written


def restore(conn: sqlite3.Connection, root: Path, funds: list[Fund]) -> Restored:
    """Load each live fund's stored prices where the warehouse has none for a date.

    Only the funds in `funds`, filed under the scheme_id the warehouse gives them
    today: a file whose header names another fund is refused rather than moved.
    Today's AMFI prices were loaded first and stand (AMFI is the record).
    """
    by_code = {f.amfi_code: f for f in funds if f.amfi_code}
    done = Restored()
    folder = root / NAV_DIR
    if not folder.is_dir():
        return done
    for path in sorted(folder.glob("*.csv.gz")):
        code = path.name.removesuffix(".csv.gz")
        fund = by_code.get(code)
        if fund is None:
            continue  # no longer live: dropped, not loaded
        content = path.read_bytes()
        try:
            series = decode(content)
            if (series.scheme_id, series.amfi_code) != (fund.scheme_id, code):
                raise StoreError(
                    f"names {series.scheme_id}/{series.amfi_code}, not "
                    f"{fund.scheme_id}/{code}"
                )
        except StoreError as exc:
            done.refused.append((fund.scheme_id, str(exc)))
            continue
        file_id = hashlib.sha256(content).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO raw_file (file_id, source_id, url, fetched_at,"
            " byte_size, storage_path, parse_status) VALUES (?,?,?,?,?,?, 'ok')",
            (file_id, SOURCE_ID, str(NAV_DIR / path.name), datetime.now(UTC),
             len(content), str(path)),
        )
        before = conn.total_changes
        conn.executemany(
            "INSERT INTO nav_daily (scheme_id, nav_date, nav, source_file_id)"
            " VALUES (?,?,?,?) ON CONFLICT(scheme_id, nav_date) DO NOTHING",
            [(fund.scheme_id, on, nav, file_id) for on, nav in series.navs],
        )
        done.rows += conn.total_changes - before
        done.funds += 1
    conn.commit()
    return done


FETCHED = NAV_DIR / "fetched.csv"


def save_fetched(conn: sqlite3.Connection, root: Path) -> int:
    """Each mfapi URL and the last day it was fetched ('S6:<code>' in raw_file)."""
    rows = conn.execute(
        "SELECT source_id, url, max(CAST(fetched_at AS TEXT)) FROM raw_file"
        " WHERE source_id LIKE 'S6:%' GROUP BY source_id ORDER BY source_id"
    ).fetchall()
    (root / FETCHED).parent.mkdir(parents=True, exist_ok=True)
    with (root / FETCHED).open("w", encoding="utf-8", newline="") as fh:
        out = csv.writer(fh, lineterminator="\n")
        out.writerow(["source_id", "url", "fetched_at"])
        out.writerows(rows)
    return len(rows)


def restore_fetched(conn: sqlite3.Connection, root: Path) -> int:
    """Put those fetches back in `raw_file`: the fact that each happened, dated,
    with no bytes kept (as for everything the build fetches)."""
    path = root / FETCHED
    if not path.is_file():
        return 0
    with path.open(encoding="utf-8", newline="") as fh:
        rows = [(f"fetched:{r['source_id']}:{r['fetched_at'][:10]}", r["source_id"],
                 r["url"], r["fetched_at"]) for r in csv.DictReader(fh)]
    conn.executemany(
        "INSERT OR IGNORE INTO raw_file (file_id, source_id, url, fetched_at,"
        " byte_size, storage_path, parse_status) VALUES (?,?,?,?, 0, '', 'ok')",
        rows,
    )
    conn.commit()
    return len(rows)


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(r[1]) for r in conn.execute(f"PRAGMA table_info({table})")]


def save_holdings(conn: sqlite3.Connection, root: Path) -> int:
    """Write the aggregator's current disclosures under `root/data/holdings/`.

    Every column as SQLite holds it, ordered by key; an empty field is NULL.
    Returns the number of disclosures.
    """
    folder = root / HOLDINGS_DIR
    folder.mkdir(parents=True, exist_ok=True)
    counts: dict[str, int] = {}
    for table, (where, order) in HOLDINGS.items():
        columns = _columns(conn, table)
        out = io.StringIO()
        writer = csv.writer(out, lineterminator="\n")
        writer.writerow(columns)
        rows = conn.execute(
            # As text, exactly as stored: no converter runs on a value that
            # is only being carried (a DECIMAL_TEXT keeps its digits, a
            # TIMESTAMP whatever form it was written in).
            f"SELECT {', '.join(f'CAST(t.{c} AS TEXT)' for c in columns)} FROM {table} t"
            f" WHERE {where} ORDER BY {order}"
        ).fetchall()
        writer.writerows(["" if v is None else v for v in row] for row in rows)
        (folder / f"{table}.csv.gz").write_bytes(
            gzip.compress(out.getvalue().encode("utf-8"), mtime=0)
        )
        counts[table] = len(rows)
    return counts["holding_disclosure"]


def restore_holdings(conn: sqlite3.Connection, root: Path) -> int:
    """Load what `save_holdings` wrote; the disclosures restored, or 0.

    All three files or none: a disclosure without its rows would read as a fund
    that holds nothing. A row already in the warehouse is left as it is.
    """
    folder = root / HOLDINGS_DIR
    loaded: dict[str, tuple[list[str], list[list[str]]]] = {}
    for table in HOLDINGS:
        path = folder / f"{table}.csv.gz"
        if not path.is_file():
            return 0
        try:
            rows = list(csv.reader(io.StringIO(
                gzip.decompress(path.read_bytes()).decode("utf-8"))))
        except (OSError, EOFError, UnicodeDecodeError, csv.Error) as exc:
            raise StoreError(f"{path.name}: not a readable gzip CSV ({exc})") from exc
        if not rows or rows[0] != _columns(conn, table):
            raise StoreError(f"{path.name}: its columns are not {table}'s")
        loaded[table] = (rows[0], rows[1:])
    for table, (columns, rows) in loaded.items():
        conn.executemany(
            f"INSERT OR IGNORE INTO {table} ({', '.join(columns)})"
            f" VALUES ({', '.join('?' * len(columns))})",
            [[None if v == "" else v for v in row] for row in rows],
        )
    conn.commit()
    return len(loaded["holding_disclosure"][1])


__all__ = ["FETCHED", "HOLDINGS_DIR", "NAV_DIR", "Restored", "Series", "StoreError",
           "decode", "encode", "restore", "restore_fetched", "restore_holdings",
           "save", "save_fetched", "save_holdings"]
