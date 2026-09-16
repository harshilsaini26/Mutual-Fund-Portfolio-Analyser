"""Expanding a publisher's ZIP into the inbox.

ICICI publishes its monthly disclosure as a ZIP of per-scheme workbooks, and
the inbox scan only reads `.xlsx`/`.xls` — so the archive landed and was
silently skipped, while `jobs.status` went on printing
`fetch_amc --amc icici && ingest_inbox` as the command that would fix ICICI.

A ZIP entry is attacker-controlled the moment the publisher is, so the
zip-slip cases here are the same trust boundary as the publisher filename in
`test_security.py` — the archive arrives off that same listing.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

from jobs.ingest_inbox import _expand_zips


def _zip(path: Path, members: dict[str, bytes]) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name, body in members.items():
            zf.writestr(name, body)


def test_workbooks_come_out_of_the_archive(tmp_path: Path) -> None:
    _zip(tmp_path / "monthly.zip", {"FundA.xlsx": b"a", "FundB.xlsx": b"b"})

    assert _expand_zips(tmp_path) == 2
    assert (tmp_path / "FundA.xlsx").read_bytes() == b"a"
    assert (tmp_path / "FundB.xlsx").read_bytes() == b"b"


def test_nested_members_are_flattened(tmp_path: Path) -> None:
    """The inbox scan is `iterdir`, not `rglob`, so a member left in a
    subdirectory would extract successfully and still never be read."""
    _zip(tmp_path / "m.zip", {"2026/Aug/Fund.xlsx": b"x"})

    assert _expand_zips(tmp_path) == 1
    assert (tmp_path / "Fund.xlsx").read_bytes() == b"x"


def test_a_traversing_member_cannot_escape_the_inbox(tmp_path: Path) -> None:
    """Zip-slip. `write_bytes` on an unsanitised member name puts publisher
    bytes at a publisher-chosen path, which is the bug already fixed one layer
    up in `jobs/fetch_amc.py` — the archive comes off the same listing."""
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    _zip(
        inbox / "m.zip",
        {
            "../../escaped.xlsx": b"nope",
            "..\\..\\escaped2.xlsx": b"nope",
            "/tmp/abs.xlsx": b"nope",
        },
    )

    _expand_zips(inbox)

    escaped = [p for p in tmp_path.rglob("*.xlsx") if p.parent != inbox]
    assert not escaped, f"wrote outside the inbox: {escaped}"
    assert (inbox / "escaped.xlsx").exists(), "traversal stripped, name kept"


def test_non_workbook_members_are_left_alone(tmp_path: Path) -> None:
    """An allow-list, so a ZIP cannot drop a `.bat` into a folder the user
    runs jobs from."""
    _zip(tmp_path / "m.zip", {"readme.txt": b"x", "run.bat": b"x", "ok.xlsx": b"x"})

    assert _expand_zips(tmp_path) == 1
    assert not (tmp_path / "run.bat").exists()
    assert not (tmp_path / "readme.txt").exists()


def test_an_existing_workbook_is_never_overwritten(tmp_path: Path) -> None:
    """Re-running ingest must not clobber a file someone edited or placed."""
    (tmp_path / "Fund.xlsx").write_bytes(b"mine")
    _zip(tmp_path / "m.zip", {"Fund.xlsx": b"theirs"})

    assert _expand_zips(tmp_path) == 0
    assert (tmp_path / "Fund.xlsx").read_bytes() == b"mine"


def test_no_zip_is_not_an_error(tmp_path: Path) -> None:
    assert _expand_zips(tmp_path) == 0
