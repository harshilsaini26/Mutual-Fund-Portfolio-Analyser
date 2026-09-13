"""What the warehouse owes, and the command that would fetch it. V1-45.

    python -m jobs.status            # offline, instant
    python -m jobs.status --check    # also ask the AMCs that can be asked

Both tiers can already answer "what is published"; nothing was asking them. The
gap this closes is not machinery, it is the moment a person has to remember it
is the 13th and wonder whether August's portfolios are out.

**Offline by default, and that is the important half.** Staleness is arithmetic
on dates the warehouse already holds: SEBI requires a monthly portfolio within
ten days of the month end, so on any given day there is a most recent month-end
whose disclosure ought to exist, and comparing it to `max(as_of_date)` needs no
network at all. `--check` adds what the AMC says it actually has, which is worth
a request only for the houses with a discovery adapter.

**Nothing here maps an AMC to an adapter.** `ingest_inbox` refused to keep such
a table -- "a second place to be wrong about something the file already says"
-- and the same answer works in reverse: `raw_file.parser_id` records which
parser read each disclosure, and that parser's own `amc_id` is the discovery
key. The provenance already knows.
"""

from __future__ import annotations

import argparse
import sys
from dataclasses import dataclass
from datetime import date
from typing import Any

import yaml
from src.common.decimals import connect
from src.m0_data.config import REPO_ROOT, source, warehouse_path
from src.m0_data.fetch.amc_direct import DISCOVERY
from src.m0_data.parse.holdings.registry import REGISTRY

#: SEBI's Mutual Funds Regulations require a scheme's monthly portfolio to be
#: published within **ten days** of the month end. That is the commonly cited
#: figure and it is used here only to decide when to start saying "overdue" --
#: it decides no number that reaches a portfolio, and a reader who finds it
#: wrong loses a nudge and nothing else. CLAUDE.md invariant 8's discipline
#: about never inventing a rate is why it is named and sourced rather than
#: being an unexplained `10` inside a comparison.
DISCLOSURE_GRACE_DAYS = 10

INDEX_YAML = REPO_ROOT / "config" / "amc_disclosure_index.yaml"
SLUGS_YAML = REPO_ROOT / "config" / "groww_slugs.yaml"

#: `holdings.kotak` -> the parser, so its `amc_id` can be asked for.
BY_PARSER_ID = {p.parser_id: p for p in REGISTRY}


@dataclass
class Standing:
    """One fund house's position: what we hold against what should exist."""

    amc_id: str
    schemes: int
    #: The OLDEST of each scheme's newest disclosure, not the newest overall.
    #:
    #: `MAX(as_of_date)` across a house is the wrong number and it reads as the
    #: right one. Kotak's August fortnightly carried 21 of its 96 schemes, so
    #: the house's max was August while 75 schemes were still on July -- and
    #: the first version of this command printed `current` for exactly that
    #: state. A staleness report whose optimism grows with the size of the
    #: fund house is worse than no report.
    have: date
    #: How many schemes are already at the expected date. `schemes - current`
    #: is what a fetch would actually fix.
    current: int
    tier: str
    #: The parser's `amc_id`, which is also the discovery key. None when the
    #: disclosure came from the coverage tier and no AMC parser was involved.
    adapter: str | None
    #: Filled by `--check`: the newest as-of the AMC itself lists.
    available: date | None = None
    error: str | None = None

    def months_behind(self, today: date) -> int:
        want = expected_as_of(today)
        return (want.year - self.have.year) * 12 + (want.month - self.have.month)

    def verdict(self, today: date) -> str:
        if self.error:
            return "unknown"
        behind = self.schemes - self.current
        if behind == 0:
            return "current"
        months = self.months_behind(today)
        gap = f"{months} month{'s' if months > 1 else ''}" if months > 0 else "partial"
        return f"{behind}/{self.schemes} behind, {gap}"


def expected_as_of(today: date) -> date:
    """The most recent month end whose disclosure is due by now.

    August's portfolio is not late on the 1st of September. It becomes due on
    the 10th, so before then the answer is July -- which is what stops this
    command reporting every fund house as behind for a third of every month.
    """
    this_month_start = today.replace(day=1)
    last_month_end = date.fromordinal(this_month_start.toordinal() - 1)
    if today.day > DISCLOSURE_GRACE_DAYS:
        return last_month_end
    prior_month_start = last_month_end.replace(day=1)
    return date.fromordinal(prior_month_start.toordinal() - 1)


def standings(conn: Any, today: date | None = None) -> list[Standing]:
    """One row per fund house, from what is actually loaded.

    `today` is an argument rather than a call to `date.today()` inside the
    loop, so a test can state the date it means. It mattered here: the first
    version read the clock, and its tests passed only on days after the
    tenth -- green on the day they were written and red for a third of every
    month afterwards.

    Grouped by AMC rather than by scheme because the AMC is the actionable
    unit: one Kotak workbook carries 109 schemes, and a list that repeated
    the same download 109 times would bury the four houses that need one.
    """
    rows = conn.execute(
        """
        SELECT s.amc_id,
               d.scheme_id,
               MAX(d.as_of_date),
               d.source_tier,
               r.parser_id
        FROM holding_disclosure d
        JOIN scheme s ON s.scheme_id = d.scheme_id
        LEFT JOIN raw_file r ON r.file_id = d.source_file_id
        WHERE d.is_current = 1
        GROUP BY s.amc_id, d.scheme_id, d.source_tier
        """
    ).fetchall()

    # Per scheme first, then folded up. Doing it in one GROUP BY would give
    # the house's newest disclosure, which is the number that made this
    # command call a 96-scheme house current on the strength of 21 of them.
    houses: dict[tuple[str, str], list[tuple[date, str | None]]] = {}
    for amc_id, _scheme_id, latest, tier, parser_id in rows:
        have = latest if isinstance(latest, date) else date.fromisoformat(str(latest))
        houses.setdefault((str(amc_id), str(tier)), []).append(
            (have, str(parser_id or ""))
        )

    want = expected_as_of(today or date.today())
    out: list[Standing] = []
    for (amc_id, tier), schemes in houses.items():
        parser_ids = {p for _, p in schemes if p}
        parser = BY_PARSER_ID.get(next(iter(parser_ids), ""))
        adapter = parser.amc_id if parser and parser.amc_id in DISCOVERY else None
        out.append(
            Standing(
                amc_id=amc_id,
                schemes=len(schemes),
                have=min(d for d, _ in schemes),
                current=sum(1 for d, _ in schemes if d >= want),
                tier=tier,
                adapter=adapter,
            )
        )
    out.sort(key=lambda s: (s.current - s.schemes, s.have, s.amc_id))
    return out


def ask_the_amcs(rows: list[Standing]) -> None:
    """Fill `available` for the houses with a discovery adapter.

    One request per ADAPTER, not per row: Kotak appears once per tier and
    twice would be two identical calls to the same endpoint.
    """
    from jobs.fetch_amc import listing

    cfg = source("S5")
    seen: dict[str, tuple[date | None, str | None]] = {}
    for row in rows:
        if row.adapter is None:
            continue
        if row.adapter not in seen:
            try:
                found = listing(row.adapter, cfg)
                newest = max(f.as_of for f in found) if found else None
                seen[row.adapter] = (newest, None)
            except Exception as exc:  # a listing that moved must not stop the report
                seen[row.adapter] = (None, f"{type(exc).__name__}: {exc}"[:70])
        row.available, row.error = seen[row.adapter]


def next_step(row: Standing, today: date) -> str:
    """The command, or the page, that would close this gap.

    A report that says a thing is stale and leaves the reader to work out what
    to do about it has done the easy half.
    """
    if row.verdict(today) == "current":
        return ""
    want = expected_as_of(today)
    period = f"{want:%Y-%m}"

    if row.adapter:
        if row.available is not None and row.available < want:
            return f"not published yet (newest {row.available})"
        return (
            f"python -m jobs.fetch_amc --amc {row.adapter} --period {period}"
            " && python -m jobs.ingest_inbox"
        )

    page = _disclosure_page(row.amc_id)
    if page:
        return f"download {period} from {page} into data/inbox/"
    return f"no discovery adapter and no page on file for {row.amc_id!r}"


def _disclosure_page(amc_id: str) -> str | None:
    """The AMC's monthly disclosure page, from AMFI's own directory (V1-32).

    Matched on the normalised house name rather than an id, because the index
    is keyed by the name AMFI prints and `scheme.amc_id` is a slug of the name
    AMFI prints -- two spellings of one thing, and neither is a key the other
    was built from.
    """
    if not INDEX_YAML.exists():
        return None
    with INDEX_YAML.open(encoding="utf-8") as fh:
        index = yaml.safe_load(fh) or {}
    wanted = amc_id.replace("_", " ").strip().lower()
    for entry in index.get("disclosures") or []:
        name = str(entry.get("amc") or "").lower()
        if wanted and wanted in name:
            return str(entry.get("monthly_portfolio") or "") or None
    return None


def report(check: bool = False, today: date | None = None) -> list[dict[str, object]]:
    today = today or date.today()
    conn = connect(str(warehouse_path()))
    rows = standings(conn, today)
    if check:
        ask_the_amcs(rows)
    return [
        {
            "amc": r.amc_id,
            "schemes": r.schemes,
            "have": str(r.have),
            "tier": r.tier,
            "expected": str(expected_as_of(today)),
            # Three distinct states, and conflating them was the first
            # version's mistake: a house we asked and got an answer from, a
            # house we asked and could not reach, and a house with no adapter
            # to ask. Only the middle one is a question mark.
            "available": (
                str(r.available)
                if r.available
                else ("unreachable" if r.error else "no adapter")
                if check and r.adapter
                else "-"
            ),
            "status": r.verdict(today),
            "next": next_step(r, today),
        }
        for r in rows
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="ask each AMC with a discovery adapter what it has published",
    )
    args = parser.parse_args(argv)

    rows = report(args.check)
    if not rows:
        print("No disclosures loaded. `python -m jobs.fetch_amc --amc kotak --list`")
        return 0

    today = date.today()
    print(f"  As of {today}, the disclosure due is {expected_as_of(today)}.\n")
    head = f"  {'fund house':<20}{'schemes':>8}  {'have':<12}{'tier':<12}{'status':<24}"
    if args.check:
        head += f"{'published':<12}"
    print(head)
    print("  " + "-" * (len(head) - 2))
    for row in rows:
        line = (
            f"  {row['amc']:<20}{row['schemes']:>8}  {row['have']:<12}"
            f"{row['tier']:<12}{row['status']:<24}"
        )
        if args.check:
            line += f"{row['available']:<12}"
        print(line)

    steps = [str(r["next"]) for r in rows if r["next"]]
    if steps:
        print("\n  To catch up:")
        for step in dict.fromkeys(steps):
            print(f"    {step}")
    else:
        print("\n  Everything is current.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
