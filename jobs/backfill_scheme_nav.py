"""NAV history for the schemes you actually hold. OPEN-07, finally implementable.

    python -m jobs.backfill_scheme_nav --scheme INF179K01UT0 INF204K01E54
    python -m jobs.backfill_scheme_nav --held        # every scheme with a position

OPEN-07 wants full history for schemes the user holds, but **AMFI's export is
keyed on the AMC code** — asking for one fund means fetching the whole house.
That is how `nav_daily` came to hold 3,118,359 rows to serve the 5,924 belonging
to held schemes: 0.19%, and 584.9 MB of a 596 MB warehouse.

mfapi (S6) is per scheme, so this asks for exactly the schemes named. One request
each, through the same rate limiter and robots check as every other fetch.

**It fills gaps and never overwrites.** S6 is a mirror and AMFI stays the source
of record (V1-19): where both have a date, AMFI's value stands.

`jobs/backfill_nav.py` is unchanged and still right for bulk history when you
want a whole AMC.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.common.decimals import connect
from src.m0_data.config import raw_root, source, warehouse_path
from src.m0_data.fetch.base import (
    DomainRateLimiter,
    FetchCandidate,
    RobotsCache,
    archive,
    conditional_get,
)
from src.m0_data.load import load_navs_where_absent
from src.m0_data.parse.nav.mfapi import PARSER_ID, PARSER_VERSION, parse_mfapi
from src.m0_data.schema.apply import apply_migrations

SOURCE_ID = "S6"


def held_scheme_ids(conn: sqlite3.Connection) -> list[str]:
    """Schemes with a disclosure loaded, as a proxy for "held".

    The real answer lives in Zone B's `position`, which is encrypted and needs a
    key this job does not ask for. A scheme we bothered to load holdings for is
    the closest honest stand-in, and `--scheme` takes an explicit list when it
    is not.
    """
    return [
        r[0]
        for r in conn.execute(
            "SELECT DISTINCT scheme_id FROM holding_disclosure WHERE is_current = 1"
            " ORDER BY scheme_id"
        )
    ]


def run(scheme_ids: list[str]) -> list[dict[str, object]]:
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
         json.dumps({"schemes": scheme_ids})),
    )
    conn.commit()

    limiter = DomainRateLimiter(
        float(cfg["rate_limit_per_sec"]), int(cfg["burst"])
    )
    robots = RobotsCache()
    summaries: list[dict[str, object]] = []
    try:
        for scheme_id in scheme_ids:
            summaries.append(_one(conn, scheme_id, cfg, limiter, robots))
        conn.execute(
            "UPDATE job_run SET finished_at=?, status=?, files_parsed=?,"
            " rows_written=? WHERE run_id=?",
            (datetime.now(UTC),
             "partial" if any(s.get("error") for s in summaries) else "ok",
             len(summaries),
             sum(int(str(s.get("added", 0))) for s in summaries), run_id),
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
    args = parser.parse_args()

    scheme_ids = list(args.scheme or [])
    if args.held:
        conn = connect(str(warehouse_path()))
        try:
            scheme_ids = sorted(set(scheme_ids) | set(held_scheme_ids(conn)))
        finally:
            conn.close()
    if not scheme_ids:
        raise SystemExit("nothing to do: pass --scheme <ISIN>... or --held")

    for summary in run(scheme_ids):
        print(" | ".join(f"{k}={v}" for k, v in summary.items()))


if __name__ == "__main__":
    main()
