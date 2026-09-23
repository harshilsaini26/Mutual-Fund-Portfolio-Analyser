"""Index catalogue, TRI levels, and the scheme->index mapping. S12.

Three things, because they are three different cadences:

    python -m jobs.fetch_index --catalogue     # 259 indices, rarely changes
    python -m jobs.fetch_index --resolve       # benchmark_id from fund names
    python -m jobs.fetch_index --declared      # benchmark_id from disclosures
    python -m jobs.fetch_index --backfill "Nifty 50" --from 2011 --to 2026
    python -m jobs.fetch_index --held --from 2011   # every index a fund needs

`--resolve` reaches index funds and ETFs, whose names carry their index.
`--declared` reaches active funds, whose names do not, by reading the
benchmark each archived disclosure states. It wins where the two disagree:
a stated benchmark is a fact, a name is an inference.

`--catalogue` and `--resolve` are cheap and offline-ish; `--backfill` is the
expensive one. NSE caps a request at one calendar year, so fifteen years is
fifteen requests per index, at §2.3's one per two seconds. `--held` is the form
worth running: it backfills only the indices some scheme is actually
benchmarked to, which is the difference between a handful of series and all
259.

Every response is archived by content hash before anything parses it (§5.2),
and levels upsert. Content addressing alone cannot stop a re-run duplicating
the archive here: NSE stamps each response with a per-request `RequestNumber`,
so the same year hashes differently every time, and a full `--held` re-run
once stored 1,369 duplicate files, 47 MB. So a run skips the years already
loaded (`still_needed`) and fetches only the latest loaded year onward, plus
anything before an index's first level. `--full` fetches everything, to pick
up a level NSE has restated.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
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
    registered_index,
)
from src.m0_data.normalise.index_id import index_key
from src.m0_data.parse.base import ParseFailed
from src.m0_data.parse.holdings.benchmark import declared_benchmarks
from src.m0_data.parse.index.nifty import PARSER_ID, PARSER_VERSION, parse_tri
from src.m0_data.resolve.benchmark import (
    Benchmark,
    BenchmarkMatcher,
    apply_declared,
    resolve_declared,
    resolve_scheme_benchmarks,
)

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
    # Marked parsed, as `backfill` marks every chunk. Without this the
    # catalogue stayed `pending` after a load that succeeded -- the same defect
    # the review found in empty chunks, in the one path that fix missed.
    conn.execute(
        "UPDATE raw_file SET parse_status='ok', parser_id=?, parser_version=?,"
        " parsed_at=? WHERE file_id=?",
        ("index.nse_catalogue", "1", datetime.now(UTC), result.file_id),
    )
    conn.commit()
    return loaded


def still_needed(year: int, first_seen: date | None, last_seen: date | None) -> bool:
    """Whether a calendar year of an index's series still has to be fetched.

    A year before the one holding the latest loaded level is complete: it was
    fetched in a run that already had data from a later year, so it was fetched
    after it ended. The latest loaded year may be partial -- the run that loaded
    it may have been mid-year -- so it is fetched again, with every year after.
    Years before the first loaded level are fetched too, because nothing here
    records whether an earlier run asked for them; for an index that launched
    after 2011 they come back empty, which costs a request and no archive.

    Re-fetching a complete year was defect 9. NSE stamps every response with a
    per-request `RequestNumber`, so the same year hashes differently each time
    and the content-addressed archive stored a fresh copy on every run: 1,369
    files, 47 MB, for data already on disk.
    """
    if first_seen is None or last_seen is None:
        return True
    return year < first_seen.year or year >= last_seen.year


def loaded_span(conn: Any, index_name: str) -> tuple[date | None, date | None]:
    """`(first_seen, last_seen)` of the registered index this name keys to."""
    index_id = registered_index(conn, index_key(index_name), True)
    if index_id is None:
        return None, None
    row = conn.execute(
        "SELECT first_seen, last_seen FROM benchmark_index WHERE index_id = ?",
        (index_id,),
    ).fetchone()
    return (row[0], row[1]) if row else (None, None)


def backfill(
    conn: Any,
    index_name: str,
    start: date,
    end: date,
    cfg: dict[str, Any],
    polite: _Polite,
    *,
    full: bool = False,
) -> dict[str, int]:
    """The levels one index is still missing over a range. One request a year.

    `full` fetches every year regardless -- the way to pick up a level NSE has
    restated in a year already loaded, which the incremental rule never
    revisits.
    """
    levels = 0
    empty = 0
    skipped = 0
    first_seen, last_seen = (None, None) if full else loaded_span(conn, index_name)
    for chunk in year_chunks(index_name, start, end):
        if not still_needed(chunk.start.year, first_seen, last_seen):
            skipped += 1
            continue
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
        n = 0
        if staged:
            _, n = load_index_levels(conn, staged, str(result.file_id))
        else:
            # A year before the index existed. Not an error, and NOT silent:
            # an empty response is exactly what a malformed request also
            # returns, so the count is reported rather than shrugged off.
            empty += 1
        # Marked parsed either way. An empty response parsed cleanly and holds
        # nothing; left `pending`, it is a file any retry job re-finds on every
        # run and can never clear, since re-parsing yields the same nothing.
        conn.execute(
            "UPDATE raw_file SET parse_status='ok', parser_id=?, parser_version=?,"
            " parsed_at=? WHERE file_id=?",
            (PARSER_ID, PARSER_VERSION, datetime.now(UTC), result.file_id),
        )
        conn.commit()
        levels += n
    return {"levels": levels, "empty_chunks": empty, "skipped": skipped}


def held_indices(conn: Any) -> list[str]:
    """The indices some scheme is actually benchmarked to, in name order.

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


def plan_declared(
    conn: Any, matcher: BenchmarkMatcher
) -> tuple[dict[tuple[str, str], Benchmark], Counter[str], list[str]]:
    """Every archived disclosure's stated benchmark, resolved, newest per family.

    Returns `(plan, tally, conflicts)`. Writes nothing -- `apply_declared` does
    -- so a run can be measured before it changes a row.

    Sheets are identified to a scheme family by `discover_sheets`, the same
    path `ingest_inbox` loads them through; this adds no second way of deciding
    which fund a sheet describes. A family appears in every archived
    disclosure of it, and the newest statement is the one that stands: SEBI
    moved benchmarks in 2021, and an old disclosure's is not the current one.
    """
    # Imported here: `jobs.load_holdings` pulls openpyxl and the whole parser
    # registry in at import (V1-48), and no other flag of this job needs them.
    import openpyxl

    from jobs.ingest_inbox import _amc_for
    from jobs.load_holdings import discover_sheets

    files = conn.execute(
        "SELECT storage_path FROM raw_file"
        " WHERE source_id LIKE 'S5:%' AND storage_path LIKE '%.xlsx'"
    ).fetchall()
    tally: Counter[str] = Counter()
    found: dict[tuple[str, str], list[tuple[date, Benchmark]]] = defaultdict(list)
    for (storage_path,) in files:
        path = Path(storage_path)
        if not path.exists():
            tally["archived file missing"] += 1
            continue
        # Detected from the file, never read off the archive tag. The tag is
        # `S5:nippon` where Nippon's schemes are filed under `nippon_india`, so
        # trusting it looked every Nippon sheet up under a fund house with no
        # schemes: 107 sheets stating a benchmark, 0 identified. `_amc_for` is
        # what `ingest_inbox` itself uses, so this cannot disagree with how the
        # disclosure was loaded.
        amc, _, _ = _amc_for(conn, path)
        if not amc:
            tally["fund house not identified"] += 1
            continue
        entries, refusals = discover_sheets(conn, path, amc)
        tally["sheet not identified"] += len(refusals)
        workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
        try:
            for entry in entries:
                sheet = workbook[entry["sheet"]]
                rows = [list(r) for r in sheet.iter_rows(values_only=True)]
                bm, reason = resolve_declared(declared_benchmarks(rows), matcher)
                tally[reason] += 1
                if bm is not None:
                    found[(entry["amc_id"], entry["family"])].append((entry["as_of"], bm))
        finally:
            workbook.close()

    plan, conflicts = newest_per_family(found)
    return plan, tally, conflicts


def newest_per_family(
    found: dict[tuple[str, str], list[tuple[date, Benchmark]]],
) -> tuple[dict[tuple[str, str], Benchmark], list[str]]:
    """The benchmark each family's newest disclosure states.

    Older disclosures lose: SEBI moved many schemes' benchmarks in 2021, and a
    2019 sheet's is not the one in force. Two sheets of the newest date that
    disagree are a conflict and the family is left out of the plan -- picking
    either is a guess, so it keeps whatever it already had.
    """
    plan: dict[tuple[str, str], Benchmark] = {}
    conflicts: list[str] = []
    for family, stated in found.items():
        newest = max(when for when, _ in stated)
        current = {b.index_id: b for when, b in stated if when == newest}
        if len(current) > 1:
            conflicts.append(f"{family[1]}: {sorted(current)}")
            continue
        plan[family] = next(iter(current.values()))
    return plan, conflicts


def one_per_index(names: list[str]) -> list[str]:
    """Each index once, first spelling kept, order preserved.

    Decided by key, not by string. `--backfill X --held` listed X twice
    whenever a scheme is benchmarked to X -- 16 wasted requests and a summary
    counting 124 series for 123 -- and a casing difference is the same index.
    """
    seen: set[str] = set()
    unique: list[str] = []
    for name in names:
        if (key := index_key(name)) not in seen:
            seen.add(key)
            unique.append(name)
    return unique


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalogue", action="store_true",
                        help="register the indices NSE publishes")
    parser.add_argument("--resolve", action="store_true",
                        help="fill scheme.benchmark_id from each scheme's name")
    parser.add_argument("--declared", action="store_true",
                        help="fill scheme.benchmark_id from what disclosures state")
    parser.add_argument("--backfill", metavar="INDEX",
                        help="fetch TRI levels for one index, by its NSE name")
    parser.add_argument("--held", action="store_true",
                        help="backfill every index a scheme is benchmarked to")
    parser.add_argument("--full", action="store_true",
                        help="re-fetch years already loaded, to pick up a restatement")
    parser.add_argument("--from", dest="from_year", type=int, default=DEFAULT_FROM)
    parser.add_argument("--to", dest="to_year", type=int, default=date.today().year)
    args = parser.parse_args(argv)

    if not any((args.catalogue, args.resolve, args.declared, args.backfill, args.held)):
        parser.error(
            "nothing to do: pass --catalogue, --resolve, --declared, --backfill or --held"
        )

    # Checked here, not left to `year_chunks`. Its ValueError is outside the
    # per-index isolation below, so a mistyped year used to kill the run with a
    # traceback before the first request. `end` is clamped to today, so a
    # `--from` in the future is caught by the same comparison.
    start = date(args.from_year, 1, 1)
    end = min(date(args.to_year, 12, 31), date.today())
    if (args.backfill or args.held) and start > end:
        parser.error(f"--from {args.from_year} is after --to {args.to_year} or today")

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

        # Before the backfill below, so an index only a disclosure names is in
        # `held_indices` by the time `--held` asks for it.
        if args.declared:
            plan, tally, conflicts = plan_declared(
                conn, BenchmarkMatcher.from_warehouse(conn)
            )
            written = apply_declared(conn, plan)
            conn.commit()
            print(f"declared: {len(plan)} families resolved;"
                  f" {written['schemes']} share classes"
                  f" ({written['was_null']} new, {written['changed']} replacing an"
                  f" inferred one, {written['unchanged']} already agreed)")
            for reason, n in tally.most_common():
                print(f"    {n:5}  {reason}")
            for conflict in conflicts:
                print(f"    conflict  {conflict}")

        names = [args.backfill] if args.backfill else []
        wanted = one_per_index(names + (held_indices(conn) if args.held else []))
        # One index's failure costs that index, not the run. A 123-series
        # backfill is ~70 minutes of polite requests, and letting a single
        # unparseable response discard everything fetched after it is the same
        # mistake `_expand_zips` made with a corrupt archive. The parser stays
        # strict; isolation belongs in the batch loop.
        failed: list[tuple[str, str]] = []
        no_series: list[str] = []
        for name in wanted:
            try:
                got = backfill(conn, name, start, end, cfg, polite, full=args.full)
            except (ParseFailed, MixedIndexResponse, FetchError) as exc:
                failed.append((name, f"{type(exc).__name__}: {exc}"))
                print(f"  {name:38} FAILED  {type(exc).__name__}")
                continue
            # No series only if nothing was skipped either: a loaded index
            # with nothing new also loads 0, and is not a debt index.
            if got["levels"] == 0 and got["skipped"] == 0:
                no_series.append(name)
            print(f"  {name:38} {got['levels']:6} levels"
                  f"  ({got['empty_chunks']} empty, {got['skipped']} already loaded)")

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
