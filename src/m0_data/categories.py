"""Canonical fund categories: which of AMFI's category names are the same thing.

DECISIONS V1-76. AMFI is part-way through renaming its categories, so one SEBI
category can appear under two headings ("Equity Scheme - Flexi Cap Fund",
"Equity Schemes - Flexi Cap Fund") and a peer group built on the raw string
would split it. `config/categories.yaml` records which names are merged and
why, and which are kept apart because merging them would be a judgement.

A name the file does not list is not guessed at: it becomes a group of its own,
named as AMFI writes it, placed in a family by the start of the name, and
flagged `mapped=False` so a page can say so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from src.m0_data.config import REPO_ROOT

CATEGORIES_YAML = REPO_ROOT / "config" / "categories.yaml"

#: The front page's tiles, in reading order: key, name, what is in it.
FAMILIES = (
    ("equity", "Equity", "Shares of listed companies"),
    ("debt", "Debt", "Bonds, government securities and money-market paper"),
    ("hybrid", "Hybrid", "Shares and bonds together"),
    ("other", "Index funds, ETFs and more",
     "Index funds, exchange-traded funds and funds of funds"),
    ("solution", "Solution oriented", "Retirement and children's funds"),
)
#: For a name the file does not list: how its heading begins, per family.
_FAMILY_PREFIXES = (
    ("equity", ("equity", "growth", "elss")),
    ("debt", ("debt", "income", "money market", "gilt", "liquid")),
    ("hybrid", ("hybrid", "balanced")),
    ("solution", ("solution", "children", "life cycle", "retirement")),
)


@dataclass(frozen=True)
class Category:
    key: str
    name: str
    family: str
    #: Listed in `config/categories.yaml`; False for a name shown as AMFI wrote it.
    mapped: bool = True
    #: Why this group is kept apart from a similar one, when it is.
    note: str | None = None
    #: What SEBI's rules say it holds, in a line (the front page's map, V1-81).
    about: str | None = None


def normalise(name: str) -> str:
    """AMFI's name with runs of spaces collapsed ("Other  ETFs")."""
    return " ".join(name.split())


@lru_cache(maxsize=4)
def _table(path: Path = CATEGORIES_YAML) -> dict[str, Category]:
    loaded: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    table: dict[str, Category] = {}
    for entry in loaded["categories"]:
        category = Category(
            key=str(entry["key"]),
            name=str(entry["name"]),
            family=str(entry["family"]),
            note=" ".join(str(entry["note"]).split()) if entry.get("note") else None,
            about=entry.get("about"),
        )
        for amfi_name in entry["merges"]:
            table[normalise(str(amfi_name))] = category
    return table


def family_of(name: str) -> str:
    """The family a heading belongs to, read from how it begins."""
    head = name.partition(" - ")[0].strip().lower()
    return next(
        (key for key, starts in _FAMILY_PREFIXES if head.startswith(starts)), "other"
    )


def category_of(sebi_category: str | None) -> Category:
    """The canonical category for AMFI's category name."""
    name = normalise(sebi_category or "Other")
    found = _table().get(name)
    if found is not None:
        return found
    slug = re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_") or "other"
    return Category(
        key=f"unmapped/{slug}",
        name=name,
        family=family_of(name),
        mapped=False,
        note="A category name AMFI has not used before; shown as AMFI writes it.",
    )


__all__ = ["CATEGORIES_YAML", "FAMILIES", "Category", "category_of", "family_of",
           "normalise"]
