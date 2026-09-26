"""The daily site build's workflow keeps its token to itself. DECISIONS V1-75.

`.github/workflows/site.yml` can push to the repository. What keeps that safe is
in the file, so it is asserted rather than trusted: it never runs on a pull
request (where a stranger's code would run with the token), it asks for no more
than write access to contents, and every action is pinned to a commit rather
than a tag that could be moved to other code.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "site.yml"


def _workflow() -> dict[Any, Any]:
    loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    return loaded


def test_it_never_runs_on_a_pull_request() -> None:
    # PyYAML reads the bare key `on` as the boolean True.
    workflow = _workflow()
    triggers: dict[str, Any] = workflow.get(True) or workflow.get("on") or {}
    assert set(triggers) == {"schedule", "workflow_dispatch"}


def test_it_asks_only_for_write_access_to_contents() -> None:
    assert _workflow()["permissions"] == {"contents": "write"}


def test_every_action_is_pinned_to_a_commit() -> None:
    uses = re.findall(r"uses:\s*(\S+)", WORKFLOW.read_text(encoding="utf-8"))
    assert uses, "no actions found"
    for action in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", action), action


def test_the_checkouts_keep_no_credentials() -> None:
    steps = _workflow()["jobs"]["build"]["steps"]
    checkouts = [
        s for s in steps if str(s.get("uses", "")).startswith("actions/checkout")
    ]
    assert checkouts and all(s["with"]["persist-credentials"] is False for s in checkouts)
