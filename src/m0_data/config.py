"""Paths and source configuration. MODULE_0.md §13.

Environment variables are read at call time, not at import: a test that points
`MF_WAREHOUSE` at a temporary file must not depend on import order.
"""

from __future__ import annotations

import os
from bisect import bisect_right
from datetime import date
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_YAML = REPO_ROOT / "config" / "sources.yaml"
RISK_FREE_YAML = REPO_ROOT / "config" / "risk_free.yaml"
STATE_CODES_YAML = REPO_ROOT / "config" / "state_isin_codes.yaml"

#: §13.2. `/data` is gitignored in full — it holds the raw archive and both
#: warehouses, and none of it is source.
DEFAULT_DATA_ROOT = REPO_ROOT / "data"


def data_root() -> Path:
    return Path(os.environ.get("MF_DATA_ROOT", str(DEFAULT_DATA_ROOT)))


def warehouse_path() -> Path:
    """Zone A. SQLite, not DuckDB — DECISIONS V0-19."""
    env = os.environ.get("MF_WAREHOUSE")
    return Path(env) if env else data_root() / "warehouse" / "canonical.db"


def raw_root() -> Path:
    """§3.1: /data/raw/{source_id}/{yyyy}/{mm}/{sha256[:2]}/{sha256}.{ext}"""
    return data_root() / "raw"


def inbox_root() -> Path:
    """Where a human drops a disclosure they downloaded themselves.

    Fetching is automated where an AMC allows it and manual where it does not —
    Kotak serves a CAPTCHA (V1-32), and 47 of 52 AMCs have no manifest entry at
    all. `jobs/ingest_inbox.py` reads whatever is here, works out which AMC each
    file belongs to and loads every scheme in it, so the manual step is a
    download and nothing else.

    Gitignored with the rest of `data/`.
    """
    env = os.environ.get("MF_INBOX")
    return Path(env) if env else data_root() / "inbox"


def settings_path() -> Path:
    """Answers `jobs.setup` asked for once, kept with the data it fetched.

    Under `data/`, which is gitignored in full, so a contact address typed into
    the setup never reaches the repository.
    """
    return data_root() / "settings.yaml"


def settings() -> dict[str, Any]:
    path = settings_path()
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    return loaded if isinstance(loaded, dict) else {}


def save_setting(key: str, value: str) -> None:
    # Read before opening for writing: "w" empties the file, and the others
    # were lost the first time a second setting was saved.
    merged = {**settings(), key: value}
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as fh:
        yaml.safe_dump(merged, fh)


def contact_email() -> str:
    """Goes into the User-Agent. §2.3 requires an honest, contactable agent.

    The environment first, then the answer `jobs.setup` saved, so a one-command
    start asks once rather than needing a variable in every shell.
    """
    return (
        os.environ.get("MF_CONTACT_EMAIL")
        or str(settings().get("contact_email") or "")
        or "unset@example.invalid"
    )


def load_sources(path: Path = SOURCES_YAML) -> dict[str, Any]:
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh)
    if not isinstance(loaded, dict) or "sources" not in loaded:
        raise ValueError(f"{path} is not a source configuration")
    return loaded


def source(source_id: str, path: Path = SOURCES_YAML) -> dict[str, Any]:
    """One source's config, with `defaults` merged underneath it."""
    cfg = load_sources(path)
    if source_id not in cfg["sources"]:
        raise KeyError(f"no source {source_id!r} in {path}")
    merged = dict(cfg.get("defaults", {}))
    merged.update(cfg["sources"][source_id])
    agent = str(merged["user_agent"])
    if agent == "browser":
        # A host whose filter rejects anything but a browser string. The
        # contact travels in `From:` instead — DECISIONS V1-05.
        from src.m0_data.fetch.base import BROWSER_USER_AGENT

        agent = BROWSER_USER_AGENT
    merged["user_agent"] = agent.replace("{CONTACT_EMAIL}", contact_email())
    merged["from_email"] = str(merged.get("from_email", "")).replace(
        "{CONTACT_EMAIL}", contact_email()
    )
    return merged


#: The workbook formats the holdings parsers read. Here rather than in a job:
#: `jobs.fetch_amc` needs it to allow-list downloads, and importing it from
#: `jobs.ingest_inbox` dragged in `jobs.load_holdings` and openpyxl -- 1.4s to
#: import a two-element tuple, against a decision (V1-48) that load_holdings
#: does too much at import to be worth importing.
WORKBOOKS = (".xlsx", ".xls")


@lru_cache(maxsize=8)
def risk_free_rates(path: Path = RISK_FREE_YAML) -> list[tuple[date, Decimal]]:
    """Hand-entered risk-free observations, oldest first. S13.

    A list rather than a dict so `risk_free_on` can binary-search it, and
    `Decimal` because a rate feeds a ratio that money is judged by
    (invariant 1). Empty is not an error: the file may carry no observation,
    and every M2 figure except Sharpe and Sortino is unaffected by that.

    Cached because the file holds every 91-day auction since 2011 and M2 asks
    for a rate once per window per scheme. Parsing it costs 52ms, which over
    15,006 schemes and four windows is 50 minutes of re-reading one unchanged
    file. Keyed on `path`, so a test pointing at its own tmp file is unaffected
    -- but a caller that REWRITES a path it already read must call
    `risk_free_rates.cache_clear()`, as `jobs.status` does for `_index`.
    """
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    observed = loaded.get("observations") or {}
    return sorted(
        (d if isinstance(d, date) else date.fromisoformat(str(d)), Decimal(str(v)))
        for d, v in observed.items()
    )


def risk_free_on(when: date, path: Path = RISK_FREE_YAML) -> Decimal | None:
    """The rate in force on `when`: the latest observation on or before it.

    None before the first observation. A Sharpe built on a rate that was not
    yet on record is a ratio with a guessed denominator, which is worse than
    no ratio at all.
    """
    rates = risk_free_rates(path)
    i = bisect_right([d for d, _ in rates], when) - 1
    return rates[i][1] if i >= 0 else None


def state_isin_codes(path: Path = STATE_CODES_YAML) -> dict[str, str]:
    """A state government's two-digit ISIN code -> the state. V1-71.

    Raises on an entry without its evidence, or on a code that is not two
    digits: the file's whole claim is that no entry is a guess, and an entry
    that does not say where it came from cannot show that it is not.
    """
    with path.open(encoding="utf-8") as fh:
        loaded = yaml.safe_load(fh) or {}
    out: dict[str, str] = {}
    for code, entry in (loaded.get("codes") or {}).items():
        code = str(code)
        entry = entry or {}
        if not (len(code) == 2 and code.isdigit() and code != "00"):
            raise ValueError(f"{path}: {code!r} is not a state's two-digit code")
        if not entry.get("state") or not entry.get("evidence"):
            raise ValueError(f"{path}: code {code} needs a state and its evidence")
        out[code] = str(entry["state"])
    return out
