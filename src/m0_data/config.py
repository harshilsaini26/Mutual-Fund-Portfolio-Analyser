"""Paths and source configuration. MODULE_0.md §13.

Environment variables are read at call time, not at import: a test that points
`MF_WAREHOUSE` at a temporary file must not depend on import order.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
SOURCES_YAML = REPO_ROOT / "config" / "sources.yaml"

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


def contact_email() -> str:
    """Goes into the User-Agent. §2.3 requires an honest, contactable agent."""
    return os.environ.get("MF_CONTACT_EMAIL", "unset@example.invalid")


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
    merged["user_agent"] = str(merged["user_agent"]).replace(
        "{CONTACT_EMAIL}", contact_email()
    )
    return merged
