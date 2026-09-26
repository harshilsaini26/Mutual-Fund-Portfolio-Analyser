"""NAV history per fund from mfapi.in (S6): named funds, or every live fund.

    python -m jobs.backfill_scheme_nav --scheme INF179K01UT0 INF204K01E54
    python -m jobs.backfill_scheme_nav --held               # funds with holdings loaded
    python -m jobs.backfill_scheme_nav --universe --missing # every live fund still short

AMFI's own history export is keyed on the fund house, so one fund means fetching
the whole house. mfapi is per fund: one request returns a fund's whole history.

**It fills gaps and never overwrites.** S6 is a mirror and AMFI stays the source
of record (V1-19): where both have a date, AMFI's value stands.

**`--universe --missing` is the daily build's history step** (DECISIONS V1-75):
every live fund with a Direct plan (`m0_data.universe`), fetched only when its
history is short -- none at all, starting well after launch, only the daily
file's days, a recent gap, or behind the newest daily file. A fund loaded once
stays loaded (the build keeps it in its store), so an ordinary day fetches
nothing and a missed day fetches the funds it left behind.

**One fund's failure is that fund's.** A 404, a malformed payload or a robots
refusal is recorded against the fund and the run carries on; the job ends
`partial` with the list. A run over 1,800 funds cannot hang on one.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
from src.common.decimals import connect
from src.m0_data.config import raw_root, source, warehouse_path
from src.m0_data.derive.nav_adj import build_all_nav_adj
from src.m0_data.derive.scheme_family import disclosed_scheme_ids
from src.m0_data.fetch.base import (
    DomainRateLimiter,
    FetchCandidate,
    RobotsCache,
    archive,
    conditional_get,
)
from src.m0_data.load import load_navs_where_absent
from src.m0_data.parse.nav.mfapi import (
    PARSER_ID,
    PARSER_VERSION,
    MfapiParseError,
    parse_mfapi,
)
from src.m0_data.schema.apply import apply_migrations
from src.m0_data.universe import live_funds

SOURCE_ID = "S6"

#: Direct plans began on 1 January 2013; nothing a Direct fund holds is older.
DIRECT_PLANS_BEGAN = date(2013, 1, 1)
#: A history starting this long after the fund's launch is missing its start.
LATE_START_DAYS = 31
#: Prices on fewer than this share of calendar days is a series built from the
#: daily file alone. A complete one prices ~68% of days (weekdays less holidays).
SPARSE_SHARE = 0.5
#: A stretch without prices longer than any market closure, looked for in the
#: last month only: an older one is a fund that was suspended, not a missed day.
GAP_DAYS = 5
RECENT_DAYS = 31
#: A fund mfapi was asked for this recently has everything mfapi has: its start
#: cannot move earlier, nor its last price later, by asking again. Measured on
#: the first full build: 206 of 1,864 funds stay "short" after a full fetch
#: (mfapi's history for them begins late, or AMFI stopped pricing them).
FETCHED_RECENTLY = timedelta(days=30)
GAP = "a {}-day gap in the last month"


@dataclass(frozen=True)
class Coverage:
    first: date | None
    last: date | None
    rows: int
    recent_gap: int
    inception: date | None
    #: The last day mfapi was asked for this fund (`raw_file` 'S6:<code>').
    fetched: date | None = None


def _day(value: Any) -> date | None:
    return None if value is None else date.fromisoformat(str(value)[:10])


def coverage(conn: sqlite3.Connection, scheme_ids: list[str]) -> dict[str, Coverage]:
    """What each fund's stored history looks like, in one pass."""
    wanted = set(scheme_ids)
    fetched = {
        str(source)[len(SOURCE_ID) + 1:]: _day(at)
        for source, at in conn.execute(
            "SELECT source_id, max(fetched_at) FROM raw_file"
            " WHERE source_id LIKE ? GROUP BY source_id", (f"{SOURCE_ID}:%",),
        )
    }
    out: dict[str, Coverage] = {}
    for sid, first, last, rows, inception, code in conn.execute(
        "SELECT s.scheme_id, min(n.nav_date), max(n.nav_date), count(n.nav_date),"
        " s.inception_date, s.amfi_code FROM scheme s"
        " LEFT JOIN nav_daily n USING (scheme_id) GROUP BY s.scheme_id"
    ):
        if sid in wanted:
            out[sid] = Coverage(_day(first), _day(last), int(rows), 0, _day(inception),
                                fetched.get(str(code)))
    newest = max((c.last for c in out.values() if c.last), default=None)
    if newest is None:
        return out
    since = newest - timedelta(days=RECENT_DAYS)
    for sid, gap in conn.execute(
        "SELECT scheme_id, max(gap) FROM ("
        "  SELECT scheme_id, julianday(nav_date)"
        "    - julianday(lag(nav_date) OVER (PARTITION BY scheme_id ORDER BY nav_date))"
        "    AS gap FROM nav_daily WHERE nav_date >= ?"
        ") GROUP BY scheme_id",
        (since.isoformat(),),
    ):
        if sid in out and gap is not None:
            c = out[sid]
            out[sid] = Coverage(c.first, c.last, c.rows, int(gap), c.inception,
                                c.fetched)
    return out


def short_reason(c: Coverage, newest: date) -> str | None:
    """Why a fund's history needs fetching, or None when it is complete."""
    if c.first is None or c.last is None:
        return "no prices"
    if c.last < newest - timedelta(days=GAP_DAYS):
        return "behind the newest daily file"
    start = max(c.inception or DIRECT_PLANS_BEGAN, DIRECT_PLANS_BEGAN)
    if c.first > start + timedelta(days=LATE_START_DAYS):
        return "starts after the fund's launch"
    if c.rows < (c.last - c.first).days * SPARSE_SHARE:
        return "only the daily file's days"
    if c.recent_gap > GAP_DAYS:
        return GAP.format(c.recent_gap)
    return None


def missing(
    conn: sqlite3.Connection, scheme_ids: list[str], today: date | None = None
) -> list[str]:
    """The funds among `scheme_ids` whose history is short and worth asking for.

    A fund mfapi answered within `FETCHED_RECENTLY` is not asked again: it
    mirrors AMFI, so what it lacked then it lacks now. Except a recent gap,
    which a missed day of our own makes and one fetch fills.
    """
    found = coverage(conn, scheme_ids)
    newest = max((c.last for c in found.values() if c.last), default=None)
    if newest is None:
        return list(scheme_ids)
    today = today or date.today()
    out = []
    for sid in scheme_ids:
        c = found.get(sid)
        why = short_reason(c, newest) if c else "no prices"
        if why is None:
            continue
        # The gap is read directly, not from `why`: a fund can be short for two
        # reasons, and `short_reason` names only the first.
        fetched = c.fetched if c else None
        if (fetched and today - fetched < FETCHED_RECENTLY
                and c is not None and c.recent_gap <= GAP_DAYS):
            continue
        out.append(sid)
    return out


def run(
    scheme_ids: list[str],
    progress: Callable[[str], None] = lambda line: None,
) -> list[dict[str, object]]:
    cfg = source(SOURCE_ID)
    db_path = warehouse_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(str(db_path))
    conn = connect(str(db_path))

    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO job_run (run_id, job_name, started_at, params_json)"
        " VALUES (?,?,?,?)",
        (run_id, "backfill_scheme_nav", datetime.now(UTC),
         json.dumps({"schemes": len(scheme_ids)})),
    )
    conn.commit()

    limiter = DomainRateLimiter(float(cfg["rate_limit_per_sec"]), int(cfg["burst"]))
    robots = RobotsCache()
    summaries: list[dict[str, object]] = []
    try:
        for n, scheme_id in enumerate(scheme_ids, start=1):
            try:
                summaries.append(_one(conn, scheme_id, cfg, limiter, robots))
            except (httpx.HTTPError, MfapiParseError, ValueError) as exc:
                # This fund's failure is this fund's: record it and carry on.
                conn.rollback()
                summaries.append(
                    {"scheme_id": scheme_id, "error": f"{type(exc).__name__}: {exc}"}
                )
            if n % 50 == 0 or n == len(scheme_ids):
                failed = sum(1 for s in summaries if s.get("error"))
                progress(f"  {n:,} of {len(scheme_ids):,} funds ({failed} failed)")
        added = sum(int(str(s.get("added", 0))) for s in summaries)
        if added:
            # Only the daily loaders rebuilt the total-return series; a fund
            # filled here would otherwise keep `nav_adj` NULL until the next.
            build_all_nav_adj(conn)
        errors = [s for s in summaries if s.get("error")]
        conn.execute(
            "UPDATE job_run SET finished_at=?, status=?, files_parsed=?,"
            " rows_written=?, error_text=? WHERE run_id=?",
            (datetime.now(UTC), "partial" if errors else "ok", len(summaries),
             added, "; ".join(f"{s['scheme_id']}: {s['error']}" for s in errors[:20])
             or None, run_id),
        )
        conn.commit()
        return summaries
    except Exception as exc:
        conn.execute(
            "UPDATE job_run SET finished_at=?, status='failed', error_text=?"
            " WHERE run_id=?",
            (datetime.now(UTC), f"{type(exc).__name__}: {exc}", run_id),
        )
        conn.commit()
        raise
    finally:
        conn.close()


def _one(
    conn: sqlite3.Connection,
    scheme_id: str,
    cfg: dict[str, Any],
    limiter: DomainRateLimiter,
    robots: RobotsCache,
) -> dict[str, object]:
    row = conn.execute(
        "SELECT amfi_code, scheme_name FROM scheme WHERE scheme_id = ?", (scheme_id,)
    ).fetchone()
    if row is None or not row[0]:
        return {"scheme_id": scheme_id, "error": "no amfi_code in scheme master"}
    amfi_code = str(row[0])

    url = str(cfg["url"]).format(code=amfi_code)
    if not robots.allows(url, str(cfg["user_agent"])):
        return {"scheme_id": scheme_id, "error": "robots.txt forbids"}

    response = conditional_get(
        url,
        user_agent=str(cfg["user_agent"]),
        timeout_connect=float(cfg["timeout_connect"]),
        timeout_read=float(cfg["timeout_read"]),
        retries=int(cfg["retries"]),
        from_email=str(cfg.get("from_email") or "") or None,
        limiter=limiter,
    )
    response.raise_for_status()
    content = response.content

    result, path = archive(
        content, FetchCandidate(url=url, source_id=f"{SOURCE_ID}:{amfi_code}"),
        "application/json", raw_root(), lambda fid: _archived(conn, fid),
    )
    if path is not None:
        conn.execute(
            "INSERT INTO raw_file (file_id, source_id, url, fetched_at, byte_size,"
            " storage_path, parse_status) VALUES (?,?,?,?,?,?, 'pending')",
            (result.file_id, f"{SOURCE_ID}:{amfi_code}", url,
             datetime.now(UTC), result.byte_size, str(path)),
        )
        conn.commit()

    parsed = parse_mfapi(content, scheme_id, amfi_code)
    added = load_navs_where_absent(conn, parsed.navs, str(result.file_id))
    conn.execute(
        "UPDATE raw_file SET parse_status='ok', parser_id=?, parser_version=?,"
        " parsed_at=? WHERE file_id=?",
        (PARSER_ID, PARSER_VERSION, datetime.now(UTC), result.file_id),
    )
    conn.commit()

    return {
        "scheme_id": scheme_id,
        "amfi_code": amfi_code,
        "fetch": result.status,
        "bytes": result.byte_size,
        "offered": len(parsed.navs),
        # Offered minus added is how many dates AMFI already had. A mirror that
        # adds nothing is a mirror confirming the publisher, which is useful to
        # see rather than to hide.
        "added": added,
        "already_held": len(parsed.navs) - added,
        "range": f"{parsed.first}..{parsed.last}",
        "warnings": len(parsed.warnings),
    }


def _archived(conn: sqlite3.Connection, file_id: str) -> bool:
    row = conn.execute(
        "SELECT storage_path FROM raw_file WHERE file_id = ?", (file_id,)
    ).fetchone()
    return bool(row) and Path(row[0]).exists()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheme", nargs="+", help="scheme_id (ISIN) to backfill")
    parser.add_argument(
        "--held", action="store_true",
        help="every scheme with a current disclosure loaded",
    )
    parser.add_argument(
        "--universe", action="store_true",
        help="every live fund with a Direct plan (m0_data.universe)",
    )
    parser.add_argument(
        "--missing", action="store_true",
        help="only the funds whose history is short",
    )
    parser.add_argument("--quiet", action="store_true", help="progress lines only")
    args = parser.parse_args()

    conn = connect(str(warehouse_path()))
    try:
        scheme_ids = list(args.scheme or [])
        if args.held:
            scheme_ids = sorted(set(scheme_ids) | set(disclosed_scheme_ids(conn)))
        if args.universe:
            scheme_ids = sorted(set(scheme_ids) | {f.scheme_id for f in live_funds(conn)})
        asked = len(scheme_ids)
        if args.missing:
            scheme_ids = missing(conn, scheme_ids)
    finally:
        conn.close()
    if args.missing:
        print(f"{len(scheme_ids):,} of {asked:,} funds have short history; fetching them")
    if not scheme_ids:
        if not args.missing:
            raise SystemExit(
                "nothing to do: pass --scheme <ISIN>..., --held or --universe"
            )
        return

    summaries = run(scheme_ids, progress=print)
    errors = [s for s in summaries if s.get("error")]
    for summary in errors if args.quiet else summaries:
        print(" | ".join(f"{k}={v}" for k, v in summary.items()))
    print(f"added {sum(int(str(s.get('added', 0))) for s in summaries):,} prices;"
          f" {len(errors)} fund(s) failed")


if __name__ == "__main__":
    main()
