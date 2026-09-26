"""Load AMFI's expense ratios into `scheme_ter`. DECISIONS V1-78.

    python -m jobs.fetch_ter                     # last month, again
    python -m jobs.fetch_ter --if-missing        # last month, unless it is loaded
    python -m jobs.fetch_ter --month 2026-08     # a named month

One workbook per finished month (`src/m0_data/fetch/amfi_ter.py`, ~4 MB), and
from it one figure per fund and plan: the one on the month's last published day
(migration 017 says why monthly). Each is joined to its live share classes by
`family_key` and plan, so a fund's Direct Growth and Direct IDCW both carry its
Direct TER. A name that joins nothing, or a family two fund houses share, is
counted and reported, never guessed.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

from src.common.decimals import connect
from src.m0_data.config import raw_root, source, warehouse_path
from src.m0_data.fetch.amfi_ter import FundTer, month_url, parse_ter
from src.m0_data.fetch.base import (
    DomainRateLimiter,
    FetchCandidate,
    RobotsCache,
    archive,
    conditional_get,
)
from src.m0_data.normalise.family import family_key
from src.m0_data.schema.apply import apply_migrations
from src.m0_data.universe import LIVE_WINDOW_DAYS

SOURCE_ID = "S14"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"

#: (family_key, plan): what a fund-level figure and a share class share.
Key = tuple[str, str]


@dataclass(frozen=True)
class Loaded:
    month: str
    day: date
    funds: int
    unjoined: int
    conflicting: int
    share_classes: int
    added: int


def last_month(today: date) -> date:
    """The last day of the newest month AMFI has finished publishing."""
    return today.replace(day=1) - timedelta(days=1)


def month_end(rows: list[FundTer]) -> tuple[dict[Key, FundTer], int]:
    """Each fund and plan's figure on the last day it is listed.

    AMFI spells some funds two ways within a month ("Axis ELSS Tax Saver Fund",
    "Axis ELSS- Tax Saver Fund"); `family_key` makes them one. Two spellings
    giving different figures on the same last day cannot both be right, so the
    pair is dropped and counted rather than one picked.
    """
    last: dict[Key, FundTer] = {}
    clash: set[Key] = set()
    for row in rows:
        key = (family_key(row.fund_name), row.plan)
        held = last.get(key)
        if held is None or row.day > held.day:
            last[key] = row
            clash.discard(key)
        elif row.day == held.day and (row.total, row.base) != (held.total, held.base):
            clash.add(key)
    for key in clash:
        del last[key]
    return last, len(clash)


def share_classes(conn: Any) -> dict[Key, list[str]]:
    """Live share classes by family and plan, for families of one fund house."""
    latest = conn.execute("SELECT max(last_seen) FROM scheme").fetchone()[0]
    if latest is None:
        return {}
    found: dict[Key, list[str]] = defaultdict(list)
    houses: dict[str, set[str]] = defaultdict(set)
    for sid, family, plan, amc in conn.execute(
        "SELECT scheme_id, scheme_family, plan, amc_id FROM scheme"
        " WHERE scheme_family IS NOT NULL AND last_seen >= date(?, ?)",
        (str(latest), f"-{LIVE_WINDOW_DAYS} days"),
    ):
        found[(str(family), str(plan))].append(str(sid))
        houses[str(family)].add(str(amc))
    return {k: v for k, v in found.items() if len(houses[k[0]]) == 1}


def load(conn: Any, figures: dict[Key, FundTer], file_id: str) -> tuple[int, int, int]:
    """Append what is new: (funds joined, share classes, rows added).

    A share class that already has this day's figure is left alone; one with a
    different figure for the same day gets the next revision (invariant 2).
    """
    classes = share_classes(conn)
    days = sorted({f.day for f in figures.values()})
    held: dict[tuple[str, date], tuple[int, Decimal, Decimal | None]] = {}
    for day in days:
        for sid, rev, total, base in conn.execute(
            "SELECT scheme_id, revision, total_ter, base_ter FROM scheme_ter"
            " WHERE valid_from = ? ORDER BY scheme_id, revision",
            (day,),
        ):
            held[(str(sid), day)] = (
                int(rev), Decimal(str(total)),
                None if base is None else Decimal(str(base)),
            )
    now = datetime.now(UTC)
    rows: list[tuple[object, ...]] = []
    joined = classes_seen = 0
    for key, fig in figures.items():
        members = classes.get(key, [])
        joined += bool(members)
        classes_seen += len(members)
        for sid in members:
            before = held.get((sid, fig.day))
            if before is not None and before[1:] == (fig.total, fig.base):
                continue
            rev = 1 if before is None else before[0] + 1
            rows.append((sid, fig.day, rev, fig.total, fig.base, file_id, now))
    conn.executemany(
        "INSERT INTO scheme_ter (scheme_id, valid_from, revision, total_ter,"
        " base_ter, source_file_id, ingested_at) VALUES (?,?,?,?,?,?,?)",
        rows,
    )
    return joined, classes_seen, len(rows)


def _fetch(url: str, client: Any = None) -> bytes:
    cfg = source(SOURCE_ID)
    response = conditional_get(
        url,
        user_agent=str(cfg["user_agent"]),
        timeout_connect=float(cfg["timeout_connect"]),
        timeout_read=float(cfg["timeout_read"]),
        retries=int(cfg["retries"]),
        from_email=str(cfg.get("from_email") or "") or None,
        limiter=DomainRateLimiter(float(cfg["rate_limit_per_sec"]), int(cfg["burst"])),
        robots=RobotsCache() if cfg.get("respect_robots") else None,
        client=client,
    )
    response.raise_for_status()
    return bytes(response.content)


def run(month: date, if_missing: bool = False, client: Any = None) -> Loaded | None:
    """`month` is any day in it. None when `if_missing` found it loaded."""
    db = warehouse_path()
    apply_migrations(str(db))
    conn = connect(str(db))
    try:
        if if_missing and conn.execute(
            "SELECT 1 FROM scheme_ter WHERE valid_from >= ? LIMIT 1",
            (month.replace(day=1),),
        ).fetchone():
            return None
        url = month_url(month)
        content = _fetch(url, client)
        result, path = archive(
            content, FetchCandidate(url=url, source_id=SOURCE_ID), XLSX, raw_root(),
            lambda fid: conn.execute(
                "SELECT 1 FROM raw_file WHERE file_id = ?", (fid,)
            ).fetchone() is not None,
        )
        if path is not None:
            conn.execute(
                "INSERT INTO raw_file (file_id, source_id, url, fetched_at,"
                " byte_size, storage_path, parse_status) VALUES (?,?,?,?,?,?, 'pending')",
                (result.file_id, SOURCE_ID, url, datetime.now(UTC),
                 result.byte_size, str(path)),
            )
        figures, conflicting = month_end(parse_ter(content))
        joined, classes, added = load(conn, figures, str(result.file_id))
        day = max(f.day for f in figures.values())
        conn.execute(
            "UPDATE raw_file SET parse_status='ok', parser_id='ter.amfi',"
            " parser_version='1', parsed_at=?, as_of_date=? WHERE file_id=?",
            (datetime.now(UTC), day, result.file_id),
        )
        conn.commit()
        return Loaded(f"{month:%Y-%m}", day, joined, len(figures) - joined,
                      conflicting, classes, added)
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--month", help="YYYY-MM; default the month just finished")
    parser.add_argument("--if-missing", action="store_true",
                        help="do nothing if that month is already loaded")
    args = parser.parse_args(argv)
    try:
        month = (date.fromisoformat(f"{args.month}-01") if args.month
                 else last_month(date.today()))
    except ValueError:
        raise SystemExit(f"--month {args.month!r} is not YYYY-MM") from None
    loaded = run(month, args.if_missing)
    if loaded is None:
        print(f"expense ratios for {month:%Y-%m} are already loaded")
        return 0
    print(
        f"expense ratios for {loaded.month}, as of {loaded.day}: {loaded.funds:,} fund"
        f" plans joined to {loaded.share_classes:,} share classes, {loaded.added:,}"
        f" rows added; {loaded.unjoined:,} not joined, {loaded.conflicting:,}"
        f" dropped for two different figures"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
