"""Fetch, parse, resolve, normalise, validate and load a disclosure.

    python -m jobs.load_holdings --amc hdfc
    python -m jobs.load_holdings --file "path/to/Monthly ... .xlsx"

The whole L0 -> L3 path for one file, each stage separable on purpose (§3): the
archive is content-addressed, parsing is a pure function on bytes, resolution
reads only the entity master, normalisation and validation read only the parsed
rows. A bug in any one is fixed by re-running from the layer to its left, never
by editing data.

Links come from `config/amc_manifest.yaml`. Discovery for the AMCs that have a
backend listing lives in `src/m0_data/fetch/amc_direct.py` and is driven by
`jobs/fetch_amc.py` (V1-44); this job takes a path or a manifest entry and does
not discover anything itself.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import openpyxl
import yaml
from src.common.decimals import connect
from src.m0_data.config import REPO_ROOT, raw_root, source, warehouse_path
from src.m0_data.derive.disclosed_issuers import derive_disclosed_issuers
from src.m0_data.fetch.base import (
    DomainRateLimiter,
    FetchCandidate,
    archive,
    conditional_get,
)
from src.m0_data.load import aum_for as _aum_for
from src.m0_data.load import build_holding_rows, load_holdings, retire_siblings
from src.m0_data.parse.base import HoldingsParser, ParseFailed, RawFile
from src.m0_data.parse.holdings.registry import by_parser_id, route
from src.m0_data.resolve.cascade import (
    load_isin_prefix_index,
    load_issuer_index,
)
from src.m0_data.resolve.scheme_match import (
    canonical_scheme,
    identify_scheme,
    live_families,
    refuse_contested,
)
from src.m0_data.schema.apply import apply_migrations
from src.m0_data.validate.checks import (
    HoldingRow,
    as_json,
    promote_or_quarantine,
    validate_disclosure,
)

MANIFEST = REPO_ROOT / "config" / "amc_manifest.yaml"
SOURCE_PREFIX = "S5"

def load_manifest(path: Path = MANIFEST) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if not isinstance(loaded, dict) or "disclosures" not in loaded:
        raise ValueError(f"{path} is not an AMC manifest")
    return loaded


def run(
    amc_id: str | None,
    file_path: Path | None = None,
    scheme_id: str | None = None,
    sheet: str | None = None,
    parser_id: str | None = None,
    all_sheets: bool = False,
) -> list[dict[str, object]]:
    # S5 carries the browser agent HDFC's CDN requires, with the contact in
    # `From:` — DECISIONS V1-05.
    cfg = source("S5")
    db_path = warehouse_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    apply_migrations(str(db_path))
    conn = connect(str(db_path))

    run_id = str(uuid.uuid4())
    conn.execute(
        "INSERT INTO job_run (run_id, job_name, started_at, params_json)"
        " VALUES (?,?,?,?)",
        (run_id, "load_holdings", datetime.now(UTC),
         json.dumps({"amc": amc_id, "file": str(file_path) if file_path else None,
                     "scheme": scheme_id})),
    )
    conn.commit()

    summaries: list[dict[str, object]] = []
    try:
        # Issuers the disclosures already loaded have named (V1-41). Run
        # BEFORE building the indexes so this load resolves against what every
        # previous one discovered, and again at the end so the next load
        # resolves against what this one adds. Both calls are a couple of
        # queries; the alternative is telling a person to run the job twice.
        derive_disclosed_issuers(conn)
        conn.commit()
        index = load_issuer_index(conn)
        prefixes = load_isin_prefix_index(conn)
        if all_sheets:
            if not (file_path and amc_id):
                raise RuntimeError("--all-sheets needs --file and --amc")
            found, refused = discover_sheets(conn, file_path, amc_id, parser_id)
            for name, reason, detail in refused:
                summaries.append({
                    "sheet": name, "loaded": "no", "why": reason, "detail": detail,
                })
            entries_to_load = found
        else:
            entries_to_load = _entries(amc_id, file_path, scheme_id, sheet, parser_id)
        for entry in entries_to_load:
            summaries.append(_one(conn, entry, cfg, index, prefixes))

        status = "partial" if any(
            s.get("validation_status") != "ok" for s in summaries
        ) else "ok"
        conn.execute(
            "UPDATE job_run SET finished_at=?, status=?, files_parsed=?,"
            " rows_written=? WHERE run_id=?",
            (datetime.now(UTC), status, len(summaries),
             sum(int(str(s.get("holding", 0))) for s in summaries), run_id),
        )
        conn.commit()
        # And again, so what this load added is available to the next one.
        derive_disclosed_issuers(conn)
        conn.commit()
        return summaries
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


def _entries(
    amc_id: str | None,
    file_path: Path | None,
    scheme_id: str | None,
    sheet: str | None = None,
    parser_id: str | None = None,
) -> list[dict[str, Any]]:
    if file_path:
        # A disclosure names its scheme in prose, and prose is not a key — the
        # same reason every manifest entry carries `scheme_id` (MODULE_0.md
        # §4.4). Guessing it from the filename would file a portfolio against
        # whichever scheme the name most resembled, which for an AMC with four
        # plans of one fund is a coin toss.
        if not scheme_id:
            raise RuntimeError("--file needs --scheme: the ISIN this file describes")
        # `sheet` matters as much as `scheme_id` for a workbook that holds
        # one scheme per sheet. Kotak publishes 119 of them and Nippon 108,
        # so a load without it merges every portfolio in the file into one
        # — which does not fail loudly, it fails as a plausible number.
        return [{
            "amc_id": amc_id or "unknown",
            "path": str(file_path),
            "scheme_id": scheme_id,
            "sheet": sheet,
            "parser": parser_id,
        }]
    manifest = load_manifest()
    rows = [
        e for e in manifest["disclosures"]
        if amc_id is None or e.get("amc_id") == amc_id
    ]
    if not rows:
        raise RuntimeError(f"no manifest entries for amc {amc_id!r}")
    return rows


def discover_sheets(
    conn: Any, file_path: Path, amc_id: str, parser_id: str | None = None
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """One entry per sheet that names a scheme we can be sure of.

    Returns `(entries, refusals)`. A refusal is `(sheet, reason, detail)` and is
    an ordinary outcome, not an error: on the two real multi-sheet workbooks
    roughly one sheet in six refuses, mostly debt and index schemes whose
    header names them by an internal code alone.

    **Every sheet is parsed before it is identified**, which is what keeps a
    contents page out of the results: Nippon's `Index` sheet lists every fund
    in the workbook, and a matcher run on raw cells would have matched it to
    whichever it listed first. It is not a portfolio, so `parse_holdings`
    raises on it and it never reaches the matcher.
    """
    content = file_path.read_bytes()
    file_id = hashlib.sha256(content).hexdigest()
    raw = RawFile(file_id, f"{SOURCE_PREFIX}:{amc_id}", file_path.name, content)
    # Same three rungs as the loader itself (V1-35): an explicit choice, then
    # the parser that read this file before, then the sniff. It matters here
    # because the obvious thing to point `--all-sheets` at is a workbook
    # already in the archive, whose filename is its sha256.
    parser = _parser_for(conn, file_id, raw, parser_id)

    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    sheet_names = list(workbook.sheetnames)
    workbook.close()

    parsed: dict[str, Any] = {}
    refusals: list[tuple[str, str, str]] = []
    for name in sheet_names:
        try:
            result = parser.parse(raw, name)
        except ParseFailed as exc:
            refusals.append((name, "not a portfolio", str(exc)[:70]))
            continue
        if result.as_of_date is None:
            refusals.append((name, "no as-on date", ""))
            continue
        parsed[name] = result

    matches = {}
    families_by_date: dict[date, dict[str, list[str]]] = {}
    for name, result in parsed.items():
        as_of = result.as_of_date
        if as_of not in families_by_date:
            families_by_date[as_of] = live_families(conn, amc_id, as_of)
        matches[name] = identify_scheme(
            result.header_candidates, families_by_date[as_of]
        )
    matches = refuse_contested(matches)

    entries: list[dict[str, Any]] = []
    for name, match in matches.items():
        if not match.family:
            refusals.append((name, match.method, ", ".join(match.rivals)[:70]))
            continue
        scheme_id = canonical_scheme(conn, match.family, amc_id)
        if scheme_id is None:
            refusals.append((name, "no scheme for family", match.family))
            continue
        entries.append({
            "amc_id": amc_id,
            "path": str(file_path),
            "scheme_id": scheme_id,
            "sheet": name,
            "parser": parser.parser_id,
            "matched_by": match.method,
            "family": match.family,
            # A family can appear in several archived disclosures; a caller
            # wanting the current statement of anything needs the newest.
            "as_of": parsed[name].as_of_date,
        })
    return entries, refusals


def _one(
    conn: Any,
    entry: dict[str, Any],
    cfg: dict[str, Any],
    index: dict[str, str],
    prefixes: dict[str, str],
) -> dict[str, object]:
    amc_id = entry["amc_id"]
    source_id = f"{SOURCE_PREFIX}:{amc_id}"

    if entry.get("path"):
        content = Path(entry["path"]).read_bytes()
        url, filename = f"file://{entry['path']}", Path(entry["path"]).name
    else:
        url = entry["url"]
        filename = entry.get("filename") or url.rsplit("/", 1)[-1]
        response = conditional_get(
            url,
            user_agent=str(cfg["user_agent"]),
            timeout_connect=float(cfg["timeout_connect"]),
            timeout_read=float(cfg["timeout_read"]),
            retries=int(cfg["retries"]),
            from_email=str(cfg.get("from_email") or "") or None,
            limiter=DomainRateLimiter(
                float(cfg["rate_limit_per_sec"]), int(cfg["burst"])
            ),
        )
        response.raise_for_status()
        content = response.content

    from urllib.parse import unquote

    filename = unquote(filename)
    result, path = archive(
        content, FetchCandidate(url=url, source_id=source_id),
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        raw_root(), lambda fid: _archived(conn, fid),
    )
    if path is not None:
        conn.execute(
            "INSERT INTO raw_file (file_id, source_id, url, fetched_at, byte_size,"
            " storage_path, parse_status) VALUES (?,?,?,?,?,?, 'pending')",
            (result.file_id, source_id, url, datetime.now(UTC),
             result.byte_size, str(path)),
        )
        conn.commit()

    raw = RawFile(str(result.file_id), source_id, filename, content)
    parser = _parser_for(conn, str(result.file_id), raw, entry.get("parser"))
    # Nippon publishes 108 schemes as 108 sheets of one workbook, so the
    # manifest entry names the sheet. Absent, the whole workbook is read —
    # which is right for HDFC and ICICI, one scheme per file (V1-15).
    parsed = parser.parse(raw, entry.get("sheet"))
    assert parsed.as_of_date is not None  # parse_holdings raises otherwise

    scheme_id = entry["scheme_id"]
    securities = parsed.securities

    rows, weights, unpriced = build_holding_rows(
        conn, securities, parsed.pct_scale, index, prefixes
    )

    witness = _aum_for(conn, scheme_id, parsed.as_of_date)
    aum = witness.amount if witness else None
    aum_basis = witness.basis if witness else "point_in_time"
    aum_as_of = witness.as_of if witness else None
    checks = validate_disclosure(
        [
            HoldingRow(
                str(r["isin"]) if r["isin"] else None,
                str(r["instrument_class"]),
                Decimal(str(r["market_value"])),
                Decimal(str(r["pct_to_nav"])) if r["pct_to_nav"] is not None else None,
                str(r["issuer_id"]),
            )
            for r in rows
        ],
        parsed.as_of_date, date.today(), aum, aum_basis, aum_as_of,
    )
    status = promote_or_quarantine(checks)
    unresolved = next(c for c in checks if c.code == "V3").observed or "0%"

    counts = load_holdings(
        conn, scheme_id, parsed.as_of_date, rows,
        {
            "pct_sum_raw": Decimal(100) - weights.residual,
            "weight_residual": weights.residual,
            "unresolved_mv_pct": Decimal(unresolved.rstrip("%")),
            "total_mv": weights.total_market_value,
            "aum_reported": aum,
            "reported_unit": securities[0].market_value_unit if securities else None,
            "validation_status": status,
            "validation_notes": as_json(checks, unpriced=unpriced),
        },
        str(result.file_id),
    )
    # Even when the load itself was skipped as unchanged: the sibling's filing
    # may predate the family change that moved this sheet.
    retired = retire_siblings(
        conn, scheme_id, entry.get("family"), amc_id, parsed.as_of_date,
        str(result.file_id),
    )
    conn.execute(
        "UPDATE raw_file SET parse_status='ok', parser_id=?, parser_version=?,"
        " parsed_at=?, as_of_date=? WHERE file_id=?",
        (parser.parser_id, parser.version, datetime.now(UTC), parsed.as_of_date,
         result.file_id),
    )
    conn.commit()

    failed = [c.code for c in checks if c.passed is False]
    return {
        "scheme_id": scheme_id, "as_of": str(parsed.as_of_date),
        "parser": parser.parser_id, "fetch": result.status, **counts,
        "unresolved_mv_pct": unresolved, "validation_status": status,
        "failed_checks": ",".join(failed) or "none",
        **({"retired": ",".join(retired)} if retired else {}),
    }


def _parser_for(
    conn: Any, file_id: str, raw: RawFile, override: str | None = None,
) -> HoldingsParser:
    """An explicit choice, then the parser that read this file before, then sniff.

    The middle rung is the one that matters. `sniff` routes on the filename
    (§6.1, and deliberately — opening the workbook would cost a parse per
    candidate), while the archive names a file for its sha256. So a file could
    be parsed on the way in and never again, which quietly made MODULE_0.md §3's
    promise false: re-running from the layer to its left raised
    `NoParserMatched` at 0.10 from everything.

    Preferring the recorded parser over a fresh sniff is also the more
    reproducible answer, and `CLAUDE.md` invariant 10 wants a rebuild to produce
    byte-identical output: the same bytes should be read by the same parser
    whatever the file happens to be called this time. `--parser` is the way out
    when the recorded one was the wrong one, which is the case a parser fix
    exists to correct.
    """
    if override:
        return by_parser_id(override)
    row = conn.execute(
        "SELECT parser_id FROM raw_file WHERE file_id=? AND parser_id IS NOT NULL",
        (file_id,),
    ).fetchone()
    if row:
        return by_parser_id(str(row[0]))
    return route(raw)


def _archived(conn: Any, file_id: str) -> bool:
    row = conn.execute(
        "SELECT storage_path FROM raw_file WHERE file_id = ?", (file_id,)
    ).fetchone()
    return bool(row) and Path(row[0]).exists()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--amc", help="amc_id from config/amc_manifest.yaml")
    parser.add_argument("--file", type=Path, help="a local disclosure to load instead")
    parser.add_argument(
        "--scheme", help="scheme_id (ISIN) the --file describes; required with --file"
    )
    parser.add_argument(
        "--sheet",
        help="sheet name, for a workbook holding one scheme per sheet "
             "(Kotak ships 119, Nippon 108). Without it the whole workbook "
             "is read as one portfolio",
    )
    parser.add_argument(
        "--all-sheets",
        action="store_true",
        help="load every scheme in a multi-sheet workbook, identifying each "
             "sheet against the AMFI master. Needs --amc; --scheme is then "
             "derived per sheet rather than given",
    )
    parser.add_argument(
        "--parser",
        help="force a parser_id, overriding both the one raw_file recorded "
             "for this file and the sniff. The way to re-read a file the "
             "wrong parser claimed",
    )
    args = parser.parse_args()
    for summary in run(
        args.amc, args.file, args.scheme, args.sheet, args.parser,
        args.all_sheets,
    ):
        print(" | ".join(f"{k}={v}" for k, v in summary.items()))


if __name__ == "__main__":
    main()
