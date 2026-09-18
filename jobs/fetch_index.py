"""Index catalogue, TRI levels, and the scheme->index mapping. S12.

Three things, because they are three different cadences:

    python -m jobs.fetch_index --catalogue     # 259 indices, rarely changes
    python -m jobs.fetch_index --resolve       # fills scheme.benchmark_id
    python -m jobs.fetch_index --backfill "Nifty 50" --from 2011 --to 2026
    python -m jobs.fetch_index --held --from 2011   # every index a fund needs

`--catalogue` and `--resolve` are cheap and offline-ish; `--backfill` is the
expensive one. NSE caps a request at one calendar year, so fifteen years is
fifteen requests per index, at §2.3's one per two seconds. `--held` is the form
worth running: it backfills only the indices some scheme is actually
benchmarked to, which is the difference between a handful of series and all
259.

Read-only against the network in the sense that matters -- every response is
archived by content hash before anything parses it (§5.2), so a re-run of a
range already fetched writes no new file and loads the same levels.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

from src.common.decimals import connect
from src.m0_data.config import raw_root, source, warehouse_path
from src.m0_data.fetch.base import (
    DomainRateLimiter,
    FetchCandidate,
    FetchError,
    RobotsCache,
    archive,
    conditional_get,
)
from src.m0_data.fetch.nifty_tri import TRI_URL, TriChunk, year_chunks
from src.m0_data.load import (
    MixedIndexResponse,
    has_table,
    load_index_catalogue,
    load_index_levels,
)
from src.m0_data.parse.base import ParseFailed
from src.m0_data.parse.index.nifty import PARSER_ID, PARSER_VERSION, parse_tri
from src.m0_data.resolve.benchmark import BenchmarkMatcher, resolve_scheme_benchmarks

SOURCE_ID = "S12"

#: NSE's own catalogue of every index it publishes, as the historical-data
#: page loads it. Static, on their CDN, and served with a byte-order mark.
CATALOGUE_URL = "https://liveindexsa.niftyindices.com/assets/json/IndexMapping.json"

#: The first 91-day-auction-era year NSE's TRI endpoint answers for. Earlier
#: requests return an empty list rather than an error, which is why the job
#: reports levels loaded per chunk instead of assuming a range worked.
DEFAULT_FROM = 2011


@dataclass(frozen=True)
class _Polite:
    """One rate limiter and one robots cache for a whole run.

    `DomainRateLimiter` keeps its bucket in instance state, so building one per
    request enforces nothing -- V1-55, measured. A fifteen-year backfill is the
    run where that matters most.
    """

    limiter: DomainRateLimiter
    robots: RobotsCache | None
    client: Any = None


def _polite(cfg: dict[str, Any], client: Any = None) -> _Polite:
    return _Polite(
        DomainRateLimiter(float(cfg["rate_limit_per_sec"]), int(cfg["burst"])),
        RobotsCache() if cfg.get("respect_robots") else None,
        client,
    )


def _archived(conn: Any, file_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM raw_file WHERE file_id = ?", (file_id,)
    ).fetchone()
    return row is not None


def _record(conn: Any, result: Any, path: Any, url: str) -> None:
    """§5.2: the file is on disk before this row exists."""
    if path is None:
        return
    conn.execute(
        "INSERT INTO raw_file (file_id, source_id, url, fetched_at,"
        " byte_size, storage_path, parse_status) VALUES (?,?,?,?,?,?, 'pending')",
        (result.file_id, SOURCE_ID, url, datetime.now(UTC), result.byte_size, str(path)),
    )
    conn.commit()


def _post(chunk: TriChunk, cfg: dict[str, Any], polite: _Polite) -> bytes:
    response = conditional_get(
        TRI_URL,
        user_agent=str(cfg["user_agent"]),
        method="POST",
        json_body=chunk.body,
        extra_headers=chunk.headers,
        timeout_connect=float(cfg["timeout_connect"]),
        timeout_read=float(cfg["timeout_read"]),
        retries=int(cfg["retries"]),
        backoff_base=float(cfg["backoff_base_sec"]),
        backoff_cap=float(cfg["backoff_cap_sec"]),
        limiter=polite.limiter,
        robots=polite.robots,
        from_email=str(cfg.get("from_email")) or None,
        client=polite.client,
    )
    return bytes(response.content)


def fetch_catalogue(conn: Any, cfg: dict[str, Any], polite: _Polite) -> int:
    """Register the 259 indices NSE publishes. No levels, just identity."""
    response = conditional_get(
        CATALOGUE_URL,
        user_agent=str(cfg["user_agent"]),
        extra_headers={"Accept": "application/json"},
        timeout_connect=float(cfg["timeout_connect"]),
        timeout_read=float(cfg["timeout_read"]),
        limiter=polite.limiter,
        robots=polite.robots,
        from_email=str(cfg.get("from_email")) or None,
        client=polite.client,
    )
    content = bytes(response.content)
    result, path = archive(
        content,
        FetchCandidate(url=CATALOGUE_URL, source_id=SOURCE_ID),
        "application/json",
        raw_root(),
        lambda fid: _archived(conn, fid),
    )
    _record(conn, result, path, CATALOGUE_URL)

    entries = json.loads(content.decode("utf-8-sig"))
    pairs = [
        (str(e["Index_long_name"]).strip(), str(e.get("Trading_Index_Name", "")).strip())
        for e in entries
    ]
    loaded = load_index_catalogue(conn, pairs)
    conn.commit()
    return loaded


def backfill(
    conn: Any,
    index_name: str,
    start: date,
    end: date,
    cfg: dict[str, Any],
    polite: _Polite,
) -> dict[str, int]:
    """Every level for one index over a range. One request per calendar year."""
    levels = 0
    empty = 0
    for chunk in year_chunks(index_name, start, end):
        content = _post(chunk, cfg, polite)
        result, path = archive(
            content,
            FetchCandidate(url=TRI_URL, source_id=SOURCE_ID, meta={"chunk": chunk.label}),
            "application/json",
            raw_root(),
            lambda fid: _archived(conn, fid),
        )
        _record(conn, result, path, TRI_URL)

        staged = parse_tri(content, file_id=str(result.file_id))
        if not staged:
            # A year before the index existed. Not an error, and NOT silent:
            # an empty response is exactly what a malformed request also
            # returns, so the count is reported rather than shrugged off.
            empty += 1
            continue
        _, n = load_index_levels(conn, staged, str(result.file_id))
        conn.execute(
            "UPDATE raw_file SET parse_status='ok', parser_id=?, parser_version=?,"
            " parsed_at=? WHERE file_id=?",
            (PARSER_ID, PARSER_VERSION, datetime.now(UTC), result.file_id),
        )
        conn.commit()
        levels += n
    return {"levels": levels, "empty_chunks": empty}


def held_indices(conn: Any) -> list[str]:
    """The indices some scheme is actually benchmarked to, newest names first.

    The whole catalogue is 259 series and fifteen years each; what a reader
    needs is the ones a fund in this warehouse points at.
    """
    rows = conn.execute(
        "SELECT DISTINCT b.index_name FROM scheme s"
        " JOIN benchmark_index b ON b.index_id = s.benchmark_id"
        " WHERE s.benchmark_id IS NOT NULL"
        " ORDER BY b.index_name"
    ).fetchall()
    return [str(r[0]) for r in rows]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", action="store_true",
                        help="register the indices NSE publishes")
    parser.add_argument("--resolve", action="store_true",
                        help="fill scheme.benchmark_id from each scheme's name")
    parser.add_argument("--backfill", metavar="INDEX",
                        help="fetch TRI levels for one index, by its NSE name")
    parser.add_argument("--held", action="store_true",
                        help="backfill every index a scheme is benchmarked to")
    parser.add_argument("--from", dest="from_year", type=int, default=DEFAULT_FROM)
    parser.add_argument("--to", dest="to_year", type=int, default=date.today().year)
    args = parser.parse_args(argv)

    if not any((args.catalogue, args.resolve, args.backfill, args.held)):
        parser.error("nothing to do: pass --catalogue, --resolve, --backfill or --held")

    cfg = source(SOURCE_ID)
    polite = _polite(cfg)
    conn = connect(str(warehouse_path()))
    try:
        if not has_table(conn, "benchmark_index"):
            print("benchmark_index is missing -- run the migrations first"
                  " (python -m src.m0_data.schema.apply)")
            return 1

        if args.catalogue:
            print(f"catalogue: {fetch_catalogue(conn, cfg, polite)} indices registered")

        if args.resolve:
            matcher = BenchmarkMatcher.from_warehouse(conn)
            counts = resolve_scheme_benchmarks(conn, matcher)
            conn.commit()
            print(f"resolve: {counts['filled']} of {counts['considered']} schemes"
                  f" matched against {len(matcher)} index spellings")

        start = date(args.from_year, 1, 1)
        end = min(date(args.to_year, 12, 31), date.today())
        wanted = held_indices(conn) if args.held else []
        if args.backfill:
            wanted = [args.backfill, *wanted]
        # One index's failure costs that index, not the run. A 123-series
        # backfill is ~70 minutes of polite requests, and letting a single
        # unparseable response discard everything fetched after it is the same
        # mistake `_expand_zips` made with a corrupt archive. The parser stays
        # strict; isolation belongs in the batch loop.
        failed: list[tuple[str, str]] = []
        no_series: list[str] = []
        for name in wanted:
            try:
                got = backfill(conn, name, start, end, cfg, polite)
            except (ParseFailed, MixedIndexResponse, FetchError) as exc:
                failed.append((name, f"{type(exc).__name__}: {exc}"))
                print(f"  {name:38} FAILED  {type(exc).__name__}")
                continue
            if got["levels"] == 0:
                no_series.append(name)
            print(f"  {name:38} {got['levels']:6} levels"
                  f"  ({got['empty_chunks']} empty years)")

        if wanted:
            print(f"backfill: {len(wanted)} index series, {start:%Y}..{end:%Y}")
        if no_series:
            # Not a failure. NSE publishes no total-return series for its G-Sec
            # indices -- a bond index's level already carries accrued interest.
            # Reported because "0 levels" and "request was wrong" look identical
            # from here, and a reader should know which indices have nothing.
            print(f"  {len(no_series)} with no TRI series at all: "
                  f"{', '.join(no_series[:6])}"
                  f"{' ...' if len(no_series) > 6 else ''}")
        if failed:
            print(f"  {len(failed)} FAILED:")
            for name, why in failed:
                print(f"    {name}: {why}")
            return 1
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
