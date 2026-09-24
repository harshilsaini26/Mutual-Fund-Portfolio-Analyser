"""The one test that runs against a real CAS. `MODULE_1.md` §5.2, §5.4.

`src/m1_ledger/cas/pdf.py` is the only module in this project that touches a
password or a PDF, and it has been uncovered since it was written. It cannot be
covered by the synthetic statement the rest of the CAS tests use: that fixture
is `list[str]` by construction, which is exactly what makes `parse.py` testable
and exactly why it says nothing about decryption or layout extraction.

**What this establishes that nothing else does.** Every other CAS test reads
lines this repository generated. `scripts/build_v0_cas.py` renders the golden
portfolio into a statement, `parse.py` reads it back, and the round trip proves
the parser inverts the generator — it does not prove the generator resembles
what CAMS and KFintech actually print. Only a real statement does that, and the
two places it can disagree are the two that matter: `pdfplumber`'s
`layout=True` extraction, where a NAV and a unit count are distinguished by
column position alone, and the transaction vocabulary in `config/txn_types.yaml`.

**Zone B.** A real CAS carries a PAN, folio numbers and a postal address.
`tests/fixtures/local/` is gitignored in full; the file must never reach the
repository, and `PLAN.md` §6.3 puts it under the strictest handling in the
project. The test is skipped, not failed, when it is absent — a machine without
the fixture is the normal case, including CI.

**The password.** §5.4 says "from user input at import time. Never stored." A
test cannot prompt, so it reads `MF_CAS_PASSWORD` from the environment and
skips when unset. That is a departure from the letter of §5.4 and it is
deliberate: an environment variable is not written to disk by this code, is not
committed, and is scoped to the process. It should not become the import
path's mechanism — `jobs/` must still prompt.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.common.types import Isin, SchemeId, UserId
from src.m0_data.config import warehouse_path
from src.m0_data.providers.warehouse import WarehouseMarketDataProvider
from src.m1_ledger.cas import StagedTxn, import_cas
from src.m1_ledger.cas.pdf import decrypt_and_extract, file_id
from src.m1_ledger.lots import build_book

from tests.helpers import closing_balance

REAL_CAS = Path(__file__).resolve().parents[1] / "fixtures" / "local" / "cas_real.pdf"
PASSWORD_ENV = "MF_CAS_PASSWORD"
USER = UserId("USER-01")

pytestmark = pytest.mark.skipif(
    not REAL_CAS.exists(), reason="local-only Zone B fixture; see the module docstring"
)


def _password() -> str:
    password = os.environ.get(PASSWORD_ENV)
    if not password:
        pytest.skip(f"{PASSWORD_ENV} is not set")
    return password


@pytest.fixture(scope="module")
def lines() -> list[str]:
    return decrypt_and_extract(REAL_CAS.read_bytes(), _password())


#: What `import_cas` needs to turn a staged row into a scheme.
Resolver = Callable[[StagedTxn], SchemeId | None]


@pytest.fixture(scope="module")
def resolve() -> Resolver:
    """Resolve against the real warehouse, not the fake.

    A real statement names schemes the fixture provider has never heard of, so
    the fake would report every row unmatched and prove nothing. The warehouse
    holds AMFI's full scheme master.
    """
    db = warehouse_path()
    if not db.exists():
        pytest.skip("no warehouse; run jobs.fetch_nav first")
    provider = WarehouseMarketDataProvider(connect(str(db)))

    def _resolve(s: StagedTxn) -> SchemeId | None:
        ref = provider.resolve_scheme(
            Isin(s.scheme_raw_isin) if s.scheme_raw_isin else None,
            s.scheme_raw_name,
            None,
            s.txn_date,
        )
        return ref.scheme_id

    return _resolve


def test_the_pdf_decrypts_and_yields_text(lines: list[str]) -> None:
    """§5.2. The layer no synthetic fixture can exercise.

    `file_id` is asserted here too because it is the same bytes-in, hash-out
    path the archive relies on, and a real PDF is the only input that has ever
    been through it.
    """
    assert lines, "decryption produced no text at all"
    assert any("Folio" in ln or "folio" in ln for ln in lines)
    assert len(file_id(REAL_CAS.read_bytes())) == 64


def test_the_real_statement_parses_without_losing_a_line(
    lines: list[str], resolve: Resolver
) -> None:
    """§5.3's silent-drop failure, on a statement we did not write.

    `unparsed_lines > 0` is blocking (V0-15): a line the regexes never
    recognised is a transaction that silently is not in the ledger, and the
    ledger still reconciles against nothing. On a real statement this is the
    first honest measure of the parser's vocabulary — the synthetic fixture
    only ever contains rows `build_v0_cas.py` knew how to write.
    """
    report = import_cas(USER, lines, resolve)
    assert report.ctx.unparsed == [], (
        f"{len(report.ctx.unparsed)} lines inside a scheme block were not "
        f"consumed; first: {report.ctx.unparsed[:3]}"
    )
    assert report.unmatched == 0, f"{report.unmatched} rows resolved to no scheme"
    assert report.status == "ok", f"status={report.status} flags={report.flags}"


def test_every_folio_on_the_real_statement_reconciles(
    lines: list[str], resolve: Resolver
) -> None:
    """The V0 gate, on real data. `PLAN.md` §7: every folio to 0.001 units.

    The statement prints the position twice — a running balance on each
    transaction line and an explicit closing balance per scheme block — and the
    book replayed from the transactions is a third figure. All three must tie.
    This is the same check the golden statement gets, with one difference that
    matters: nothing here was generated by this repository, so agreement is
    evidence rather than a round trip.
    """
    report = import_cas(USER, lines, resolve)
    book = build_book(report.txns)

    checked = 0
    for marker in report.ctx.balances:
        if marker.kind != "closing":
            continue
        printed = closing_balance(report.ctx, marker.folio, marker.scheme_raw_isin)
        if printed is None:
            continue
        computed = sum(
            (
                lot.units_remaining
                for lot in book.all_lots()
                if lot.folio == marker.folio
                and str(lot.scheme_id) == marker.scheme_raw_isin
            ),
            Decimal(0),
        )
        assert abs(printed - computed) <= Decimal("0.001"), (
            f"folio {marker.folio} scheme {marker.scheme_raw_isin}: "
            f"statement says {printed}, ledger computes {computed}"
        )
        checked += 1

    assert checked, "the statement printed no closing balances to reconcile against"
