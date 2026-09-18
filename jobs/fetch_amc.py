"""Fetch an AMC's own disclosure, no browser and no download. V1-44.

    python -m jobs.fetch_amc --amc kotak --list
    python -m jobs.fetch_amc --amc kotak --period 2026-08
    python -m jobs.fetch_amc --amc icici              # the latest published

**The precision tier.** Lands the AMC's own statutory workbook in `data/inbox/`,
where `jobs.ingest_inbox` already knows what to do with it — so the file arrives
by itself instead of by hand.

The inbox rather than loading directly, because `ingest_inbox` is the path every
hand-downloaded file has taken since V1-39 and is the one that refuses a sheet it
cannot identify. Routing a fetched file down a second path would mean two
behaviours to keep in step.

`--list` answers "is this month's disclosure out yet" without downloading 25 MB
to find out, and since the listings are historical it also says how far back a
backfill could go.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path, PureWindowsPath
from typing import Any

from src.m0_data.config import WORKBOOKS, inbox_root, source
from src.m0_data.fetch.amc_direct import DISCOVERY, DiscoveredFile, for_period
from src.m0_data.fetch.base import (
    DomainRateLimiter,
    FetchError,
    RobotsCache,
    conditional_get,
)

#: S5 is the AMC disclosure source; these adapters only change how its URL is
#: found, so they inherit its politeness settings rather than declaring new ones.
SOURCE_ID = "S5"


def listing(amc_id: str, cfg: dict[str, Any], client: Any = None) -> list[DiscoveredFile]:
    """Everything the AMC says it has published, newest first."""
    adapter = DISCOVERY[amc_id]
    spec = adapter.request()

    # Through `conditional_get`, not around it. The first version called
    # `httpx.request` directly and so skipped the robots.txt check, the retry
    # loop and the jittered backoff on the FIRST request this job makes to an
    # AMC -- while `download()` twenty lines below honoured all three. A
    # discovery call is not exempt from §2.3 because it asks for a list rather
    # than a file, and a transient 500 from one house should not turn
    # `jobs.status --check` into a traceback for every house.
    response = conditional_get(
        str(spec["url"]),
        user_agent=str(cfg["user_agent"]),
        method=str(spec.get("method") or "GET"),
        json_body=spec.get("json"),
        extra_headers=dict(spec.get("headers") or {}),
        timeout_connect=float(cfg["timeout_connect"]),
        timeout_read=float(cfg["timeout_read"]),
        retries=int(cfg["retries"]),
        from_email=str(cfg.get("from_email") or "") or None,
        limiter=DomainRateLimiter(float(cfg["rate_limit_per_sec"]), int(cfg["burst"])),
        robots=RobotsCache() if cfg.get("respect_robots") else None,
        client=client,
    )
    response.raise_for_status()
    return adapter.parse_listing(response.content)


#: What a disclosure download may be named: whatever `ingest_inbox` can read,
#: plus the ZIP it now expands. Derived rather than restated -- two hand-kept
#: lists drift, and a format added to one but not the other downloads and is
#: then silently skipped, which is exactly the ICICI ZIP bug in a new guise.
#: The allow-list also removes `.bat`, `.lnk` and `.ps1` endings entirely.
DOWNLOADABLE = (*WORKBOOKS, ".zip")


def download(found: DiscoveredFile, cfg: dict[str, Any], into: Path) -> Path:
    """The file, into the inbox, under the name the AMC published it as.

    Written to a `.part` and renamed, so an interrupted fetch cannot leave a
    truncated workbook where `ingest_inbox` will find it and try to parse it.
    The same argument as §5.2's write-then-manifest, one layer out.
    """
    response = conditional_get(
        found.url,
        user_agent=str(cfg["user_agent"]),
        timeout_connect=float(cfg["timeout_connect"]),
        timeout_read=float(cfg["timeout_read"]),
        retries=int(cfg["retries"]),
        from_email=str(cfg.get("from_email") or "") or None,
        limiter=DomainRateLimiter(float(cfg["rate_limit_per_sec"]), int(cfg["burst"])),
        robots=RobotsCache() if cfg.get("respect_robots") else None,
    )
    response.raise_for_status()
    if not response.content:
        raise FetchError(f"{found.url} returned no bytes")

    into.mkdir(parents=True, exist_ok=True)

    # `found.filename` is whatever the AMC's listing JSON said, and `into /
    # name` contains nothing by itself: pathlib splits on both separators on
    # Windows, honours `..`, and an ABSOLUTE right operand discards the left
    # entirely. A hijacked listing could therefore name any path the user can
    # write -- a .bat in the Startup folder, say -- and `write_bytes` below
    # would put its own bytes there.
    #
    # This project already treats AMC-published content as untrusted
    # (`tests/unit/test_security.py` hardens against XSS and CSV formula
    # payloads in the same documents). The listing is the same boundary, and
    # every OTHER network-to-disk write here is content-addressed and ignores
    # the publisher's name entirely (`fetch/base.py`'s `archive_path`). This
    # one needs a name, so it takes only the basename.
    #
    # PureWindowsPath, not Path: it splits on `\\` on every platform. Path
    # does not on Linux, which would leave `..\\..\\evil` intact there.
    name = PureWindowsPath(found.filename).name
    if not name.lower().endswith(DOWNLOADABLE):
        raise FetchError(
            f"{found.url}: refusing publisher filename {found.filename!r}"
        )
    target = into / name
    part = target.with_suffix(target.suffix + ".part")
    part.write_bytes(response.content)
    part.replace(target)
    return target


def run(
    amc_id: str,
    period: str | None = None,
    kind: str | None = None,
    list_only: bool = False,
) -> list[dict[str, object]]:
    if amc_id not in DISCOVERY:
        raise SystemExit(
            f"no discovery adapter for {amc_id!r}; have {', '.join(sorted(DISCOVERY))}"
        )
    cfg = source(SOURCE_ID)
    found = listing(amc_id, cfg)

    if list_only:
        return [
            {
                "as_of": str(f.as_of),
                "kind": f.kind,
                "filename": f.filename,
                "title": f.title,
            }
            for f in sorted(found, key=lambda f: f.as_of, reverse=True)
        ]

    wanted = for_period(found, period, kind)
    if not wanted:
        raise SystemExit(
            f"{amc_id}: nothing published for {period or 'the latest period'}"
            f"{f' as {kind}' if kind else ''}."
            f" `--list` shows {len(found)} disclosures,"
            f" newest {max(f.as_of for f in found)}."
        )

    out: list[dict[str, object]] = []
    for f in wanted:
        target = download(f, cfg, inbox_root())
        out.append(
            {
                "as_of": str(f.as_of),
                "kind": f.kind,
                "bytes": target.stat().st_size,
                "saved": str(target),
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amc", required=True, help=", ".join(sorted(DISCOVERY)))
    parser.add_argument("--period", help="YYYY-MM; omit for the latest published")
    parser.add_argument(
        "--kind",
        choices=["monthly", "fortnightly"],
        help="Kotak publishes both and they are different documents",
    )
    parser.add_argument(
        "--list", action="store_true", help="print what is published, fetch nothing"
    )
    args = parser.parse_args(argv)

    rows = run(args.amc, args.period, args.kind, args.list)
    for row in rows:
        print("  " + "  ".join(f"{v}" for v in row.values()))
    if not args.list and rows:
        print(f"\n{len(rows)} file(s) in {inbox_root()}. Now:")
        print("  python -m jobs.ingest_inbox")
    return 0


if __name__ == "__main__":
    sys.exit(main())
