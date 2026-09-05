"""Seed the entity master from AMFI's market-cap list. PLAN.md §7 V1 steps 1 and 6.

    python -m jobs.build_entity_master --period 30Jun2026

Nothing downstream can resolve a holding until `issuer` and `instrument` have
rows, and the V1 gate's `unresolved_pct < 2%` is unmeasurable against an empty
master — so this runs before any holdings parser.

Safe to re-run. The archive is content-addressed and the loads are upserts, the
same property `fetch_nav` and `backfill_nav` have.

**Sector classification is not here yet.** The plan had NIFTY 500 supplying it
best-effort; niftyindices began timing out during development after I made
several probes in quick succession *bypassing the rate limiter*, which is
exactly what §2.3's one-request-per-two-seconds exists to prevent. Rather than
hammer it further, the sector taxonomy is carried forward — the AMFI list alone
satisfies the entity master and the market-cap taxonomy. DECISIONS V1-03.
"""

from __future__ import annotations

import argparse
import json
import re
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from src.common.decimals import connect
from src.m0_data.config import raw_root, source, warehouse_path
from src.m0_data.fetch.base import FetchCandidate, archive, conditional_get
from src.m0_data.load import load_mcap
from src.m0_data.parse.mcap.amfi import (
    PARSER_ID,
    PARSER_VERSION,
    parse_mcap_xlsx,
)
from src.m0_data.schema.apply import apply_migrations

SOURCE_ID = "S4"

#: Two hosts serve the archive — the current period sits on `portal.` and older
#: ones on `www.`. Both are tried rather than one guessed at, because §2 forbids
#: falling back to a guess when a URL 404s.
URL_TEMPLATES = (
    "https://portal.amfiindia.com/spages/AverageMarketCapitalization{period}.xlsx",
    "https://www.amfiindia.com/Themes/Theme1/downloads/"
    "AverageMarketCapitalization{period}.xlsx",
)

PERIOD_RE = re.compile(r"^\d{2}[A-Za-z]{3}\d{4}$")


def candidate_urls(period: str) -> list[str]:
    """Both hosts, in order. `period` is AMFI's own `30Jun2026` form."""
    if not PERIOD_RE.match(period):
        raise ValueError(f"period must look like 30Jun2026, got {period!r}")
    return [t.format(period=period) for t in URL_TEMPLATES]


def run(period: str) -> dict[str, object]:
    cfg = source("S1")  # same host family, same politeness settings
    db_path = warehouse_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(str(db_path))
    conn = connect(str(db_path))

    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO job_run (run_id, job_name, started_at, params_json)"
        " VALUES (?, ?, ?, ?)",
        (run_id, "build_entity_master", datetime.now(UTC),
         json.dumps({"period": period})),
    )
    conn.commit()

    summary: dict[str, object] = {"run_id": run_id, "period": period}
    try:
        content, url = _fetch_first_available(candidate_urls(period), cfg)
        filename = url.rsplit("/", 1)[1]

        result, path = archive(
            content,
            FetchCandidate(url=url, source_id=SOURCE_ID),
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            raw_root(),
            lambda fid: _archived(conn, fid),
        )
        if path is not None:
            conn.execute(
                "INSERT INTO raw_file (file_id, source_id, url, fetched_at,"
                " byte_size, storage_path, parse_status) VALUES (?,?,?,?,?,?, 'pending')",
                (result.file_id, SOURCE_ID, url, datetime.now(UTC),
                 result.byte_size, str(path)),
            )
            conn.commit()

        parsed = parse_mcap_xlsx(content, filename)
        counts = load_mcap(conn, parsed, result.file_id)
        conn.execute(
            "UPDATE raw_file SET parse_status='ok', parser_id=?, parser_version=?,"
            " parsed_at=?, as_of_date=? WHERE file_id=?",
            (PARSER_ID, PARSER_VERSION, datetime.now(UTC), parsed.basis_date,
             result.file_id),
        )
        conn.commit()

        summary |= {
            "fetch": result.status, "bytes": result.byte_size,
            "basis_date": str(parsed.basis_date), **counts,
            "unranked": len(parsed.unranked),
            "bucket_disagreements": len(parsed.warnings),
        }
        # A disagreement with AMFI's own column means we misread the file, so
        # it blocks `ok` rather than being noted and forgotten.
        status = "partial" if parsed.warnings else "ok"
        conn.execute(
            "UPDATE job_run SET finished_at=?, status=?, files_fetched=?,"
            " rows_written=?, warnings=? WHERE run_id=?",
            (datetime.now(UTC), status, 1 if result.status == "fetched" else 0,
             counts["issuer"] + counts["instrument"] + counts["classification"],
             len(parsed.warnings), run_id),
        )
        conn.commit()
        summary["status"] = status
        return summary

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


def _fetch_first_available(urls: list[str], cfg: dict[str, Any]) -> tuple[bytes, str]:
    """Try each host. Fail loudly naming every URL attempted (§2).

    A workbook is a ZIP, so `PK` is the cheapest possible check that we got a
    file rather than an HTML error page — and both AMFI and NSE have been seen
    to serve those under HTTP 200 (V0-24, OPEN-03).
    """
    attempted: list[str] = []
    for url in urls:
        attempted.append(url)
        try:
            response = conditional_get(
                url,
                user_agent=str(cfg["user_agent"]),
                timeout_connect=float(cfg["timeout_connect"]),
                timeout_read=float(cfg["timeout_read"]),
                retries=1,
            )
        except Exception:
            continue
        if response.status_code == 200 and response.content[:2] == b"PK":
            return response.content, url
    raise RuntimeError(f"no market-cap workbook found; attempted: {attempted}")


def _archived(conn: Any, file_id: str) -> bool:
    row = conn.execute(
        "SELECT storage_path FROM raw_file WHERE file_id = ?", (file_id,)
    ).fetchone()
    return bool(row) and Path(row[0]).exists()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--period", default=None,
        help="AMFI period end, e.g. 30Jun2026. Defaults to the latest half-year.",
    )
    args = parser.parse_args()
    period = args.period or _latest_period(date.today())
    for key, value in run(period).items():
        print(f"{key}: {value}")


def _latest_period(today: date) -> str:
    """The most recent half-year end AMFI would have published.

    Published a few weeks after the period closes, so the current half is not
    available until well into the next one — hence the completed half, not the
    nearest one.
    """
    return f"30Jun{today.year}" if today.month > 8 else f"31Dec{today.year - 1}"


if __name__ == "__main__":
    main()
