"""Load a scheme's portfolio from its Groww page. DECISIONS V1-43.

    python -m jobs.fetch_groww                      # every slug in the map
    python -m jobs.fetch_groww --scheme INF179K01UT0
    python -m jobs.fetch_groww --slug <slug> --dry-run
    python -m jobs.fetch_groww --crawl 100 --map data/groww.csv   # the daily build

**The crawl** (DECISIONS V1-79) is how the public build covers every fund Groww
lists without a hand-kept map: its sitemap names ~1,600 Direct Growth pages, and
each page states its own ISIN. A CSV map (`--map`) remembers what each page said
and when it was read. Each run reads at most `--crawl` pages -- pages due a
monthly refresh first, then pages never read, then pages that named no fund we
list, again after three months -- so the first pass takes about sixteen days at
100 a day, and after it the refresh needs about fifty a day.

**The coverage tier.** `load_holdings` reads an AMC's own workbook and covers
the five houses with a parser; this reads an aggregator's page and covers any
fund Groww lists. NOT a replacement where a workbook exists — the page carries
no ISIN column, costing 8.96% of rows unresolved against 0.00%.

**The slug is the whole risk, so it is checked twice.** A slug cannot be built
from a scheme name: Groww keeps the pre-rename name, so HDFC Flexi Cap lives at
`hdfc-equity-fund-direct-growth`. A wrong slug that 404s is harmless; one that
RESOLVES would load a real portfolio under the wrong `scheme_id` with nothing
downstream able to tell. So the page's own `isin` is compared against the one
the map promised, and a mismatch REFUSES.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import yaml
from src.common.decimals import connect
from src.m0_data.config import REPO_ROOT, raw_root, source, warehouse_path
from src.m0_data.fetch.base import (
    DomainRateLimiter,
    FetchCandidate,
    RobotsCache,
    archive,
    conditional_get,
)
from src.m0_data.load import aum_for as _aum_for
from src.m0_data.load import build_holding_rows, load_holdings
from src.m0_data.parse.base import ParseFailed, RawFile
from src.m0_data.parse.holdings.groww import GrowwHoldingsParser
from src.m0_data.resolve.cascade import (
    load_isin_prefix_index,
    load_issuer_index,
)
from src.m0_data.universe import live_funds
from src.m0_data.validate.checks import (
    HoldingRow,
    as_json,
    promote_or_quarantine,
    validate_disclosure,
)

SOURCE_ID = "S7"
SLUGS_YAML = REPO_ROOT / "config" / "groww_slugs.yaml"
SITEMAP = "https://groww.in/mf-sitemap.xml"
_SLUG = re.compile(r"https://groww\.in/mutual-funds/([a-z0-9-]+-direct-growth)\b")
#: A page read this long ago is read again: disclosures are monthly.
REFRESH = timedelta(days=30)
#: A page that named no fund we list, or failed, is tried again after this.
RECHECK = timedelta(days=90)

#: What `source_tier` records for anything loaded here. Migration 011.
TIER = "aggregator"


class SlugMismatch(ParseFailed):
    """The page is a real portfolio for a scheme other than the one asked for.

    Its own class because it is the one failure that would otherwise be
    invisible: a 404 announces itself, and a portfolio filed under the wrong
    scheme_id does not.
    """


def load_slugs(path: Path = SLUGS_YAML) -> dict[str, dict[str, Any]]:
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    slugs = loaded.get("slugs") or {}
    if not isinstance(slugs, dict):
        raise ValueError(f"{path}: `slugs` is not a mapping")
    return slugs


@dataclass(frozen=True)
class Polite:
    """One limiter and one robots cache for a whole crawl (V1-55's lesson: one
    built per request enforces nothing)."""

    limiter: DomainRateLimiter
    robots: RobotsCache | None


def polite(cfg: dict[str, Any]) -> Polite:
    return Polite(
        DomainRateLimiter(float(cfg["rate_limit_per_sec"]), int(cfg["burst"])),
        RobotsCache() if cfg.get("respect_robots") else None,
    )


def fetch_page(
    slug: str, cfg: dict[str, Any], manners: Polite | None = None
) -> tuple[bytes, str]:
    """One polite GET of `https://groww.in/mutual-funds/<slug>`.

    `respect_robots` is on for this source and the check runs here rather than
    being assumed: robots.txt allows `/mutual-funds/<slug>` today, and a source
    whose permission is read once at implementation time is a source whose
    permission goes stale silently.
    """
    url = str(cfg["url"]).format(slug=slug)
    return _get(url, cfg, manners or polite(cfg)), url


def _get(url: str, cfg: dict[str, Any], manners: Polite) -> bytes:
    response = conditional_get(
        url,
        user_agent=str(cfg["user_agent"]),
        timeout_connect=float(cfg["timeout_connect"]),
        timeout_read=float(cfg["timeout_read"]),
        retries=int(cfg["retries"]),
        from_email=str(cfg.get("from_email") or "") or None,
        limiter=manners.limiter,
        robots=manners.robots,
    )
    response.raise_for_status()
    return bytes(response.content)


def page_isin(content: bytes) -> str | None:
    """The ISIN the page states for ITSELF, which is the only trustworthy one."""
    from src.m0_data.parse.holdings.groww import _payload

    data = _payload(RawFile("probe", SOURCE_ID, "probe.html", content))
    isin = data.get("isin")
    return str(isin).strip().upper() if isin else None


def _archived(conn: Any, file_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM raw_file WHERE file_id = ?", (file_id,)).fetchone()
    return row is not None


def _guard_scheme_is_known(conn: Any, slug: str, scheme_id: str) -> None:
    """The scheme has to exist before a disclosure can be filed against it.

    `holding_disclosure.scheme_id REFERENCES scheme(scheme_id)` is declared and
    INERT -- SQLite enforces foreign keys only under `PRAGMA foreign_keys = ON`,
    which this project does not set. So with `--slug`, which bypasses the map and
    takes whatever ISIN the page states, an unknown scheme wrote a full
    disclosure and every holding row under an id no scheme has. It then vanished
    from anything that joins `scheme` -- including `jobs/status.py`'s own report
    -- so the fund read as loaded and missing at the same time depending which
    query you asked.
    """
    known = conn.execute(
        "SELECT 1 FROM scheme WHERE scheme_id = ?", (scheme_id,)
    ).fetchone()
    if not known:
        raise SlugMismatch(
            f"{slug}: {scheme_id} is not in the scheme master. Load the AMFI"
            " universe first (`python -m jobs.fetch_nav`); refusing to file a"
            " portfolio against a scheme nothing else can see."
        )


def _amc_direct_cover(conn: Any, scheme_id: str) -> str | None:
    """The as-of date of an AMC file already covering this scheme, if any.

    Not correctness -- restraint. The guard that keeps the coverage tier from
    outranking an AMC file lives in `weights.latest_disclosure`, where every
    reader passes, rather than in the job that writes the data (V1-46). This
    only avoids fetching, parsing and storing a page that would then never be
    selected. `--force` puts the two side by side deliberately.
    """
    row = conn.execute(
        "SELECT as_of_date FROM holding_disclosure"
        " WHERE scheme_id = ? AND is_current = 1 AND source_tier = 'amc_direct'"
        " ORDER BY as_of_date DESC LIMIT 1",
        (scheme_id,),
    ).fetchone()
    return str(row[0]) if row else None


def _archive_page(conn: Any, content: bytes, url: str) -> Any:
    """Archive the raw bytes and file the `raw_file` row, if it is new."""
    result, path = archive(
        content,
        FetchCandidate(url=url, source_id=SOURCE_ID),
        "text/html",
        raw_root(),
        lambda fid: _archived(conn, fid),
    )
    if path is not None:
        conn.execute(
            "INSERT INTO raw_file (file_id, source_id, url, fetched_at, byte_size,"
            " storage_path, parse_status) VALUES (?,?,?,?,?,?, 'pending')",
            (
                result.file_id,
                SOURCE_ID,
                url,
                datetime.now(UTC),
                result.byte_size,
                str(path),
            ),
        )
        conn.commit()
    return result


def _validate_rows(
    conn: Any, scheme_id: str, parsed: Any, rows: list[dict[str, object]]
) -> tuple[list[Any], str, str, Decimal | None]:
    """Run the disclosure checks against an AUM witness the page cannot fake.

    §10's V2 is THE units check, and it was being skipped here: the call passed
    `aum_reported=None`, so every Groww disclosure recorded "no AUM on record to
    reconcile against". That is the wrong path to disable it on.
    `MARKET_VALUE_UNIT` is ASSERTED on this page rather than read off a header
    -- no cell states a unit -- and the parser's own `_check_unit` compares rows
    to the page's `aum`, both in the same unit, so it scales with a unit error
    and can never catch one. `scheme_aum` is the only witness here that is
    independent of the page, which is exactly what V2 wants.
    """
    witness = _aum_for(conn, scheme_id, parsed.as_of_date)
    aum = witness.amount if witness else None

    checks = validate_disclosure(
        [
            HoldingRow(
                None,
                str(r["instrument_class"]),
                Decimal(str(r["market_value"])),
                Decimal(str(r["pct_to_nav"])) if r["pct_to_nav"] is not None else None,
                str(r["issuer_id"]),
            )
            for r in rows
        ],
        parsed.as_of_date,
        date.today(),
        aum,
        witness.basis if witness else "point_in_time",
        witness.as_of if witness else None,
    )
    status = promote_or_quarantine(checks)
    unresolved = next(c for c in checks if c.code == "V3").observed or "0%"
    return checks, status, unresolved, aum


def _one(
    conn: Any,
    scheme_id: str | None,
    slug: str,
    cfg: dict[str, Any],
    index: dict[str, str],
    prefixes: dict[str, str],
    dry_run: bool,
    force: bool = False,
    manners: Polite | None = None,
) -> dict[str, object]:
    content, url = fetch_page(slug, cfg, manners)

    # Parsed ONCE. The first draft parsed here for the ISIN check, again for
    # the dry-run summary and a third time after archiving -- three passes of
    # the `__NEXT_DATA__` regex and three `json.loads` of a ~500 KB payload for
    # one page. Nothing the later passes needed came from the parse; the only
    # thing that changes after archiving is the `file_id`, which is a field on
    # `RawFile` and not an input to parsing.
    parser = GrowwHoldingsParser()
    parsed = parser.parse(RawFile("probe", SOURCE_ID, f"{slug}.html", content))
    assert parsed.as_of_date is not None
    stated = page_isin(content)

    if scheme_id and stated and stated != scheme_id.upper():
        raise SlugMismatch(
            f"{slug}: the map promised {scheme_id} and the page states {stated}."
            " Refusing to load a portfolio under a scheme_id it does not claim."
        )

    if dry_run:
        return {
            "slug": slug,
            "scheme_name": parsed.scheme_raw_name,
            "isin": stated,
            "as_of": str(parsed.as_of_date),
            "rows": len(parsed.securities),
            "stated_total": str(parsed.stated_total),
            "loaded": "no (dry run)",
            "yaml": f"  {stated}:\n    slug: {slug}\n"
            f"    scheme_name: {parsed.scheme_raw_name}",
        }

    if not stated:
        raise SlugMismatch(f"{slug}: the page states no ISIN; refusing to guess one")
    scheme_id = stated
    _guard_scheme_is_known(conn, slug, scheme_id)

    covered = _amc_direct_cover(conn, scheme_id)
    if covered and not force:
        return {
            "slug": slug,
            "scheme_id": scheme_id,
            "as_of": str(parsed.as_of_date),
            "skipped": f"covered amc_direct at {covered}",
            "hint": "--force to load anyway; the AMC's own file resolves more",
        }

    result = _archive_page(conn, content, url)
    rows, weights, unpriced = build_holding_rows(
        conn, parsed.securities, parsed.pct_scale, index, prefixes
    )
    checks, status, unresolved, aum = _validate_rows(conn, scheme_id, parsed, rows)

    counts = load_holdings(
        conn,
        scheme_id,
        parsed.as_of_date,
        rows,
        {
            "pct_sum_raw": Decimal(100) - weights.residual,
            "weight_residual": weights.residual,
            "unresolved_mv_pct": Decimal(unresolved.rstrip("%")),
            "total_mv": weights.total_market_value,
            "aum_reported": aum,
            "reported_unit": parsed.securities[0].market_value_unit
            if parsed.securities
            else None,
            "validation_status": status,
            "validation_notes": as_json(checks, unpriced=unpriced),
            "source_tier": TIER,
        },
        str(result.file_id),
    )
    conn.execute(
        "UPDATE raw_file SET parse_status='ok', parser_id=?, parser_version=?,"
        " parsed_at=?, as_of_date=? WHERE file_id=?",
        (
            parser.parser_id,
            parser.version,
            datetime.now(UTC),
            parsed.as_of_date,
            result.file_id,
        ),
    )
    conn.commit()

    return {
        "slug": slug,
        "scheme_id": scheme_id,
        "as_of": str(parsed.as_of_date),
        "fetch": result.status,
        **counts,
        "unresolved_mv_pct": unresolved,
        "validation_status": status,
        "source_tier": TIER,
    }


def run(
    scheme_ids: list[str] | None = None,
    slug: str | None = None,
    dry_run: bool = False,
    force: bool = False,
) -> list[dict[str, object]]:
    cfg = source(SOURCE_ID)
    conn = connect(str(warehouse_path()))
    index = load_issuer_index(conn)
    prefixes = load_isin_prefix_index(conn)

    if slug:
        targets: list[tuple[str | None, str]] = [(None, slug)]
    else:
        slugs = load_slugs()
        wanted = set(scheme_ids or slugs)
        targets = [
            (sid, str(entry["slug"])) for sid, entry in slugs.items() if sid in wanted
        ]
        missing = wanted - set(slugs)
        if missing:
            raise SystemExit(
                f"no slug for {', '.join(sorted(missing))} in {SLUGS_YAML.name}."
                " Find it in https://groww.in/mf-sitemap.xml and add it."
            )

    out = []
    for scheme_id, target in targets:
        out.append(_one(conn, scheme_id, target, cfg, index, prefixes, dry_run, force))
    return out


@dataclass(frozen=True)
class Seen:
    """What one page said when it was last read: its ISIN ("" if it named none
    or could not be read), its portfolio's date, and the day it was read."""

    isin: str
    as_of: str
    checked: date


def read_map(path: Path) -> dict[str, Seen]:
    if not path.is_file():
        return {}
    with path.open(encoding="utf-8", newline="") as fh:
        return {
            r["slug"]: Seen(r["isin"], r["as_of"], date.fromisoformat(r["checked"]))
            for r in csv.DictReader(fh)
        }


def write_map(path: Path, seen: dict[str, Seen]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as fh:
        out = csv.writer(fh, lineterminator="\n")
        out.writerow(["slug", "isin", "as_of", "checked"])
        for slug in sorted(seen):
            s = seen[slug]
            out.writerow([slug, s.isin, s.as_of, s.checked.isoformat()])


def plan(
    listed: list[str], seen: dict[str, Seen], live: set[str], covered: set[str],
    today: date, limit: int,
) -> list[tuple[str, str | None]]:
    """Which pages to read today, and the ISIN each promised: pages of live funds
    due a refresh, then pages never read, then pages worth trying again. A fund
    its fund house's own file covers is not read at all."""
    due: list[tuple[str, str | None]] = []
    new: list[tuple[str, str | None]] = []
    retry: list[tuple[str, str | None]] = []
    for slug in sorted(set(listed) | set(seen)):
        s = seen.get(slug)
        if s is None:
            new.append((slug, None))
        elif s.isin in covered:
            continue
        elif s.isin in live and today - s.checked >= REFRESH:
            due.append((slug, s.isin))
        elif s.isin not in live and today - s.checked >= RECHECK and slug in listed:
            retry.append((slug, None))
    return (due + new + retry)[:limit]


def crawl(limit: int, map_path: Path, today: date | None = None) -> dict[str, int]:
    """Read up to `limit` Groww pages, loading each one's portfolio (V1-79)."""
    counts = {"read": 0, "loaded": 0, "skipped": 0, "failed": 0}
    if limit <= 0:
        return counts  # not even the sitemap
    today = today or date.today()
    cfg = source(SOURCE_ID)
    manners = polite(cfg)
    seen = read_map(map_path)
    conn = connect(str(warehouse_path()))
    try:
        listed = _SLUG.findall(_get(SITEMAP, cfg, manners).decode("utf-8", "replace"))
        live = {f.scheme_id for f in live_funds(conn)}
        covered = {str(r[0]) for r in conn.execute(
            "SELECT scheme_id FROM holding_disclosure"
            " WHERE is_current = 1 AND source_tier = 'amc_direct'")}
        index = load_issuer_index(conn)
        prefixes = load_isin_prefix_index(conn)
        for slug, promised in plan(listed, seen, live, covered, today, limit):
            counts["read"] += 1
            try:
                got = _one(conn, promised, slug, cfg, index, prefixes, False,
                           manners=manners)
                seen[slug] = Seen(str(got["scheme_id"]), str(got["as_of"]), today)
                counts["skipped" if "skipped" in got else "loaded"] += 1
            except Exception as exc:  # one page's failure is that page's alone
                # SlugMismatch is a page for a fund we do not list, or not the
                # one promised; anything else is a page we could not read.
                # Either way nothing of it stays, and it is tried again later.
                conn.rollback()
                print(f"  ! {slug}: {type(exc).__name__}: {exc}")
                seen[slug] = Seen("", "", today)
                counts["failed"] += 1
    finally:
        conn.close()
        write_map(map_path, seen)
    return counts


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scheme", action="append", help="scheme ISIN from the map")
    parser.add_argument("--slug", help="a Groww slug directly, bypassing the map")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print what the page says about itself and load nothing",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="load even where the AMC's own file already covers the scheme",
    )
    parser.add_argument("--crawl", type=int, metavar="N",
                        help="read up to N pages from Groww's sitemap (the build)")
    parser.add_argument("--map", type=Path, default=REPO_ROOT / "data" / "groww.csv",
                        help="the crawl's memory of what each page said")
    args = parser.parse_args(argv)

    if args.crawl is not None:
        counts = crawl(args.crawl, args.map)
        print("  " + ", ".join(f"{v:,} {k}" for k, v in counts.items()))
        return 0
    for row in run(args.scheme, args.slug, args.dry_run, args.force):
        for key, value in row.items():
            print(f"  {key:20} {value}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
