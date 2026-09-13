"""Load AMFI's scheme-wise average AUM. S3, DECISIONS V1-49.

    python -m jobs.fetch_aum --list --years 4    # what AMFI has published
    python -m jobs.fetch_aum                     # the newest quarter
    python -m jobs.fetch_aum --quarter 2026-03-31

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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from src.common.decimals import connect
from src.m0_data.config import raw_root, source, warehouse_path
from src.m0_data.fetch.amfi_aum import (
    AAUM_UNIT,
    BASIS,
    AumPayloadError,
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


@dataclass(frozen=True)
class Quarter:
    """One published quarter, addressed by the only key that is stable.

    `period_id` restarts at 1 in EVERY financial year — id 1 is
    `January - March 2026` in FY2025-26 and `April - June 2026` in FY2026-27 —
    so a CLI that took it would mean a different quarter depending on when it
    ran. `ends` is unambiguous, and it is already what `scheme_aum.as_of_date`
    stores.
    """

    fy_id: int
    period_id: int
    label: str
    ends: date


def published(
    cfg: dict[str, Any],
    years: int = 1,
    client: Any = None,
    stop_at: date | None = None,
) -> list[Quarter]:
    """The quarters AMFI lists, newest first, from `years` financial years.

    **Years with nothing published are skipped, not fatal.** AAUM arrives about
    ten days after a quarter ends, so between April and mid-July the newest
    financial year can be listed with zero periods in it — and the first
    version took `years[0]` unconditionally, so `parse_periods` raised and the
    whole job died for roughly a quarter of every year while the previous
    year's figure sat one request away.

    Walked lazily, and that is the whole reason it takes a budget. One year
    costs two requests and stops there, which is all the default run needs;
    `stop_at` stops the moment a named quarter is seen, so asking for
    `2026-03-31` costs three requests rather than the eight that walking four
    years to be safe would.
    """
    listed = parse_years(_get(years_url(), cfg, client))
    out: list[Quarter] = []
    filled = 0
    for fy_id, _ in listed:
        try:
            periods = parse_periods(_get(periods_url(fy_id), cfg, client))
        except AumPayloadError:
            # A financial year listed before its first quarter is published.
            continue
        out.extend(
            Quarter(fy_id, period_id, label, quarter_end(label))
            for period_id, label in periods
        )
        filled += 1
        if stop_at is not None and any(q.ends == stop_at for q in out):
            break
        if stop_at is None and filled >= years:
            break
    if not out:
        raise AumPayloadError(
            "AMFI lists financial years but none of them has a published quarter"
        )
    return out


def _archived(conn: Any, file_id: str) -> bool:
    return (
        conn.execute("SELECT 1 FROM raw_file WHERE file_id = ?", (file_id,)).fetchone()
        is not None
    )


def _families(conn: Any) -> dict[str, list[tuple[str, str | None]]]:
    """`amfi_code` -> EVERY (scheme_id, family key) it names.

    **A list, because an AMFI scheme code is not one scheme.** 4,592 of them
    name two ISINs in this warehouse: `100034` is both `INF209K01157` (Aditya
    Birla Large & Mid Cap, IDCW payout) and `INF209K01CE5` (the same fund, IDCW
    reinvest) -- one pool of assets, one code, two ISINs.

    The first version was a dict comprehension keyed on the code, so every
    duplicate but the last was discarded: the live payload's 8,545 codes reach
    **12,388** scheme_ids and only 8,448 got a row, losing 3,940 schemes their
    AUM witness in silence. The run's own summary hid it, reporting 97
    unmatched codes when 4,037 schemes went without.

    Ordered, because which scheme_id a code yields must not depend on SQLite's
    whim. `CLAUDE.md` invariant 10 wants a rebuild to reproduce byte-identical
    output, and an unordered `SELECT` behind a collapsing dict gave neither the
    same rows nor the same choice between them.

    Loaded once. The alternative is a query per share class, and the payload
    carries 8,545 of them.
    """
    index: dict[str, list[tuple[str, str | None]]] = defaultdict(list)
    for code, scheme_id, amc, family in conn.execute(
        "SELECT amfi_code, scheme_id, amc_id, scheme_family FROM scheme"
        " WHERE amfi_code IS NOT NULL ORDER BY amfi_code, scheme_id"
    ):
        index[str(code)].append((str(scheme_id), f"{amc}|{family}" if family else None))
    return dict(index)


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
    members: dict[str, set[str]] = defaultdict(set)
    unknown = 0
    for row in rows:
        found = index.get(row.amfi_code)
        if not found:
            unknown += 1
            continue
        # The AAUM contributes ONCE -- it is one share class's assets, however
        # many ISINs that share class is listed under -- but every scheme_id
        # the code names is a member and must get the family's figure, because
        # `aum_for` is asked about whichever ISIN a disclosure was loaded
        # against. Counting the money once and the members severally is the
        # whole distinction the first version lost.
        primary_family = found[0][1]
        key = primary_family or f"scheme:{found[0][0]}"
        totals[key] += to_inr(row.aaum_raw, AAUM_UNIT)
        for scheme_id, family in found:
            members[family or key].add(scheme_id)

    now = datetime.now(UTC)
    written = 0
    restated = 0
    for key, total in totals.items():
        # Sorted, so a rebuild writes the same rows in the same order.
        for scheme_id in sorted(members.get(key, ())):
            prior = conn.execute(
                "SELECT aum_inr FROM scheme_aum WHERE scheme_id=? AND as_of_date=?",
                (scheme_id, as_of),
            ).fetchone()
            # `INSERT OR REPLACE` overwrites in place, which every other fact
            # table in this warehouse refuses to do. `scheme_aum` has no
            # revision column in MODULE_0 §4's schema, so the overwrite stands
            # -- but a figure that MOVED is news, and counting it is what stops
            # a restatement being silent.
            if prior is not None and str(prior[0]) != str(total):
                restated += 1
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
        "restated": restated,
    }


def run(
    quarter: str | None = None,
    list_only: bool = False,
    years: int = 1,
) -> list[dict[str, object]]:
    cfg = source(SOURCE_ID)
    wanted_end: date | None = None
    if quarter:
        try:
            wanted_end = date.fromisoformat(quarter)
        except ValueError as exc:
            raise SystemExit(f"--quarter {quarter!r} is not YYYY-MM-DD") from exc

    # A named quarter may be older than the newest financial year, so the walk
    # continues until it is found rather than stopping at a fixed depth --
    # and stops the moment it IS found, which is usually the first year.
    quarters = published(cfg, years=years, stop_at=wanted_end)
    if list_only:
        return [
            {"quarter": str(q.ends), "label": q.label, "financial_year_id": q.fy_id}
            for q in quarters
        ]

    wanted = [q for q in quarters if q.ends == wanted_end] if wanted_end else quarters[:1]
    if not wanted:
        raise SystemExit(
            f"AMFI has not published a quarter ending {quarter};"
            f" `--list --years 4` shows {[str(q.ends) for q in quarters][:8]}"
        )
    fy_id, pid, label = wanted[0].fy_id, wanted[0].period_id, wanted[0].label

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
    parser.add_argument(
        "--quarter",
        help="quarter END as YYYY-MM-DD, e.g. 2026-06-30. Stable across years,"
        " unlike AMFI's period ids, which restart at 1 every financial year.",
    )
    parser.add_argument(
        "--years",
        type=int,
        default=1,
        help="how many financial years back to list (default 1)",
    )
    parser.add_argument(
        "--list", action="store_true", help="print what AMFI has published"
    )
    args = parser.parse_args(argv)
    for row in run(args.quarter, args.list, args.years):
        print("  " + "  ".join(f"{k}={v}" for k, v in row.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
