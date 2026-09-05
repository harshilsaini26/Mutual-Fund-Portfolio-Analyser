"""Historical NAV backfill. DECISIONS OPEN-07, `PLAN.md` §7 V0 step 2.

    python -m jobs.backfill_nav --amc hdfc icici kotak_mahindra --from 2024-01-01

A one-time overnight job, not an incremental task: it fills a fixed historical
range, and the leading edge belongs to `jobs/fetch_nav.py`. Safe to re-run —
each year is archived under its content hash and loaded by upsert, so a second
run over the same range writes nothing new.

`--from` is clamped to 31-Jan-2018 at the latest, because equity grandfathering
needs that day's NAV whatever the ledger's own history says (MODULE_1.md §7.5).
"""

from __future__ import annotations

import argparse
import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from src.common.decimals import connect
from src.m0_data.config import raw_root, source, warehouse_path
from src.m0_data.derive.nav_adj import build_all_nav_adj
from src.m0_data.fetch.amfi_history import HISTORY_URL, HistoryChunk, plan_backfill
from src.m0_data.fetch.base import (
    DomainRateLimiter,
    FetchCandidate,
    RobotsCache,
    archive,
    conditional_get,
)
from src.m0_data.load import load_parse_result
from src.m0_data.parse.nav.amfi import (
    PARSER_ID,
    PARSER_VERSION,
    AmfiParseError,
    parse_navall,
)
from src.m0_data.schema.apply import apply_migrations

SOURCE_ID = "S1:history"

#: AMFI's `mf` parameter is a dense-ish integer with gaps. Discovery probes this
#: range once and stores what it finds in `amc.amfi_amc_code`; an unknown code
#: returns an HTML error page rather than a 404, so "no data" is how a gap
#: presents and the probe has to read the response rather than the status.
DISCOVERY_RANGE = range(1, 60)


def discover_amc_codes(
    conn: Any, cfg: dict[str, Any], limit: int = 0
) -> int:
    """Fill `amc.amfi_amc_code` by probing one day per code.

    MODULE_0.md §5.4 calls discovery part of the problem, and it is here too:
    nothing AMFI publishes maps its own AMC names to the `mf` codes its history
    endpoint takes. The response body does — each file names the AMC it covers —
    so one cheap request per code recovers the mapping.

    Runs once. Codes already stored are skipped, so a re-run costs nothing.
    """
    from src.m0_data.load import normalise_amc_id

    known = {
        r[0]
        for r in conn.execute(
            "SELECT amfi_amc_code FROM amc WHERE amfi_amc_code IS NOT NULL"
        )
    }
    limiter = DomainRateLimiter(float(cfg["rate_limit_per_sec"]), int(cfg["burst"]))
    probe_day = date(2024, 1, 2)
    found = 0

    for code in DISCOVERY_RANGE:
        if str(code) in known:
            continue
        if limit and found >= limit:
            break
        chunk = HistoryChunk(probe_day, probe_day, str(code))
        response = conditional_get(
            f"{HISTORY_URL}?{_query(chunk)}",
            user_agent=str(cfg["user_agent"]),
            limiter=limiter,
            retries=1,
        )
        try:
            parsed = parse_navall(response.text.splitlines())
        except AmfiParseError:
            # An unknown code: HTTP 200 with an HTML error page. A gap in the
            # sequence, not a failure.
            continue
        names = {s.amc_name for s in parsed.schemes}
        for name in names:
            conn.execute(
                "UPDATE amc SET amfi_amc_code = ? WHERE amc_id = ?",
                (str(code), normalise_amc_id(name)),
            )
        found += 1
        print(f"  mf={code:<3} {', '.join(sorted(names))}")
    conn.commit()
    return found


def _query(chunk: HistoryChunk) -> str:
    return "&".join(f"{k}={v}" for k, v in chunk.params.items())


def run(
    amc_ids: list[str],
    start: date,
    today: date,
    discover: bool = True,
) -> dict[str, object]:
    cfg = source("S1")
    db_path = warehouse_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(str(db_path))
    conn = connect(str(db_path))

    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO job_run (run_id, job_name, started_at, params_json) "
        "VALUES (?, ?, ?, ?)",
        (run_id, "backfill_nav", datetime.now(UTC),
         json.dumps({"amcs": amc_ids, "from": str(start)})),
    )
    conn.commit()

    summary: dict[str, object] = {"run_id": run_id}
    try:
        if discover:
            print("discovering AMFI AMC codes...")
            discover_amc_codes(conn, cfg)

        codes = _codes_for(conn, amc_ids)
        missing = [a for a in amc_ids if a not in codes]
        if missing:
            # §2: fail loudly and name what was attempted. Backfilling three of
            # four AMCs and reporting success is the failure mode here.
            raise RuntimeError(f"no AMFI code known for {missing}; run discovery")

        chunks = plan_backfill(sorted(codes.values()), start, today)
        limiter = DomainRateLimiter(float(cfg["rate_limit_per_sec"]), int(cfg["burst"]))
        robots = RobotsCache() if cfg.get("respect_robots") else None
        totals = {"chunks": 0, "fetched": 0, "unchanged": 0, "nav_daily": 0,
                  "warnings": 0}

        for chunk in chunks:
            url = f"{HISTORY_URL}?{_query(chunk)}"
            response = conditional_get(
                url,
                user_agent=str(cfg["user_agent"]),
                timeout_connect=float(cfg["timeout_connect"]),
                timeout_read=float(cfg["timeout_read"]),
                retries=int(cfg["retries"]),
                limiter=limiter,
                robots=robots,
            )
            response.raise_for_status()

            candidate = FetchCandidate(url=url, source_id=SOURCE_ID)
            result, path = archive(
                response.content, candidate, response.headers.get("content-type"),
                raw_root(), lambda fid: _archived(conn, fid),
            )
            totals["chunks"] += 1
            totals[result.status if result.status in totals else "fetched"] += 1

            if path is not None:
                conn.execute(
                    "INSERT INTO raw_file (file_id, source_id, url, as_of_date,"
                    " fetched_at, content_type, byte_size, storage_path, parse_status)"
                    " VALUES (?,?,?,?,?,?,?,?, 'pending')",
                    (result.file_id, SOURCE_ID, url, chunk.end, datetime.now(UTC),
                     response.headers.get("content-type"), result.byte_size, str(path)),
                )
                conn.commit()

            text = _archived_text(conn, result.file_id)
            parsed = parse_navall(text.splitlines())
            counts = load_parse_result(conn, parsed, result.file_id, today)
            conn.execute(
                "UPDATE raw_file SET parse_status='ok', parser_id=?, parser_version=?,"
                " parsed_at=? WHERE file_id=?",
                (PARSER_ID, PARSER_VERSION, datetime.now(UTC), result.file_id),
            )
            conn.commit()
            totals["nav_daily"] += counts["nav_daily"]
            totals["warnings"] += counts["warnings"]
            print(f"  {chunk.label:34} {result.status:9} "
                  f"{result.byte_size:>10,}B  navs={counts['nav_daily']:>7,}")

        print("building nav_adj...")
        totals["nav_adj"] = build_all_nav_adj(conn)
        conn.commit()

        summary |= totals
        status = "partial" if totals["warnings"] else "ok"
        conn.execute(
            "UPDATE job_run SET finished_at=?, status=?, files_fetched=?,"
            " rows_written=?, warnings=? WHERE run_id=?",
            (datetime.now(UTC), status, totals["fetched"], totals["nav_daily"],
             totals["warnings"], run_id),
        )
        conn.commit()
        summary["status"] = status
        return summary

    except Exception as exc:
        conn.execute(
            "UPDATE job_run SET finished_at=?, status='failed', error_text=? "
            "WHERE run_id=?",
            (datetime.now(UTC), f"{type(exc).__name__}: {exc}", run_id),
        )
        conn.commit()
        raise
    finally:
        conn.close()


def _codes_for(conn: Any, amc_ids: list[str]) -> dict[str, str]:
    rows = conn.execute(
        "SELECT amc_id, amfi_amc_code FROM amc WHERE amfi_amc_code IS NOT NULL"
    ).fetchall()
    return {a: c for a, c in rows if a in amc_ids}


def _archived(conn: Any, file_id: str) -> bool:
    row = conn.execute(
        "SELECT storage_path FROM raw_file WHERE file_id = ?", (file_id,)
    ).fetchone()
    return bool(row) and Path(row[0]).exists()


def _archived_text(conn: Any, file_id: str | None) -> str:
    row = conn.execute(
        "SELECT storage_path FROM raw_file WHERE file_id = ?", (file_id,)
    ).fetchone()
    if not row:
        raise FileNotFoundError(f"no archived file for {file_id}")
    return Path(row[0]).read_text(encoding="utf-8", errors="strict")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amc", nargs="+", required=True,
                        help="amc_id values, e.g. hdfc icici_prudential")
    parser.add_argument("--from", dest="start", required=True,
                        help="YYYY-MM-DD; clamped to 31-Jan-2018 at the latest")
    parser.add_argument("--no-discover", action="store_true",
                        help="skip AMC code discovery (codes must already be stored)")
    args = parser.parse_args()

    result = run(
        amc_ids=args.amc,
        start=date.fromisoformat(args.start),
        today=date.today(),
        discover=not args.no_discover,
    )
    for key, value in result.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
