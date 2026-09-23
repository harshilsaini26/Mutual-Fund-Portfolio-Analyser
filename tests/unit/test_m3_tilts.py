"""Size profile: equity exposure by AMFI market-cap bucket. MODULE_3.md §12.

Hand-checkable, as `test_m3_marginal.py` is. Equity of Rs 100,000:

    ACME   60,000   large on both lists
    BETA   30,000   small on the Dec-2025 list, mid on the Jun-2026 list
    GAMMA  10,000   on neither list (a foreign listing, say)

So under the June list: large 60%, mid 30%, small 0%, unranked 10%, and 90%
of the equity is ranked. Under the December list BETA is small instead.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.common.types import ClassificationBasis, IssuerId, SchemeId, UserId
from src.m1_ledger.db import apply_ledger_schema, connect_ledger
from src.m3_lookthrough.engine import IssuerWeight, Position, compute_lookthrough
from src.m3_lookthrough.persist import save_lookthrough
from src.m3_lookthrough.providers.lookthrough import Tilt
from src.m3_lookthrough.providers.sqlite import SqliteLookThroughProvider
from src.m3_lookthrough.tilts import mcap_tilts

from tests.conftest import migrated

CURRENT, AS_OF_HOLDING = ClassificationBasis.CURRENT, ClassificationBasis.AS_OF_HOLDING
USER = UserId("USER-01")
DEC, JUN = date(2025, 12, 31), date(2026, 6, 30)
ACME, BETA, GAMMA = IssuerId("ACME"), IssuerId("BETA"), IssuerId("GAMMA")
EQUITY = {ACME: Decimal(60_000), BETA: Decimal(30_000), GAMMA: Decimal(10_000)}


def profile(tilts: list[Tilt]) -> dict[str, Decimal]:
    return {t.dimension_value: t.exposure_pct for t in tilts}


def test_every_bucket_in_reading_order_with_the_unranked_share_kept() -> None:
    tilts = mcap_tilts(EQUITY, {ACME: "large", BETA: "mid"}, CURRENT, JUN)

    assert [t.dimension_value for t in tilts] == ["large", "mid", "small", "unranked"]
    assert profile(tilts) == {
        "large": Decimal(60), "mid": Decimal(30), "small": Decimal(0),
        "unranked": Decimal(10),
    }
    assert {t.coverage_pct for t in tilts} == {Decimal(90)}
    assert {(t.mcap_basis, t.classification_basis) for t in tilts} == {(JUN, CURRENT)}


def test_no_equity_is_no_profile_rather_than_a_division_by_zero() -> None:
    assert mcap_tilts({}, {}, CURRENT, JUN) == []


@pytest.fixture
def provider(tmp_path: Path) -> SqliteLookThroughProvider:
    """Exposures stored for three dates, and two AMFI lists on record."""
    db = str(tmp_path / "warehouse.db")
    migrated(db)
    warehouse: sqlite3.Connection = connect(db)
    warehouse.executemany(
        "INSERT INTO issuer_classification (issuer_id, taxonomy, value, valid_from)"
        " VALUES (?, 'amfi_mcap', ?, ?)",
        [
            ("ACME", "large", DEC), ("BETA", "small", DEC),
            ("ACME", "large", JUN), ("BETA", "mid", JUN),
        ],
    )
    warehouse.commit()
    ledger = connect_ledger(str(tmp_path / "personal.db"), key="test-key")
    apply_ledger_schema(ledger)
    s1 = SchemeId("S1")
    weights = {
        s1: [IssuerWeight(i, v / 1000, "equity") for i, v in EQUITY.items()]
    }
    for as_of, disclosed in ((date(2025, 6, 30), date(2025, 6, 30)),
                             (date(2026, 3, 31), date(2026, 3, 31)),
                             (date(2026, 7, 31), date(2026, 3, 31))):
        result = compute_lookthrough([Position(s1, Decimal(100_000))], weights, as_of)
        save_lookthrough(ledger, USER, as_of, result, {s1: disclosed})
    return SqliteLookThroughProvider(ledger, warehouse)


def test_the_list_in_force_on_the_date_not_the_newest(
    provider: SqliteLookThroughProvider,
) -> None:
    """Invariant 6. In March the June list does not exist yet: BETA is small."""
    march = provider.tilts(USER, date(2026, 3, 31), "mcap", CURRENT)
    assert {t.mcap_basis for t in march} == {DEC}
    assert profile(march)["small"] == Decimal(30)

    july = provider.tilts(USER, date(2026, 7, 31), "mcap", CURRENT)
    assert {t.mcap_basis for t in july} == {JUN}
    assert profile(july)["mid"] == Decimal(30)


def test_as_of_holding_classifies_at_the_disclosure_date(
    provider: SqliteLookThroughProvider,
) -> None:
    """July's exposures come from a March disclosure, so AS_OF_HOLDING reads
    the list in force in March. The basis changes the answer; that is why it
    is a required argument."""
    tilts = provider.tilts(USER, date(2026, 7, 31), "mcap", AS_OF_HOLDING)
    assert {t.mcap_basis for t in tilts} == {DEC}
    assert profile(tilts)["small"] == Decimal(30)


def test_no_list_in_force_is_an_error_not_an_unranked_portfolio(
    provider: SqliteLookThroughProvider,
) -> None:
    """June 2025 predates both lists. Every company "unranked" would be a
    confident wrong answer about a portfolio nobody classified."""
    with pytest.raises(LookupError, match="no AMFI market-cap list"):
        provider.tilts(USER, date(2025, 6, 30), "mcap", CURRENT)


def test_other_dimensions_still_say_what_they_wait_on(
    provider: SqliteLookThroughProvider,
) -> None:
    with pytest.raises(NotImplementedError, match="taxonomy"):
        provider.tilts(USER, date(2026, 7, 31), "sector", CURRENT)
