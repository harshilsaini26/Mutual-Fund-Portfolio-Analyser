"""Fetch, archive, parse and load AMFI's NAV file. MODULE_0.md §12.3.

    python -m jobs.fetch_nav [--dry-run]

One job, one `job_run` row, whatever happens. §12.3's contract is that a job
records its own outcome — a job that fails without leaving a row is
indistinguishable from one that never ran, and the next morning nobody can tell
which.

Idempotent end to end. The archive is content-addressed, so re-running against
an unchanged file writes no new bytes; the loads are upserts on natural keys, so
re-parsing writes no new rows. This is the same property V0-15 established for
CAS re-import, for the same reason: the daily file overlaps everything already
loaded.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import uuid
from datetime import UTC, date, datetime
from pathlib import Path

from src.common.decimals import connect
from src.m0_data.config import raw_root, source, warehouse_path
from src.m0_data.derive.nav_adj import build_all_nav_adj
from src.m0_data.fetch.base import (
    DomainRateLimiter,
    FetchCandidate,
    RobotsCache,
    archive,
    conditional_get,
)
from src.m0_data.load import load_parse_result
from src.m0_data.parse.nav.amfi import PARSER_ID, PARSER_VERSION, parse_navall
from src.m0_data.schema.apply import apply_migrations

SOURCE_ID = "S1"


def git_sha() -> str | None:
    """Which code produced these rows. §4.2's provenance, for the job itself."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True, text=True, timeout=10, check=True,
        )
        return out.stdout.strip() or None
    except Exception:
        return None


def run(dry_run: bool = False) -> dict[str, object]:
    cfg = source(SOURCE_ID)
    db_path = warehouse_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(str(db_path))

    conn = connect(str(db_path))
    conn.row_factory = None
    run_id = str(uuid.uuid4())
    started = datetime.now(UTC)
    conn.execute(
        "INSERT INTO job_run (run_id, job_name, started_at, git_sha, params_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (run_id, "fetch_nav", started, git_sha(), json.dumps({"dry_run": dry_run})),
    )
    conn.commit()

    summary: dict[str, object] = {"run_id": run_id}
    try:
        prior = conn.execute(
            "SELECT http_etag, http_last_mod FROM raw_file WHERE source_id = ? "
            "ORDER BY fetched_at DESC LIMIT 1",
            (SOURCE_ID,),
        ).fetchone()

        response = conditional_get(
            cfg["url"],
            user_agent=cfg["user_agent"],
            etag=prior[0] if prior else None,
            last_modified=prior[1] if prior else None,
            timeout_connect=cfg["timeout_connect"],
            timeout_read=cfg["timeout_read"],
            retries=cfg["retries"],
            backoff_base=cfg["backoff_base_sec"],
            backoff_cap=cfg["backoff_cap_sec"],
            limiter=DomainRateLimiter(cfg["rate_limit_per_sec"], cfg["burst"]),
            robots=RobotsCache() if cfg.get("respect_robots") else None,
        )

        if response.status_code == 304:
            summary |= {"status": "skipped", "reason": "304 not modified"}
            _finish(conn, run_id, "skipped", summary)
            return summary
        response.raise_for_status()

        candidate = FetchCandidate(url=cfg["url"], source_id=SOURCE_ID)
        result, path = archive(
            response.content,
            candidate,
            response.headers.get("content-type"),
            raw_root(),
            lambda fid: _archived(conn, fid),
        )
        summary |= {"fetch": result.status, "file_id": result.file_id,
                    "bytes": result.byte_size}

        if path is not None:
            # §5.2: the file is on disk BEFORE the manifest row exists. A row
            # pointing at a missing file is unrecoverable; an orphan file is not.
            conn.execute(
                "INSERT INTO raw_file (file_id, source_id, url, as_of_date, fetched_at,"
                " content_type, byte_size, http_etag, http_last_mod, storage_path,"
                " parse_status) VALUES (?,?,?,?,?,?,?,?,?,?, 'pending')",
                (result.file_id, SOURCE_ID, cfg["url"], date.today(),
                 datetime.now(UTC), response.headers.get("content-type"),
                 result.byte_size, response.headers.get("etag"),
                 response.headers.get("last-modified"), str(path)),
            )
            conn.commit()

        if dry_run:
            summary |= {"status": "skipped", "reason": "dry run"}
            _finish(conn, run_id, "skipped", summary)
            return summary

        text = _archived_text(conn, result.file_id)
        parsed = parse_navall(text.splitlines())
        counts = load_parse_result(conn, parsed, result.file_id, date.today())
        counts["nav_adj"] = build_all_nav_adj(conn)

        conn.execute(
            "UPDATE raw_file SET parse_status='ok', parser_id=?, parser_version=?,"
            " parsed_at=? WHERE file_id=?",
            (PARSER_ID, PARSER_VERSION, datetime.now(UTC), result.file_id),
        )
        summary |= counts
        status = "partial" if counts["warnings"] else "ok"
        _finish(conn, run_id, status, summary, counts)
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


def _archived(conn: object, file_id: str) -> bool:
    row = conn.execute(  # type: ignore[attr-defined]
        "SELECT storage_path FROM raw_file WHERE file_id = ?", (file_id,)
    ).fetchone()
    return bool(row) and Path(row[0]).exists()


def _archived_text(conn: object, file_id: str | None) -> str:
    row = conn.execute(  # type: ignore[attr-defined]
        "SELECT storage_path FROM raw_file WHERE file_id = ?", (file_id,)
    ).fetchone()
    if not row:
        raise FileNotFoundError(f"no archived file for {file_id}")
    return Path(row[0]).read_text(encoding="utf-8", errors="strict")


def _finish(
    conn: object, run_id: str, status: str, summary: dict[str, object],
    counts: dict[str, int] | None = None,
) -> None:
    rows = sum(v for k, v in (counts or {}).items()
               if k not in {"warnings", "collisions"})
    conn.execute(  # type: ignore[attr-defined]
        "UPDATE job_run SET finished_at=?, status=?, files_fetched=?, files_parsed=?,"
        " rows_written=?, warnings=? WHERE run_id=?",
        (datetime.now(UTC), status, 1 if summary.get("fetch") == "fetched" else 0,
         1 if counts else 0, rows, (counts or {}).get("warnings", 0), run_id),
    )
    conn.commit()  # type: ignore[attr-defined]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="fetch and archive, but do not parse or load")
    args = parser.parse_args()
    for key, value in run(dry_run=args.dry_run).items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
