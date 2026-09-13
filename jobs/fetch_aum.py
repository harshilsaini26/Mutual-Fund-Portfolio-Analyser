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
    NothingPublished,
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
from src.m0_data.load import has_table
from src.m0_data.normalise.units import to_inr


class AumScaleError(RuntimeError):
    """The fetched AAUM is not on the scale `AAUM_UNIT` claims.

    `RuntimeError` so `jobs/ingest_inbox.py`'s per-file guard already catches
    it, and raised rather than warned because a 100x-wrong AUM does not
    degrade V2 -- it inverts it, quarantining every correct disclosure in the
    warehouse.
    """


SOURCE_ID = "S3"

#: How far back a named `--quarter` may look before giving up. Four financial
#: years is sixteen quarters, past anything `scheme_aum` has a use for, and it
#: bounds the walk that a mistyped date would otherwise turn into a crawl of
#: AMFI's whole history.
_STOP_AT_YEARS = 4


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
        except NothingPublished:
            # A financial year listed before its first quarter is published.
            # ONLY this case. A payload that will not parse keeps raising --
            # catching the parent swallowed a maintenance page thirteen times
            # and then blamed AMFI for publishing nothing.
            continue
        out.extend(
            Quarter(fy_id, period_id, label, quarter_end(label))
            for period_id, label in periods
        )
        filled += 1
        if stop_at is not None and any(q.ends == stop_at for q in out):
            break
        # The budget caps BOTH walks. Gating it behind `stop_at is None` meant
        # a quarter that does not exist -- a typo, or one AMFI has not
        # published -- walked every financial year listed: 13+ years at two
        # requests each, and at S3's 0.5 req/s a full minute of polite
        # crawling before reporting a typo.
        if filled >= max(years, _STOP_AT_YEARS if stop_at is not None else 0):
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


#: What a scheme code names: a scheme_id, and the family it belongs to (or
#: None when V1-37's derivation refused to place it in one).
_Member = tuple[str, tuple[str, str] | None]

#: The grouping key. A tuple so two families cannot collide through string
#: concatenation, and `("scheme", scheme_id)` for a scheme V1-37 left outside
#: any family -- its own group of one, which under-reports rather than
#: inventing a grouping that was never shown coherent.
_GroupKey = tuple[str, str]


def _group_key(found: list[_Member]) -> _GroupKey | None:
    """One key for a code's schemes, used by BOTH `totals` and `members`.

    **None means refuse.** A code whose schemes name two DIFFERENT families is
    a contradiction: AMFI numbers them as one share class and V1-37 placed them
    in separate funds, and nothing here can say which is wrong. The first
    version picked the first family and gave its total to every member — so a
    scheme in family B was written family A's AAUM, measured at **20x** its
    fund's actual figure on a two-line reproduction. V2 would then reconcile
    that scheme's disclosure against another fund entirely.

    That is worse than the stranding it replaced. A missing witness disables a
    check and says so; a wrong one corrupts it silently. V1-37's own derivation
    refuses a family it cannot show coherent rather than guessing, and this is
    the same question one level out.

    A family beside a **None** is not a disagreement. Schemes sharing an AMFI
    code are one share class by AMFI's own numbering, so they belong to one
    family by construction; a None is V1-37 declining to place a scheme, which
    is missing information rather than conflicting information. Those group
    together under the family that IS known.
    """
    families = {family for _, family in found if family is not None}
    if len(families) > 1:
        return None
    if families:
        return families.pop()
    return ("scheme", found[0][0])


def _families(conn: Any) -> dict[str, list[_Member]]:
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
    index: dict[str, list[_Member]] = defaultdict(list)
    for code, scheme_id, amc, family in conn.execute(
        "SELECT amfi_code, scheme_id, amc_id, scheme_family FROM scheme"
        " WHERE amfi_code IS NOT NULL ORDER BY amfi_code, scheme_id"
    ):
        # A TUPLE, not `f"{amc}|{family}"`. Concatenating a composite key lets
        # `("a", "b|c")` and `("a|b", "c")` collide into one group, and
        # `scheme_family` is derived from AMFI's free-text scheme names.
        index[str(code)].append(
            (str(scheme_id), (str(amc), str(family)) if family else None)
        )
    return dict(index)


#: How far the TYPICAL family's AAUM may sit from that fund's own disclosed
#: portfolio before the load is refused.
#:
#: On the MEDIAN, not the worst, and that is the whole design. A unit change
#: moves every scheme by the same factor, so the median moves with it; a fund
#: that doubled since the quarter being averaged moves only itself. The first
#: version compared the maximum and aborted a correct load on two small index
#: funds — one at 7.4x, having grown from Rs 1 Cr to Rs 7 Cr in two months,
#: which is what a new index fund gathering assets looks like.
#:
#: Measured on the live warehouse: median **1.06**, p90 1.23, max 7.42 across
#: 194 schemes. A switch from lakh to crore would put the median at **106**. A
#: bound of 10 sits an order of magnitude clear of both.
_SCALE_TOLERANCE = Decimal(10)


def assert_scale(
    conn: Any, totals: dict[_GroupKey, Decimal], members: dict[_GroupKey, set[str]]
) -> str:
    """Refuse a load whose AAUM does not agree in ORDER OF MAGNITUDE.

    `AAUM_UNIT = "lakh"` is asserted — no field on the endpoint states a unit
    — and `amfi_aum.py`'s docstring argues the assertion is safe because a
    family's sum lands within a few percent of that fund's own disclosed
    portfolio. **That comparison was only ever performed by a test against a
    frozen fixture.** A live switch to crore would have loaded all 8,545
    schemes at a hundredth of their AUM, every test would still have passed,
    and V2 would then have quarantined the whole warehouse while blaming the
    disclosures rather than the witness.

    The comparison the docstring describes is available here, in the data, at
    load time: any scheme that already has a current disclosure carries a
    `total_mv` computed from an entirely different source. Doing it is the
    difference between an argument and a check.

    Not a valuation check. AAUM is a quarterly average against a month-end
    portfolio and the two legitimately differ by ~10% (V1-49); the only error
    this has to catch is a factor of 100.
    """
    # A warehouse that has loaded no disclosure yet has no table to compare
    # against -- migration 004 creates it -- and a missing witness is not a
    # failing one. `has_table` is the check `aum_for` already uses for exactly
    # this, rather than a second spelling of it here.
    if not has_table(conn, "holding_disclosure"):
        return "no disclosure to compare against"
    known = {
        str(r[0]): Decimal(r[1])
        for r in conn.execute(
            "SELECT scheme_id, total_mv FROM holding_disclosure WHERE is_current = 1"
        )
    }
    if not known:
        return "no disclosure to compare against"

    ratios: list[Decimal] = []
    worst = Decimal(0)
    worst_scheme = ""
    for key, total in totals.items():
        for scheme_id in sorted(members[key]):
            disclosed = known.get(scheme_id)
            if disclosed is None or total <= 0 or disclosed <= 0:
                continue
            ratio = max(total, disclosed) / min(total, disclosed)
            ratios.append(ratio)
            if ratio > worst:
                worst, worst_scheme = ratio, scheme_id
    if not ratios:
        return "no disclosure to compare against"

    ratios.sort()
    median = ratios[len(ratios) // 2]
    if median > _SCALE_TOLERANCE:
        raise AumScaleError(
            f"the typical scheme's AAUM disagrees with its disclosed portfolio"
            f" by {median:.1f}x across {len(ratios)} schemes."
            f" `AAUM_UNIT` is asserted as {AAUM_UNIT!r}; a median near 100 means"
            f" AMFI changed it."
        )
    return (
        f"{len(ratios)} schemes, median {median:.2f}x,"
        f" worst {worst:.2f}x ({worst_scheme})"
    )


def load_quarter(
    conn: Any, rows: list[SchemeAaum], label: str, file_id: str
) -> dict[str, object]:
    """Sum each family's share classes and write one row per member scheme."""
    as_of = quarter_end(label)
    index = _families(conn)

    # Family total first, then written back to every member. A scheme outside
    # any family is its own group of one, keyed by scheme_id so it cannot
    # collide with a real family.
    totals: dict[_GroupKey, Decimal] = defaultdict(Decimal)
    members: dict[_GroupKey, set[str]] = defaultdict(set)
    unknown = 0
    incoherent = 0
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
        # ONE key for both maps, and that is the whole correction. The first
        # version keyed `totals` on `found[0]`'s family and `members` on each
        # scheme's OWN family, so a code naming two schemes with different
        # family keys registered the second under a key `totals` never got --
        # and the write loop iterates `totals`. Reproduced: code 100034 naming
        # INF001 (no family) and INF002 (family `abc|Fund`) wrote INF001 alone
        # and reported `scheme_aum_rows: 1, unmatched_amfi_codes: 0`. Success,
        # with a scheme missing. That is the same silent loss this function was
        # rewritten to fix, one layer in.
        key = _group_key(found)
        if key is None:
            # Counted, not dropped in silence. §4.10's rule is that a row never
            # disappears without a record, and a code whose schemes contradict
            # each other about their fund is exactly the case a human has to
            # see rather than a number the loader invents.
            incoherent += 1
            continue
        totals[key] += to_inr(row.aaum_raw, AAUM_UNIT)
        members[key].update(scheme_id for scheme_id, _ in found)

    now = datetime.now(UTC)
    # Every figure already stored for this quarter, in ONE query. The first
    # version issued a `SELECT` per scheme inside the write loop -- 12,388
    # extra round trips on the live data, for a counter that only reports --
    # where `_families` twenty lines above already prefetches for exactly this
    # reason.
    stored: dict[str, Decimal] = {
        str(r[0]): r[1]
        for r in conn.execute(
            "SELECT scheme_id, aum_inr FROM scheme_aum WHERE as_of_date = ?",
            (as_of,),
        )
    }

    written = 0
    restated = 0
    for key, total in totals.items():
        # Sorted, so a rebuild writes the same rows in the same order.
        for scheme_id in sorted(members[key]):
            # `INSERT OR REPLACE` overwrites in place, which every other fact
            # table in this warehouse refuses to do. `scheme_aum` has no
            # revision column in MODULE_0 §4's schema, so the overwrite stands
            # -- but a figure that MOVED is news, and counting it is what stops
            # a restatement being silent.
            #
            # Compared as DECIMALS. `str(prior) != str(total)` made
            # `Decimal("1000")` and `Decimal("1000.00")` a restatement, so a
            # change in AMFI's published precision would have reported every
            # scheme in the file as restated while nothing moved -- and a
            # counter that cries wolf at that scale reports nothing at all.
            prior = stored.get(scheme_id)
            if prior is not None and Decimal(prior) != total:
                restated += 1
            conn.execute(
                "INSERT OR REPLACE INTO scheme_aum (scheme_id, as_of_date, aum_inr,"
                " folio_count, basis, period_label, source_file_id, ingested_at)"
                " VALUES (?,?,?,NULL,?,?,?,?)",
                (scheme_id, as_of, total, BASIS, label, file_id, now),
            )
            written += 1
    scale = assert_scale(conn, totals, members)

    conn.commit()
    return {
        "as_of": str(as_of),
        "scale_check": scale,
        "families": len(totals),
        "scheme_aum_rows": written,
        "unmatched_amfi_codes": unknown,
        "incoherent_families": incoherent,
        "restated": restated,
    }


def run(
    quarter: str | None = None,
    list_only: bool = False,
    years: int = 1,
    client: Any = None,
) -> list[dict[str, object]]:
    """`client` exists so this whole job can be driven without the network.

    Both helpers already took one and `run` did not pass it, so every path
    through here -- the quarter lookup, the SystemExit on a quarter AMFI has
    not published, the archive-then-register sequence, the `raw_file` status
    update -- was verified only by having been run by hand. Three consecutive
    review rounds found a defect in a function that had no test.
    """
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
    quarters = published(cfg, years=years, client=client, stop_at=wanted_end)
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
    content = _get(url, cfg, client)

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
