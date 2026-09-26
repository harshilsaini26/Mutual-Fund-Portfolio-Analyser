"""Everything a fresh clone needs, in one command. DECISIONS V1-72.

    python -m jobs.setup                # first run: every step; later: today's prices
    python -m jobs.setup --list         # the steps, and which are done
    python -m jobs.setup --only history # one step again

`start.py` runs this and then `jobs.serve`, so a new user types one command.

The first run calls the existing jobs in order, each in its own process: one
that fails is reported and the rest still run, and a run that is interrupted
resumes where it stopped, because finished steps are recorded in
`data/setup.json`. After that, a start refreshes only today's prices.

Every figure comes from its public source, fetched on this machine -- nothing is
downloaded from this project, so nothing of anyone else's is redistributed. The
fund houses that publish through an interface (Kotak, ICICI) are fetched; the
others' monthly workbooks still go into `data/inbox/` by hand (`ingest_inbox`).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from src.m0_data.config import REPO_ROOT, data_root, save_setting, settings


@dataclass(frozen=True)
class Step:
    key: str
    what: str
    job: tuple[str, ...]  # `jobs.<name>` and its arguments


STEPS: tuple[Step, ...] = (
    Step("prices", "today's price for every fund, and AMFI's list of funds",
         ("fetch_nav",)),
    Step("companies", "AMFI's list of listed companies by size",
         ("build_entity_master",)),
    Step("fund_sizes", "each fund's size, from AMFI's quarterly averages",
         ("fetch_aum",)),
    Step("expense_ratios", "each fund's expense ratio, from AMFI, once a month",
         ("fetch_ter", "--if-missing")),
    Step("kotak", "Kotak's latest portfolio disclosure", ("fetch_amc", "--amc", "kotak")),
    Step("icici", "ICICI Prudential's latest portfolio disclosures",
         ("fetch_amc", "--amc", "icici")),
    Step("portfolios", "load the disclosures downloaded", ("ingest_inbox",)),
    Step("benchmarks", "which index each fund is measured against",
         ("fetch_index", "--catalogue", "--resolve", "--declared")),
    Step("history", "price history for the funds with a portfolio",
         ("backfill_scheme_nav", "--held")),
    Step("index_levels", "benchmark levels from NSE -- the slow one, about 70 minutes",
         ("fetch_index", "--held")),
)

#: What a start does once the first run has finished: seconds, not hours.
DAILY = ("prices", "expense_ratios")


def state_path() -> Path:
    return data_root() / "setup.json"


def load_state() -> dict[str, Any]:
    path = state_path()
    if not path.exists():
        return {"done": [], "refreshed": None}
    loaded: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return loaded


def save_state(state: dict[str, Any]) -> None:
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def run_job(job: Sequence[str]) -> int:
    """One job, in this interpreter, from the repository root."""
    return subprocess.call(
        [sys.executable, "-m", f"jobs.{job[0]}", *job[1:]], cwd=REPO_ROOT
    )


def ensure_contact_email(ask: Callable[[str], str] = input) -> str | None:
    """The `From:` address every request carries (§2.3), asked for once.

    Kept in `data/settings.yaml`, under the gitignored `data/`. Not asked when
    there is no one to ask (no terminal): the requests then go out with the
    placeholder, which is honest if unhelpful.
    """
    if os.environ.get("MF_CONTACT_EMAIL") or settings().get("contact_email"):
        return None
    if not sys.stdin or not sys.stdin.isatty():
        return None
    while True:
        try:
            answer = ask(
                "Your email, sent as the From: header when this fetches from AMFI, "
                "NSE and fund houses, so they can reach whoever runs it (kept on "
                "this machine): "
            ).strip()
        except EOFError:
            # A terminal that says it is interactive and is not (some shells
            # on Windows). Carry on with the placeholder rather than fail.
            print("\n  No answer; set MF_CONTACT_EMAIL, or answer on the next start.")
            return None
        if "@" in answer and "." in answer.rsplit("@", 1)[-1]:
            save_setting("contact_email", answer)
            return answer
        print("  That does not look like an email address.")


def run(
    runner: Callable[[Sequence[str]], int] = run_job,
    only: Sequence[str] = (),
    today: date | None = None,
) -> dict[str, list[str]]:
    """Run what is missing: every unfinished step, or today's refresh."""
    today = today or date.today()
    state = load_state()
    done = [str(k) for k in state.get("done", [])]
    # Unfinished steps, plus today's refresh if it has not run today -- a first
    # run resumed tomorrow still wants tomorrow's prices.
    stale = state.get("refreshed") != today.isoformat()
    if only:
        todo = [s for s in STEPS if s.key in only]
    else:
        todo = [
            s for s in STEPS if s.key not in done or (stale and s.key in DAILY)
        ]

    ran: list[str] = []
    failed: list[str] = []
    for n, step in enumerate(todo, start=1):
        print(f"\n[{n}/{len(todo)}] {step.what}", flush=True)
        if runner(step.job) == 0:
            ran.append(step.key)
            if step.key not in done:
                done.append(step.key)
            if step.key in DAILY:
                state["refreshed"] = today.isoformat()
            save_state({**state, "done": done})
        else:
            failed.append(step.key)
            print(f"  {step.key} did not finish; the other steps carry on, and the "
                  f"next start tries it again.", flush=True)
    return {"ran": ran, "failed": failed}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--list", action="store_true", help="show the steps and stop")
    parser.add_argument("--only", nargs="+", choices=[s.key for s in STEPS],
                        help="run just these steps")
    args = parser.parse_args()

    if args.list:
        done = load_state().get("done", [])
        for step in STEPS:
            mark = "done" if step.key in done else "    "
            print(f"  [{mark}] {step.key:13} {step.what}")
        return

    ensure_contact_email()
    first = any(s.key not in load_state().get("done", []) for s in STEPS)
    if first and not args.only:
        print("First run: loading market data from public sources. The last step "
              "takes about 70 minutes; everything before it, a few minutes.")
    summary = run(only=args.only or ())
    if summary["failed"]:
        print(f"\nnot finished: {', '.join(summary['failed'])}")
    elif not summary["ran"]:
        print("everything is up to date")
    sys.exit(0)


if __name__ == "__main__":
    main()
