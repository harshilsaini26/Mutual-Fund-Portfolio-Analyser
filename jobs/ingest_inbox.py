"""Load every disclosure a human dropped in `data/inbox/`. DECISIONS V1-39.

    python -m jobs.ingest_inbox

Drop an AMC's monthly portfolio workbook in the folder and run this. It works
out which fund house published it, which scheme each sheet describes, and loads
all of them — so the manual step is a download and nothing else.

**Why the manual step exists at all.** V1-32 got discovery to the *page*, not
the file: AMFI publishes a directory of every AMC's disclosure page (52 of them,
`config/amc_disclosure_index.yaml`) but hosts none of the files, and each AMC
renders its own file list its own way. Kotak answers a portfolio request with a
Radware CAPTCHA, which this project does not solve. So for most houses a person
opens a page and clicks a link; everything after that is automatic.

Nothing here is destructive and nothing has to be tidied up. The archive is
content-addressed, so re-running over the same folder re-derives rather than
duplicating: a file already loaded by the same reader and cascade reports
`skipped`, and one loaded by an older version produces a new revision (V1-29,
V1-36). Files are left where they are.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any

from src.common.decimals import connect
from src.m0_data.config import inbox_root, source, warehouse_path
from src.m0_data.parse.base import ParseFailed, RawFile
from src.m0_data.parse.holdings.registry import NoParserMatched
from src.m0_data.resolve.cascade import load_isin_prefix_index, load_issuer_index
from src.m0_data.resolve.scheme_match import (
    amc_names,
    detect_amc,
    live_families_by_amc,
)
from src.m0_data.validate.checks import UnknownAumBasis

from jobs.load_holdings import _one, _parser_for, discover_sheets

#: Extensions an AMC publishes a portfolio under. `.xls` is here because
#: Nippon serves a ZIP-format workbook under it and the extension lies (V1-15).
WORKBOOKS = (".xlsx", ".xls")


def _amc_for(conn: Any, path: Path) -> tuple[str | None, dict[str, int], str]:
    """Which fund house published this workbook, read off its own sheets.

    A file is parsed once here and once again when it is loaded. That is
    deliberate: knowing the AMC needs the sheet headers, and the alternative —
    a table mapping each parser's `amc_id` to the scheme master's — is a second
    place to be wrong about something the file already says.
    """
    import hashlib
    import io

    import openpyxl

    content = path.read_bytes()
    file_id = hashlib.sha256(content).hexdigest()
    raw = RawFile(file_id, "S5:inbox", path.name, content)
    try:
        parser = _parser_for(conn, file_id, raw, None)
    except NoParserMatched as exc:
        return None, {}, str(exc)[:90]

    workbook = openpyxl.load_workbook(io.BytesIO(content), read_only=True)
    names = list(workbook.sheetnames)
    workbook.close()

    headers: list[list[str]] = []
    as_of = None
    for name in names:
        try:
            parsed = parser.parse(raw, name)
        except ParseFailed:
            continue
        headers.append(parsed.header_candidates)
        as_of = as_of or parsed.as_of_date
    if as_of is None:
        return None, {}, "no sheet in this file parses as a portfolio"

    amc_id, tally = detect_amc(
        headers, live_families_by_amc(conn, as_of), amc_names(conn)
    )
    if amc_id is None:
        shape = ", ".join(f"{k}={v}" for k, v in sorted(tally.items())[:3])
        return None, tally, f"no AMC accounts for a majority of sheets ({shape})"
    return amc_id, tally, ""


def run(inbox: Path | None = None, amc_id: str | None = None) -> int:
    """Returns the number of schemes loaded."""
    folder = inbox or inbox_root()
    if not folder.exists():
        print(f"no inbox at {folder} — create it and drop a workbook in")
        return 0

    files = sorted(
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in WORKBOOKS and not p.name.startswith("~$")
    )
    if not files:
        print(f"{folder}: nothing to load")
        return 0

    cfg = source("S5")
    conn = connect(str(warehouse_path()))
    index = load_issuer_index(conn)
    prefixes = load_isin_prefix_index(conn)
    loaded_total = 0

    for path in files:
        print(f"\n=== {path.name} ===")
        detected, tally, why = (amc_id, {}, "") if amc_id else _amc_for(conn, path)
        if detected is None:
            print(f"    SKIPPED: {why}")
            continue
        if not amc_id:
            print(f"    amc: {detected} ({tally.get(detected, 0)} sheets name its funds)")

        entries, refusals = discover_sheets(conn, path, detected)
        loaded = 0
        for entry in entries:
            try:
                summary = _one(conn, entry, cfg, index, prefixes)
            # `UnknownAumBasis` is a ValueError, not a ParseFailed, and
            # without it here one corrupt `scheme_aum.basis` aborted the
            # whole batch -- 119 schemes from one workbook, where this
            # module's contract is to report a sheet it cannot handle and
            # carry on.
            except (ParseFailed, RuntimeError, UnknownAumBasis) as exc:
                refusals.append((str(entry["sheet"]), "load failed", str(exc)[:70]))
                continue
            conn.commit()
            loaded += 1
            if summary.get("skipped"):
                continue
        loaded_total += loaded
        print(f"    loaded {loaded} schemes, refused {len(refusals)} sheets")
        for sheet, reason, detail in refusals[:6]:
            print(f"      - {sheet:12} {reason:18} {detail[:52]}")
        if len(refusals) > 6:
            print(f"      ... and {len(refusals) - 6} more")

    conn.close()
    print(f"\n{loaded_total} schemes loaded from {len(files)} file(s)")
    return loaded_total


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--inbox", type=Path, help=f"default: {inbox_root()}"
    )
    parser.add_argument(
        "--amc",
        help="skip AMC detection and use this amc_id for every file. The way "
             "in when a workbook names too few of its own funds to be sure",
    )
    args = parser.parse_args()
    sys.exit(0 if run(args.inbox, args.amc) >= 0 else 1)


if __name__ == "__main__":
    main()
