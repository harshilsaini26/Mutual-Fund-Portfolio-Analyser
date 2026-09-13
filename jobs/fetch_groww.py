"""Load a scheme's portfolio from its Groww page. DECISIONS V1-43.

    python -m jobs.fetch_groww                      # every slug in the map
    python -m jobs.fetch_groww --scheme INF179K01UT0
    python -m jobs.fetch_groww --slug <slug> --dry-run

**The coverage tier.** `load_holdings` reads an AMC's own statutory workbook
and so covers the five fund houses that have a parser; this reads an
aggregator's page and covers any fund Groww lists. It is the answer to holding
a fund from an AMC nobody has written a reader for, and it is NOT a replacement
for the workbook where one exists -- the page carries no ISIN column, which
costs 8.96% of rows unresolved against 0.00% on the same fund and month.

**The slug is the whole risk, so it is checked twice.** A slug cannot be built
from a scheme name -- Groww keeps the name a fund had before it was renamed, so
HDFC Flexi Cap lives at `hdfc-equity-fund-direct-growth` and Parag Parikh Flexi
Cap at `parag-parikh-long-term-value-fund-direct-growth`. A wrong slug that
404s is harmless. A wrong slug that resolves is not: it would load a real
portfolio under the wrong `scheme_id`, and nothing downstream could tell. So
the page's own `isin` is compared against the one the map promised and a
mismatch REFUSES rather than loads.
"""

from __future__ import annotations

import argparse
import sys
from datetime import UTC, date, datetime
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
from src.m0_data.load import load_holdings
from src.m0_data.normalise.instrument_class import (
    class_from_section,
    instrument_class,
)
from src.m0_data.normalise.units import to_inr
from src.m0_data.normalise.weights import normalise_weights
from src.m0_data.parse.base import ParseFailed, RawFile
from src.m0_data.parse.holdings.groww import GrowwHoldingsParser
from src.m0_data.resolve.cascade import (
    load_isin_prefix_index,
    load_issuer_index,
    resolve,
)
from src.m0_data.validate.checks import (
    HoldingRow,
    as_json,
    promote_or_quarantine,
    validate_disclosure,
)

SOURCE_ID = "S7"
SLUGS_YAML = REPO_ROOT / "config" / "groww_slugs.yaml"

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


def fetch_page(slug: str, cfg: dict[str, Any]) -> tuple[bytes, str]:
    """One polite GET of `https://groww.in/mutual-funds/<slug>`.

    `respect_robots` is on for this source and the check runs here rather than
    being assumed: robots.txt allows `/mutual-funds/<slug>` today, and a source
    whose permission is read once at implementation time is a source whose
    permission goes stale silently.
    """
    url = str(cfg["url"]).format(slug=slug)
    response = conditional_get(
        url,
        user_agent=str(cfg["user_agent"]),
        timeout_connect=float(cfg["timeout_connect"]),
        timeout_read=float(cfg["timeout_read"]),
        retries=int(cfg["retries"]),
        from_email=str(cfg.get("from_email") or "") or None,
        limiter=DomainRateLimiter(float(cfg["rate_limit_per_sec"]), int(cfg["burst"])),
        robots=RobotsCache() if cfg.get("respect_robots") else None,
    )
    response.raise_for_status()
    return response.content, url


def page_isin(content: bytes) -> str | None:
    """The ISIN the page states for ITSELF, which is the only trustworthy one."""
    from src.m0_data.parse.holdings.groww import _payload

    data = _payload(RawFile("probe", SOURCE_ID, "probe.html", content))
    isin = data.get("isin")
    return str(isin).strip().upper() if isin else None


def _archived(conn: Any, file_id: str) -> bool:
    row = conn.execute("SELECT 1 FROM raw_file WHERE file_id = ?", (file_id,)).fetchone()
    return row is not None


def _one(
    conn: Any,
    scheme_id: str | None,
    slug: str,
    cfg: dict[str, Any],
    index: dict[str, str],
    prefixes: dict[str, str],
    dry_run: bool,
    force: bool = False,
) -> dict[str, object]:
    content, url = fetch_page(slug, cfg)

    # Parsed ONCE. The first draft parsed here for the ISIN check, again for
    # the dry-run summary and a third time after archiving -- three passes of
    # the `__NEXT_DATA__` regex and three `json.loads` of a ~500 KB payload for
    # one page. Nothing the later passes needed came from the parse; the only
    # thing that changes after archiving is the `file_id`, which is a field on
    # `RawFile` and not an input to parsing.
    parsed = GrowwHoldingsParser().parse(
        RawFile("probe", SOURCE_ID, f"{slug}.html", content)
    )
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

    # The scheme has to exist before a disclosure can be filed against it.
    #
    # `holding_disclosure.scheme_id REFERENCES scheme(scheme_id)` is declared
    # and INERT -- SQLite enforces foreign keys only under
    # `PRAGMA foreign_keys = ON`, which this project does not set. So with
    # `--slug`, which bypasses the map and takes whatever ISIN the page states,
    # an unknown scheme wrote a full disclosure and every holding row under an
    # id no scheme has. It then vanished from anything that joins `scheme` --
    # including `jobs/status.py`'s own report -- so the fund read as loaded and
    # missing at the same time depending which query you asked.
    known = conn.execute(
        "SELECT 1 FROM scheme WHERE scheme_id = ?", (scheme_id,)
    ).fetchone()
    if not known:
        raise SlugMismatch(
            f"{slug}: {scheme_id} is not in the scheme master. Load the AMFI"
            " universe first (`python -m jobs.fetch_nav`); refusing to file a"
            " portfolio against a scheme nothing else can see."
        )

    # Don't fetch what would not be read.
    #
    # This began as the guard that kept the coverage tier from outranking an
    # AMC file, and it was the WRONG PLACE for it: a rule enforced by the job
    # that writes the data is a rule the next writer has to remember, and
    # `latest_as_of` was meanwhile taking `max(as_of_date)` and reading no
    # tier at all. V1-46 moved the decision into
    # `m3_lookthrough.weights.latest_disclosure`, where every reader passes.
    #
    # What is left here is not correctness, it is restraint: a scheme the AMC
    # tier already covers would have its page fetched, parsed, resolved and
    # stored, and then never selected. `--force` is for deliberately putting
    # the two side by side.
    covered = conn.execute(
        "SELECT as_of_date FROM holding_disclosure"
        " WHERE scheme_id = ? AND is_current = 1 AND source_tier = 'amc_direct'"
        " ORDER BY as_of_date DESC LIMIT 1",
        (scheme_id,),
    ).fetchone()
    if covered and not force:
        return {
            "slug": slug,
            "scheme_id": scheme_id,
            "as_of": str(parsed.as_of_date),
            "skipped": f"covered amc_direct at {covered[0]}",
            "hint": "--force to load anyway; the AMC's own file resolves more",
        }

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

    parser = GrowwHoldingsParser()
    securities = parsed.securities

    values = [
        to_inr(s.market_value_raw, s.market_value_unit)
        if s.market_value_raw is not None
        else None
        for s in securities
    ]
    pcts = [
        s.pct_to_nav_raw * parsed.pct_scale if s.pct_to_nav_raw is not None else None
        for s in securities
    ]
    weights = normalise_weights(values, pcts)

    rows: list[dict[str, object]] = []
    for security, value, weight, pct in zip(
        securities, values, weights.weights, pcts, strict=True
    ):
        resolution = resolve(
            conn,
            security.instrument_raw_name,
            # None, always. The page has no ISIN column and §6.3 rule 1 means
            # the parser did not invent one -- this is where that costs.
            None,
            class_from_section(security.section),
            index,
            prefixes,
        )
        rows.append(
            {
                "isin": None,
                "issuer_id": str(resolution.issuer_id),
                "instrument_raw_name": security.instrument_raw_name,
                "quantity": security.quantity_raw,
                "market_value": value if value is not None else Decimal(0),
                "pct_to_nav": pct,
                "pct_normalised": weight,
                "instrument_class": instrument_class(
                    security.section, str(resolution.issuer_id)
                ),
                "reported_sector": security.reported_sector,
                "resolution_method": resolution.method,
                "resolution_conf": resolution.confidence,
            }
        )

    # §10's V2 is THE units check, and it was being skipped here: the call
    # passed `aum_reported=None`, so every Groww disclosure recorded "no AUM on
    # record to reconcile against". That is the wrong path to disable it on.
    # `MARKET_VALUE_UNIT` is ASSERTED on this page rather than read off a header
    # -- no cell states a unit -- and the parser's own `_check_unit` compares
    # rows to the page's `aum`, both in the same unit, so it scales with a unit
    # error and can never catch one. `scheme_aum` is the only witness here that
    # is independent of the page, which is exactly what V2 wants.
    found_aum = _aum_for(conn, scheme_id, parsed.as_of_date)
    aum, aum_basis = found_aum if found_aum else (None, "point_in_time")

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
        aum_basis,
    )
    status = promote_or_quarantine(checks)
    unresolved = next(c for c in checks if c.code == "V3").observed or "0%"

    # `holding.market_value` is NOT NULL, so a row the page did not price is
    # stored as zero and `normalise_weights` independently gives it weight zero
    # -- it contributes nothing to any look-through while every quality figure
    # is computed against a total that already excludes it. Nothing moves and
    # nobody is told, which is what `CLAUDE.md` invariant 4 forbids. The AMC
    # path has carried this list since V1-42; this one was omitting it.
    unpriced = [
        securities[i].instrument_raw_name
        for i, value in enumerate(values)
        if value is None
    ]

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
            "reported_unit": securities[0].market_value_unit if securities else None,
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
    args = parser.parse_args(argv)

    for row in run(args.scheme, args.slug, args.dry_run, args.force):
        for key, value in row.items():
            print(f"  {key:20} {value}")
        print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
