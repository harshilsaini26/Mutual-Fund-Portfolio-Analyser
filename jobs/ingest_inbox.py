"""Load every disclosure a human dropped in `data/inbox/`. DECISIONS V1-39.

    python -m jobs.ingest_inbox

Drop an AMC's monthly portfolio workbook in the folder and run this: it works
out which house published it, which scheme each sheet describes, and loads all
of them.

**Why the manual step exists.** AMFI publishes a directory of every AMC's
disclosure page but hosts none of the files, and each AMC renders its file list
its own way (V1-32). `jobs/fetch_amc.py` closes that for the houses with a
backend listing (V1-44); for the rest a person opens a page and clicks a link.

Nothing here is destructive. The archive is content-addressed, so re-running
over the same folder re-derives rather than duplicating: a file already loaded
by the same reader and cascade reports `skipped`, and one loaded by an older
version produces a new revision (V1-29, V1-36).
"""

from __future__ import annotations

import argparse
import sys
import zipfile
from pathlib import Path, PureWindowsPath
from typing import Any

from src.common.decimals import connect
from src.m0_data.config import WORKBOOKS, inbox_root, source, warehouse_path
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


#: The largest a workbook may unpack to. `zf.read` holds the whole member in
#: memory, so without this a few kilobytes of ZIP can unpack to gigabytes. The
#: largest disclosure seen is 3.8 MB; this is generous on purpose.
MAX_MEMBER_BYTES = 100_000_000


def _expand_zips(folder: Path) -> int:
    """Flatten any ZIP in the inbox into loose workbooks beside it.

    ICICI publishes its monthly disclosure as a ZIP of per-scheme workbooks,
    and `jobs.status` prints `fetch_amc --amc icici && ingest_inbox` as the
    catch-up command -- but the scan below only reads `.xlsx`/`.xls`, so the
    archive landed in the inbox and was silently skipped. The only ICICI
    disclosure in this warehouse got there through a `file://` URL: somebody
    unzipped it by hand.

    Member names are reduced to a basename before they become a path. A ZIP
    entry is attacker-controlled the moment the publisher is (zip-slip), and
    this archive comes off the same listing whose `fileName` could already
    choose where bytes landed. PureWindowsPath splits on `\\` on every
    platform; Path does not on Linux.

    An existing file is never overwritten -- re-running must not clobber a
    workbook someone edited or placed themselves.
    """
    extracted = 0
    # iterdir + suffix.lower(), not glob("*.zip"): glob is case-sensitive on
    # Linux and not on Windows, so a publisher's `.ZIP` expanded locally and
    # was silently skipped in CI. The scan below already matches this way.
    archives = sorted(
        p for p in folder.iterdir() if p.is_file() and p.suffix.lower() == ".zip"
    )
    for archive in archives:
        # The whole body, not just the open: a ZIP whose directory parses but
        # whose member data is truncated -- what a half-finished download
        # leaves -- opens fine and raises from `read`. A bad archive must cost
        # itself, not the batch, as every other failure here does.
        #
        # `zipfile` signals four different ways for four kinds of unreadable,
        # and only one of them is BadZipFile. Catching that alone let an
        # encrypted member abort the whole run before any workbook loaded --
        # the exact failure this guard exists to prevent, wearing a different
        # exception type:
        #
        #   BadZipFile          corrupt directory, or a member that fails CRC
        #   RuntimeError        member is encrypted and no password was given
        #   NotImplementedError a compression method zipfile cannot read
        #   ValueError          a member name with an embedded NUL
        #
        # OSError is deliberately NOT caught: a write that fails is the disk
        # or the permissions talking, not this archive, and the next archive
        # would fail the same way. That one should stop the run.
        try:
            with zipfile.ZipFile(archive) as zf:
                for member in zf.infolist():
                    name = PureWindowsPath(member.filename).name
                    if not name.lower().endswith(WORKBOOKS) or name.startswith("~$"):
                        continue
                    if member.file_size > MAX_MEMBER_BYTES:
                        print(
                            f"    SKIPPED {archive.name}: {name} unpacks to "
                            f"{member.file_size:,} bytes, over {MAX_MEMBER_BYTES:,}"
                        )
                        continue
                    target = folder / name
                    if not target.exists():
                        target.write_bytes(zf.read(member))
                        extracted += 1
        except (
            zipfile.BadZipFile,
            RuntimeError,
            NotImplementedError,
            ValueError,
        ) as exc:
            # The reason, not just the refusal: "encrypted" and "corrupt" want
            # different things from whoever is reading the output.
            print(f"    SKIPPED {archive.name}: {type(exc).__name__}: {str(exc)[:70]}")
    return extracted


def run(inbox: Path | None = None, amc_id: str | None = None) -> int:
    """Returns the number of schemes loaded."""
    folder = inbox or inbox_root()
    if not folder.exists():
        print(f"no inbox at {folder} — create it and drop a workbook in")
        return 0

    expanded = _expand_zips(folder)
    if expanded:
        print(f"expanded {expanded} workbook(s) out of ZIP archives")

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
            # carry on. A second line of defence only: an unknown basis is a
            # property of the WAREHOUSE, not of any one sheet, so it arrives
            # here once per sheet -- 119 times for one workbook, each reported
            # as though that sheet were at fault, and `main` exits 0 having
            # loaded nothing. `migrations/013_scheme_aum_basis.sql` puts the
            # vocabulary on the column, where a value V2 cannot read cannot be
            # stored in the first place.
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
