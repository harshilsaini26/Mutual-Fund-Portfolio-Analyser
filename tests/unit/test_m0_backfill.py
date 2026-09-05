"""Backfill planning. DECISIONS OPEN-07.

The fetching itself is not tested — it is network I/O, and `jobs/` is the layer
where that lives. What is tested is the plan: which requests get made, in what
order, and whether the grandfathering date is inside them. Getting that wrong
silently truncates the oldest holding, which is the one whose return matters
most.
"""

from __future__ import annotations

from datetime import date

import pytest
from src.m0_data.fetch.amfi_history import (
    GRANDFATHER_DATE,
    HistoryChunk,
    plan_backfill,
    year_chunks,
)


def test_a_range_splits_on_calendar_year_boundaries() -> None:
    chunks = list(year_chunks(date(2023, 6, 1), date(2025, 3, 15), "9"))
    assert [(c.start, c.end) for c in chunks] == [
        (date(2023, 6, 1), date(2023, 12, 31)),
        (date(2024, 1, 1), date(2024, 12, 31)),
        (date(2025, 1, 1), date(2025, 3, 15)),
    ]


def test_chunks_are_calendar_aligned_so_a_rerun_repeats_them_exactly() -> None:
    """A rolling window would shift with the run date.

    Every chunk would then be a new URL, archive as a new file under a new
    hash, and the job would stop being idempotent — the property that makes it
    safe to re-run at all.
    """
    first = list(year_chunks(date(2024, 1, 1), date(2026, 1, 1), "9"))
    second = list(year_chunks(date(2024, 1, 1), date(2026, 1, 1), "9"))
    assert first == second
    assert all(c.start.month == 1 and c.start.day == 1 for c in first[1:])


def test_a_backwards_range_raises_rather_than_returning_nothing() -> None:
    """An empty result would look like "already up to date"."""
    with pytest.raises(ValueError, match="precedes"):
        list(year_chunks(date(2025, 1, 1), date(2024, 1, 1), "9"))


def test_the_grandfathering_date_is_always_inside_the_plan() -> None:
    """OPEN-07: 31-Jan-2018 is required regardless of the ledger's own history.

    MODULE_1.md §7.5 computes a pre-2018 equity lot's basis as
    `max(actual, min(FMV_31Jan2018, sale_price))`. Without that day's NAV the
    consumption is marked `confidence=low` — so one missing NAV degrades the
    tax position of every pre-2018 lot.

    The clamp is what guarantees it, rather than a special-case request that
    could be dropped in a later refactor.
    """
    chunks = plan_backfill(["9"], date(2024, 1, 1), date(2026, 9, 5))
    assert any(c.start <= GRANDFATHER_DATE <= c.end for c in chunks)
    assert chunks[0].start == GRANDFATHER_DATE


def test_an_earlier_start_is_respected_and_not_clamped_forward() -> None:
    """The clamp is a floor on coverage, not a fixed start.

    A holding from 2010 must be fetched from 2010, not from 2018.
    """
    chunks = plan_backfill(["9"], date(2010, 5, 1), date(2012, 1, 1))
    assert chunks[0].start == date(2010, 5, 1)


def test_every_amc_gets_its_own_chunks_in_a_stable_order() -> None:
    """Sorted, so two runs issue the same requests in the same sequence."""
    chunks = plan_backfill(["13", "9", "3"], date(2025, 1, 1), date(2025, 6, 1))
    codes = [c.amfi_amc_code or "" for c in chunks]
    assert codes == sorted(codes)
    assert set(codes) == {"3", "9", "13"}


def test_the_query_uses_amfis_date_format_not_iso() -> None:
    """ISO dates return the HTML error page, not an error status."""
    chunk = HistoryChunk(date(2018, 1, 31), date(2018, 12, 31), "9")
    assert chunk.params == {
        "frmdt": "31-Jan-2018", "todt": "31-Dec-2018", "mf": "9",
    }


def test_an_unfiltered_chunk_omits_the_amc_parameter() -> None:
    """OPEN-07's "all others" path. ~1 MB per day, so it is never the default."""
    chunk = HistoryChunk(date(2024, 1, 1), date(2024, 1, 1))
    assert "mf" not in chunk.params
    assert chunk.label.startswith("mf=all")
