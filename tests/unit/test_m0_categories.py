"""Which of AMFI's category names are the same thing. `src/m0_data/categories.py`.

DECISIONS V1-76: merge only what is certain. These pin both halves -- the old
and new heading of one SEBI category land in one group, and a debt name whose
meaning changed does not -- and that every category AMFI uses today is mapped.
"""

from __future__ import annotations

import sqlite3
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import yaml
from src.m0_data.categories import CATEGORIES_YAML, FAMILIES, category_of

#: AMFI writes a typographic apostrophe in "Children's" (U+2019).
RSQUO = chr(0x2019)
WAREHOUSE = Path(__file__).resolve().parents[2] / "data" / "warehouse" / "canonical.db"


def _entries() -> list[dict[str, Any]]:
    loaded: dict[str, Any] = yaml.safe_load(CATEGORIES_YAML.read_text(encoding="utf-8"))
    return list(loaded["categories"])


def test_the_file_lists_each_amfi_name_once_under_unique_keys() -> None:
    entries = _entries()
    assert len({e["key"] for e in entries}) == len(entries)
    names = Counter(" ".join(n.split()) for e in entries for n in e["merges"])
    assert [n for n, c in names.items() if c > 1] == []
    families = {key for key, _, _ in FAMILIES}
    assert {e["family"] for e in entries} <= families


def test_every_group_kept_apart_from_a_similar_one_says_why() -> None:
    for key in ("debt/short_duration", "equity/sectoral_thematic", "index/undivided",
                "debt/income_legacy"):
        entry = next(e for e in _entries() if e["key"] == key)
        assert entry.get("note"), key


@pytest.mark.parametrize(("old", "new"), [
    ("Equity Scheme - Flexi Cap Fund", "Equity Schemes - Flexi Cap Fund"),
    ("Equity Scheme - ELSS", "ELSS"),
    ("Hybrid Scheme - Dynamic Asset Allocation or Balanced Advantage",
     "Hybrid Schemes - Balanced Advantage Fund/ Dynamic Asset Allocation"),
    ("Debt Scheme - Liquid Fund", "Income/Debt Oriented Schemes - Liquid Fund"),
    ("Other Scheme - Gold ETF", "Exchange Traded Funds (ETFs) - Gold ETF"),
    (f"Solution Oriented Scheme - Children{RSQUO}s Fund",
     f"Children{RSQUO}s Fund - Childrens' Fund"),
])
def test_one_sebi_category_under_two_headings_is_one_group(old: str, new: str) -> None:
    assert category_of(old).key == category_of(new).key


@pytest.mark.parametrize(("older", "newer"), [
    ("Debt Scheme - Short Duration Fund",
     "Income/Debt Oriented Schemes - Short Term Fund"),
    ("Debt Scheme - Dynamic Bond", "Income/Debt Oriented Schemes - Dynamic Term Fund"),
    ("Equity Scheme - Sectoral/ Thematic", "Equity Schemes - Thematic Fund"),
    ("Other Scheme - Index Funds", "Index Funds - Equity Funds"),
])
def test_a_name_whose_meaning_changed_is_not_merged(older: str, newer: str) -> None:
    assert category_of(older).key != category_of(newer).key
    assert category_of(older).note


def test_repeated_spaces_do_not_make_a_new_category() -> None:
    assert category_of("Other Scheme - Other  ETFs").key == "etf/undivided"


def test_a_name_never_seen_is_its_own_group_and_says_so() -> None:
    found = category_of("Equity Schemes - Quantum Fund")
    assert (found.mapped, found.family, found.name) == (
        False, "equity", "Equity Schemes - Quantum Fund"
    )
    assert found.key.startswith("unmapped/") and found.note


@pytest.mark.skipif(not WAREHOUSE.exists(), reason="needs this machine's warehouse")
def test_every_category_amfi_uses_today_is_mapped() -> None:
    conn = sqlite3.connect(f"file:{WAREHOUSE}?mode=ro", uri=True)
    try:
        latest = conn.execute("SELECT max(last_seen) FROM scheme").fetchone()[0]
        names = [row[0] for row in conn.execute(
            "SELECT DISTINCT sebi_category FROM scheme WHERE plan = 'direct'"
            " AND last_seen >= date(?, '-7 days')", (latest,),
        )]
    finally:
        conn.close()
    unmapped = sorted(n for n in names if not category_of(n).mapped)
    assert unmapped == [], unmapped
