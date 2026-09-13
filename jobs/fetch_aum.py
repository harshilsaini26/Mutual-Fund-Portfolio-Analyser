"""Load AMFI's scheme-wise average AUM. S3, DECISIONS V1-49.

    python -m jobs.fetch_aum --list       # what AMFI has published
    python -m jobs.fetch_aum              # the newest quarter
    python -m jobs.fetch_aum --period 2   # a specific one, by the id --list shows

**This is what §10's V2 has been waiting for.** V2 is the units check — the one
that catches §7.2's 100x error and quarantines rather than warns — and it
reconciles a disclosure's summed market value against `scheme_aum`. That table
did not exist, so V2 has never run on a single disclosure here: 0 of 205 carried
an `aum_reported`, and a portfolio a hundred times too large would have loaded
clean on either tier.

**The scheme is the sum of its plans.** AMFI publishes one row per share class,
so `HDFC Flexi Cap Fund - Growth Option - Direct Plan` reports Rs 34,740 Cr
against a Rs 113,606 Cr portfolio. V1-37 settled that a disclosure describes the
SCHEME, every plan of which holds one pool of assets, so the figure stored for a
scheme is its family's total — and it is stored against EVERY member, because
`aum_for` is asked about whichever ISIN a disclosure happened to be loaded
against.

A scheme with no `scheme_family` gets its own plan's figure and nothing else.
That is the same conservative behaviour V1-37's migration describes for a NULL
family: it under-reports rather than guessing at a grouping that was never shown
coherent, and an under-reported AUM makes V2 stricter rather than blinder.
"""

from __future__ import annotations

import argparse
import sys
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from src.common.decimals import connect
from src.m0_data.config import raw_root, source, warehouse_path
from src.m0_data.fetch.amfi_aum import (
    AAUM_UNIT,
    BASIS,
    SchemeAaum,
    data_url,
    parse_aaum,
    parse_periods,
    parse_years,
    periods_url,
    quarter_end,
    years_url,
)
from src.m0_data.fetch.base import (
    DomainRateLimiter,
    FetchCandidate,
    RobotsCache,
    archive,
    conditional_get,
)
from src.m0_data.normalise.units import to_inr

SOURCE_ID = "S3"


def _get(url: str, cfg: dict[str, Any], client: Any = None) -> bytes:
    response = conditional_get(
        url,
        user_agent=str(cfg["user_agent"]),
        extra_headers={"Accept": "application/json"},
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


def published(cfg: dict[str, Any]) -> list[tuple[int, int, str]]:
    """Every `(fy_id, period_id, label)` AMFI lists, newest first.

    Only the newest financial year's quarters are walked. Older ones are one
    more request each and nothing needs them yet; `--year` exists for when a
    backfill does.
    """
    years = parse_years(_get(years_url(), cfg))
    fy_id, _ = years[0]
    return [
        (fy_id, period_id, label)
        for period_id, label in parse_periods(_get(periods_url(fy_id), cfg))
    ]


def _archived(conn: Any, file_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM raw_file WHERE file_id = ?", (file_id,)).fetchone()
        is not None
    )


def _families(conn: Any) -> dict[str, tuple[str, str | None]]:
    """`amfi_code` -> (scheme_id, family key), for every scheme AMFI numbers.

    Loaded once. The alternative is a query per share class, and the payload
    carries 8,545 of them.
    """
    return {
        str(code): (str(scheme_id), f"{amc}|{family}" if family else None)
        for code, scheme_id, amc, family in conn.execute(
            "SELECT amfi_code, scheme_id, amc_id, scheme_family FROM scheme"
            " WHERE amfi_code IS NOT NULL"
        )
    }


def load_quarter(
    conn: Any, rows: list[SchemeAaum], label: str, file_id: str
) -> dict[str, object]:
    """Sum each family's share classes and write one row per member scheme."""
    as_of = quarter_end(label)
    index = _families(conn)

    # Family total first, then written back to every member. A scheme outside
    # any family is its own group of one, keyed by scheme_id so it cannot
    # collide with a real family.
    totals: dict[str, Decimal] = defaultdict(Decimal)
    members: dict[str, list[str]] = defaultdict(list)
    unknown = 0
    for row in rows:
        found = index.get(row.amfi_code)
        if found is None:
            unknown += 1
            continue
        scheme_id, family = found
        key = family or f"scheme:{scheme_id}"
        totals[key] += to_inr(row.aaum_raw, AAUM_UNIT)
        members[key].append(scheme_id)

    now = datetime.now(UTC)
    written = 0
    for key, total in totals.items():
        for scheme_id in members[key]:
            conn.execute(
                "INSERT OR REPLACE INTO scheme_aum (scheme_id, as_of_date, aum_inr,"
                " folio_count, basis, period_label, source_file_id, ingested_at)"
                " VALUES (?,?,?,NULL,?,?,?,?)",
                (scheme_id, as_of, total, BASIS, label, file_id, now),
            )
            written += 1
    conn.commit()
    return {
        "as_of": str(as_of),
        "families": len(totals),
        "scheme_aum_rows": written,
        "unmatched_amfi_codes": unknown,
    }


def run(period_id: int | None = None, list_only: bool = False) -> list[dict[str, object]]:
    cfg = source(SOURCE_ID)
    quarters = published(cfg)
    if list_only:
        return [
            {"period_id": pid, "label": label, "quarter_end": str(quarter_end(label))}
            for _, pid, label in quarters
        ]

    wanted = [q for q in quarters if q[1] == period_id] if period_id else quarters[:1]
    if not wanted:
        raise SystemExit(
            f"no period {period_id} in the newest financial year;"
            f" `--list` shows {[q[1] for q in quarters]}"
        )
    fy_id, pid, label = wanted[0]

    conn = connect(str(warehouse_path()))
    url = data_url(fy_id, pid)
    content = _get(url, cfg)

    result, path = archive(
        content,
        FetchCandidate(url=url, source_id=SOURCE_ID),
        "application/json",
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

    rows = parse_aaum(content)
    counts = load_quarter(conn, rows, label, str(result.file_id))
    conn.execute(
        "UPDATE raw_file SET parse_status='ok', parser_id='aum.amfi',"
        " parser_version='1', parsed_at=?, as_of_date=? WHERE file_id=?",
        (datetime.now(UTC), counts["as_of"], result.file_id),
    )
    conn.commit()
    return [
        {"period": label, "share_classes": len(rows), "fetch": result.status, **counts}
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--period", type=int, help="period id, from --list")
    parser.add_argument(
        "--list", action="store_true", help="print what AMFI has published"
    )
    args = parser.parse_args(argv)
    for row in run(args.period, args.list):
        print("  " + "  ".join(f"{k}={v}" for k, v in row.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
