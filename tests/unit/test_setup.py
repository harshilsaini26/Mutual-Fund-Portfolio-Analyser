"""One command from a clone: `jobs/setup.py`, `config.contact_email`, and
`jobs.serve` with no ledger yet. DECISIONS V1-72.

The jobs themselves are tested where they live; what is tested here is the
order they run in, that an interrupted first run resumes rather than restarts,
that a start after that is only today's prices, and that a new user with no
statement imported still gets a working portal.
"""

from __future__ import annotations

import sys
from collections.abc import Callable, Sequence
from datetime import date
from pathlib import Path

import jobs.setup as setup
import pytest
from jobs.serve import open_ledger
from src.m0_data.config import contact_email, save_setting

DAY = date(2026, 9, 24)


@pytest.fixture(autouse=True)
def data_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MF_DATA_ROOT", str(tmp_path))
    monkeypatch.delenv("MF_CONTACT_EMAIL", raising=False)
    return tmp_path


def _runner(
    fail: frozenset[str] = frozenset(),
) -> tuple[list[str], Callable[[Sequence[str]], int]]:
    ran: list[str] = []

    def run(job: Sequence[str]) -> int:
        key = next(s.key for s in setup.STEPS if s.job == tuple(job))
        ran.append(key)
        return 1 if key in fail else 0

    return ran, run


def test_the_first_run_is_every_step_in_order() -> None:
    ran, run = _runner()
    setup.run(run, today=DAY)
    assert ran == [s.key for s in setup.STEPS]


def test_a_failed_step_does_not_stop_the_rest_and_is_tried_again() -> None:
    ran, run = _runner(fail=frozenset({"kotak"}))
    summary = setup.run(run, today=DAY)
    assert summary["failed"] == ["kotak"]
    assert ran[-1] == "index_levels"  # carried on past it

    ran, run = _runner()
    setup.run(run, today=DAY)
    assert ran == ["kotak"]  # only what did not finish; prices already ran today


def test_after_the_first_run_a_start_is_only_todays_prices() -> None:
    _, run = _runner()
    setup.run(run, today=DAY)
    ran, run = _runner()
    setup.run(run, today=DAY)
    assert ran == []  # already refreshed today
    setup.run(run, today=date(2026, 9, 25))
    assert ran == list(setup.DAILY)


def test_a_first_run_resumed_tomorrow_still_refreshes_tomorrows_prices() -> None:
    _, run = _runner(fail=frozenset({"index_levels"}))
    setup.run(run, today=DAY)
    ran, run = _runner()
    setup.run(run, today=date(2026, 9, 25))
    # The daily steps (the expense ratios' is a no-op once a month is loaded),
    # then what did not finish.
    assert ran == ["prices", "expense_ratios", "index_levels"]


def test_the_contact_email_is_asked_once_and_kept_under_data(
    data_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert contact_email() == "unset@example.invalid"
    answers = iter(["not an address", "me@example.org"])
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    assert setup.ensure_contact_email(lambda _prompt: next(answers)) == "me@example.org"
    assert (data_root / "settings.yaml").exists()
    assert contact_email() == "me@example.org"
    assert setup.ensure_contact_email(lambda _prompt: "never asked") is None
    monkeypatch.setenv("MF_CONTACT_EMAIL", "env@example.org")
    assert contact_email() == "env@example.org"  # the environment still wins


def test_a_saved_setting_keeps_the_others(data_root: Path) -> None:
    save_setting("contact_email", "a@example.org")
    save_setting("other", "x")
    assert contact_email() == "a@example.org"


def test_no_ledger_yet_serves_an_empty_one_without_asking_for_a_key(
    tmp_path: Path,
) -> None:
    """A new user with no statement imported gets the fund pages, not an
    error -- and is not asked for a key to a ledger that does not exist."""

    def never() -> str:
        raise AssertionError("asked for a key with no ledger")

    ledger, has_portfolio = open_ledger(tmp_path / "missing.db", ask_key=never)
    assert has_portfolio is False
    assert ledger.execute("SELECT count(*) FROM position").fetchone() == (0,)
    assert not (tmp_path / "missing.db").exists()
