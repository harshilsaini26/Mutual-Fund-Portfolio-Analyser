"""Fetch, parse, resolve, normalise, validate and load a disclosure.

    python -m jobs.load_holdings --amc hdfc
    python -m jobs.load_holdings --file "path/to/Monthly ... .xlsx"

This is the whole L0 -> L3 path for one file, and each stage is separable on
purpose (§3): the archive is content-addressed, parsing is a pure function on
bytes, resolution reads only the entity master, and normalisation and
validation read only the parsed rows. A bug in any one of them is fixed by
re-running from the layer to its left, never by editing data.

Discovery is not automated yet — `config/amc_manifest.yaml` carries the links
(V1-03). The AMC pages are JavaScript-rendered, so finding a link needs a
browser while fetching one does not, and that fragility is kept out of here.
"""

from __future__ import annotations

import argparse
import json
import uuid
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
    archive,
    conditional_get,
)
from src.m0_data.load import load_holdings
from src.m0_data.normalise.units import to_inr
from src.m0_data.normalise.weights import normalise_weights
from src.m0_data.parse.base import RawFile
from src.m0_data.parse.holdings.registry import route
from src.m0_data.resolve.cascade import load_issuer_index, resolve
from src.m0_data.schema.apply import apply_migrations
from src.m0_data.validate.checks import (
    HoldingRow,
    as_json,
    promote_or_quarantine,
    validate_disclosure,
)

MANIFEST = REPO_ROOT / "config" / "amc_manifest.yaml"
SOURCE_PREFIX = "S5"

#: §4.6's `instrument_class`, from the SECTION the row sits under.
#:
#: The section heading is the disclosure's own statement of what a block of
#: rows is, and it is the only reliable signal on the sheet. The instrument
#: name is not: HDFC's short leg is written `Eternal Limited`, identically to
#: the long position twelve rows above it, and only the `OPTIONS` heading says
#: one is a derivative. Getting that wrong fails §10's V8 — a negative market
#: value on something the loader called equity.
_CLASS_BY_SECTION = (
    ("option", "derivative"),
    ("future", "derivative"),
    ("derivativ", "derivative"),
    ("hedg", "derivative"),
    ("treps", "cash"),
    ("repo", "cash"),
    ("cash", "cash"),
    ("net current asset", "cash"),
    ("margin", "cash"),
    ("government securit", "debt"),
    ("money market", "debt"),
    ("debt", "debt"),
    ("bond", "debt"),
    ("reit", "other"),
    ("invit", "other"),
    ("mutual fund", "mfunit"),
    ("equity", "equity"),
    ("listed", "equity"),
)

#: Fallback when the sheet carried no usable heading. A row that resolved to a
#: synthetic issuer is what that issuer says it is.
_CLASS_BY_ISSUER = {
    "__CASH__": "cash",
    "__TREPS__": "cash",
    "__RECV__": "cash",
    "__MARGIN__": "cash",
    "__DERIV__": "derivative",
    "__MFUNIT__": "mfunit",
    "__GSEC__": "debt",
}


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
        index = load_issuer_index(conn)
        for entry in _entries(amc_id, file_path, scheme_id):
            summaries.append(_one(conn, entry, cfg, index))

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
    amc_id: str | None, file_path: Path | None, scheme_id: str | None
) -> list[dict[str, Any]]:
    if file_path:
        # A disclosure names its scheme in prose, and prose is not a key — the
        # same reason every manifest entry carries `scheme_id` (MODULE_0.md
        # §4.4). Guessing it from the filename would file a portfolio against
        # whichever scheme the name most resembled, which for an AMC with four
        # plans of one fund is a coin toss.
        if not scheme_id:
            raise RuntimeError("--file needs --scheme: the ISIN this file describes")
        return [{
            "amc_id": amc_id or "unknown",
            "path": str(file_path),
            "scheme_id": scheme_id,
        }]
    manifest = load_manifest()
    rows = [
        e for e in manifest["disclosures"]
        if amc_id is None or e.get("amc_id") == amc_id
    ]
    if not rows:
        raise RuntimeError(f"no manifest entries for amc {amc_id!r}")
    return rows


def _one(
    conn: Any, entry: dict[str, Any], cfg: dict[str, Any], index: dict[str, str]
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
    parser = route(raw)
    # Nippon publishes 108 schemes as 108 sheets of one workbook, so the
    # manifest entry names the sheet. Absent, the whole workbook is read —
    # which is right for HDFC and ICICI, one scheme per file (V1-15).
    parsed = parser.parse(raw, entry.get("sheet"))
    assert parsed.as_of_date is not None  # parse_holdings raises otherwise

    scheme_id = entry["scheme_id"]
    securities = parsed.securities

    # Units first — everything after this is in rupees absolute (§7.2).
    values = [
        to_inr(s.market_value_raw, s.market_value_unit)
        if s.market_value_raw is not None else None
        for s in securities
    ]
    # §7.3 and §10's V1 both work in percent. HDFC reports `% to NAV` as one;
    # ICICI reports a fraction summing to 1.0. The parser observed which
    # (`pct_scale`) and this is where it is applied — the same boundary
    # `to_inr` sits on for market value, one column across. Skipping it makes
    # ICICI's reported weights sum to 1, failing V1 and making
    # `weight_residual` read 99: a portfolio that looks almost unaccounted for.
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
            conn, security.instrument_raw_name, security.isin_raw, None, index
        )
        rows.append({
            "isin": security.isin_raw,
            "issuer_id": str(resolution.issuer_id),
            "instrument_raw_name": security.instrument_raw_name,
            "quantity": security.quantity_raw,
            # `holding.market_value` is NOT NULL, so an unpriced row has to be
            # stored as zero. That loses the distinction between "worth
            # nothing" and "the file did not price it", so the count is carried
            # to the disclosure header below rather than left silent —
            # `CLAUDE.md` invariant 4 is that a row is never dropped without a
            # record, and a row whose exposure is zeroed is dropped in every
            # way that matters downstream.
            "market_value": value if value is not None else Decimal(0),
            "pct_to_nav": pct,
            "pct_normalised": weight,
            "instrument_class": _instrument_class(
                security.section, str(resolution.issuer_id)
            ),
            "reported_sector": security.reported_sector,
            "resolution_method": resolution.method,
            "resolution_conf": resolution.confidence,
        })

    aum = _aum_for(conn, scheme_id, parsed.as_of_date)
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
        parsed.as_of_date, date.today(), aum,
    )
    status = promote_or_quarantine(checks)
    unresolved = next(c for c in checks if c.code == "V3").observed or "0%"

    # A row the file did not price is stored with market_value 0 (the column is
    # NOT NULL) and `normalise_weights` independently gives it weight 0, so it
    # contributes nothing to any look-through while every quality figure is
    # computed against a total that already excludes it — nothing moves, and
    # nobody is told. Counting them here is what makes the loss visible.
    unpriced = [
        securities[i].instrument_raw_name
        for i, value in enumerate(values)
        if value is None
    ]

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
    conn.execute(
        "UPDATE raw_file SET parse_status='ok', parser_id=?, parser_version=?,"
        " parsed_at=?, as_of_date=? WHERE file_id=?",
        (parser.parser_id, parser.version, datetime.now(UTC), parsed.as_of_date,
         result.file_id),
    )
    conn.commit()

    failed = [c.code for c in checks if not c.passed]
    return {
        "scheme_id": scheme_id, "as_of": str(parsed.as_of_date),
        "parser": parser.parser_id, "fetch": result.status, **counts,
        "unresolved_mv_pct": unresolved, "validation_status": status,
        "failed_checks": ",".join(failed) or "none",
    }


def _instrument_class(section: str | None, issuer_id: str) -> str:
    """Section first, then the synthetic issuer, then equity. See _CLASS_BY_SECTION."""
    text = (section or "").lower()
    for needle, klass in _CLASS_BY_SECTION:
        if needle in text:
            return klass
    return _CLASS_BY_ISSUER.get(issuer_id, "equity")


def _aum_for(conn: Any, scheme_id: str, as_of: date) -> Decimal | None:
    row = conn.execute(
        "SELECT aum_inr FROM scheme_aum WHERE scheme_id=? AND as_of_date<=?"
        " ORDER BY as_of_date DESC LIMIT 1",
        (scheme_id, as_of),
    ).fetchone() if _has_table(conn, "scheme_aum") else None
    return row[0] if row else None


def _has_table(conn: Any, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )


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
    args = parser.parse_args()
    for summary in run(args.amc, args.file, args.scheme):
        print(" | ".join(f"{k}={v}" for k, v in summary.items()))


if __name__ == "__main__":
    main()
