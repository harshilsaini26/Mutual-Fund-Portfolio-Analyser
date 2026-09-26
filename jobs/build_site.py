"""The public site, built from the APIs in a fresh workspace. DECISIONS V1-75.

    python -m jobs.build_site --out site --store previous-site   # what CI runs
    python -m jobs.build_site --out site --base ""                # a local preview
    python -m jobs.build_site --out site --store site --push      # build and publish

GitHub Actions runs this every day (`.github/workflows/site.yml`) on a machine
that starts empty. Everything it makes lives in a temporary workspace -- the
warehouse and the raw archive both -- except the two things it keeps: the site,
and inside the site the **store**, each fund's prices in compact form
(`m0_data.store`). The next build reads the store back from the published site,
so a normal day asks mfapi for nothing and AMFI for one file.

In order:
  1. today's prices, the scheme master, categories  (fetch_nav)
  2. AMFI's company list, fund sizes and TERs       (build_entity_master, fetch_aum,
                                                     fetch_ter)
  3. holdings: Kotak and ICICI, then `inbox/`       (fetch_amc, ingest_inbox)
  4. the store, then funds still short, from mfapi  (store, backfill_scheme_nav)
     and at most 100 of Groww's pages                (fetch_groww --crawl)
  5. the total-return series, then every fund's     (nav_adj, m2_fund.stats)
     figures for its peers
  6. the pages, then the store beside them, then the budget check
  7. with `--push`, one commit on gh-pages

**Never in the build:** NSE's index levels (licensed for personal use -- the
benchmarks stay in the local app), and the personal ledger (the public copy opens
an empty in-memory one; `publish_site` asserts it). Each fetching step runs as
its own process, as `jobs.setup` runs them, so one failing is reported and the
rest still build; only the price step is required.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from src.common.decimals import connect
from src.m0_data.config import REPO_ROOT, contact_email
from src.m0_data.derive.nav_adj import build_all_nav_adj
from src.m0_data.store import (
    StoreError,
    restore,
    restore_fetched,
    restore_holdings,
    save,
    save_fetched,
    save_holdings,
)
from src.m0_data.universe import live_funds
from src.m2_fund.stats import rebuild_fund_stats

from jobs import publish_site

#: Fetching steps before the history, each `jobs.<name>` and its arguments.
#: `required` steps stop the build when they fail; the others degrade a panel.
FETCH_STEPS: tuple[tuple[str, tuple[str, ...], bool], ...] = (
    ("today's prices and the scheme master", ("fetch_nav",), True),
    ("AMFI's company list", ("build_entity_master",), False),
    ("fund sizes", ("fetch_aum",), False),
    # One ~4 MB workbook: last month's, the same all month (V1-78).
    ("expense ratios", ("fetch_ter",), False),
    ("Kotak's disclosures", ("fetch_amc", "--amc", "kotak"), False),
    ("ICICI Prudential's disclosures", ("fetch_amc", "--amc", "icici"), False),
    ("the disclosures downloaded", ("ingest_inbox",), False),
)
#: Groww pages read per build (V1-79): the first pass over its ~1,600 takes
#: about sixteen days, and a monthly refresh after it about fifty a day.
GROWW_PAGES_A_DAY = 100
#: The crawl's memory of each page, carried in the site like the store.
GROWW_MAP = Path("data") / "groww.csv"
#: Workbooks a person downloaded and committed, read by `ingest_inbox`.
REPO_INBOX = REPO_ROOT / "inbox"
#: Placeholder `config.contact_email` falls back to; CI must not use it.
UNSET_EMAIL = "unset@example.invalid"


def workspace_env(workdir: Path) -> dict[str, str]:
    """The environment every step runs in: all data under `workdir`, and nothing
    pointing at this machine's own warehouse or ledger."""
    env = {k: v for k, v in os.environ.items()
           if k not in ("MF_WAREHOUSE", "MF_LEDGER")}
    env["MF_DATA_ROOT"] = str(workdir)
    return env


def run_step(job: Sequence[str], env: dict[str, str]) -> int:
    return subprocess.call([sys.executable, "-m", f"jobs.{job[0]}", *job[1:]],
                           cwd=REPO_ROOT, env=env)


def build(out: Path, store_root: Path | None, base: str, workdir: Path,
          groww_pages: int = GROWW_PAGES_A_DAY) -> dict[str, int]:
    env = workspace_env(workdir)
    for what, job, required in FETCH_STEPS:
        print(f"\n== {what}", flush=True)
        if run_step(job, env) != 0:
            if required:
                raise SystemExit(f"the build needs {what}, and that step failed")
            print(f"  ! {what} did not finish; the build carries on without it")

    db = workdir / "warehouse" / "canonical.db"
    conn = connect(str(db))
    try:
        funds = live_funds(conn)
        print(f"\n== the store: {len(funds):,} live funds", flush=True)
        if store_root is not None:
            done = restore(conn, store_root, funds)
            print(f"  {done.funds:,} funds, {done.rows:,} prices restored;"
                  f" {len(done.refused)} refused")
            for scheme_id, why in done.refused[:20]:
                print(f"  ! {scheme_id}: {why}")
            print(f"  {restore_fetched(conn, store_root):,} funds' last mfapi fetch")
            try:
                held = restore_holdings(conn, store_root)
                print(f"  {held:,} of Groww's portfolios restored")
                if (store_root / GROWW_MAP).is_file():
                    shutil.copyfile(store_root / GROWW_MAP, workdir / "groww.csv")
            except StoreError as exc:
                # Without its portfolios the map would say they are loaded.
                print(f"  ! Groww's portfolios: {exc}; every page is read again")
    finally:
        conn.close()

    print(f"\n== Groww's pages, at most {groww_pages}", flush=True)
    if run_step(("fetch_groww", "--crawl", str(groww_pages),
                 "--map", str(workdir / "groww.csv")), env) != 0:
        print("  ! the crawl did not finish; pages use the portfolios loaded")

    print("\n== history for funds still short (mfapi)", flush=True)
    if run_step(("backfill_scheme_nav", "--universe", "--missing", "--quiet"), env) != 0:
        print("  ! the history step did not finish; pages use what is loaded")

    conn = connect(str(db))
    try:
        # Restored rows arrive without their total-return series.
        build_all_nav_adj(conn)
        conn.commit()
        today = date.today()
        print("\n== every fund's figures, for its peers (V1-77)", flush=True)
        print(f"  {rebuild_fund_stats(conn, today, progress=print):,} windows")
        print("\n== the pages", flush=True)
        summary = publish_site.build_site(conn, out, base, today)
        kept = save(conn, out, live_funds(conn))
        save_fetched(conn, out)
        print(f"  the store: {kept:,} funds, "
              f"{save_holdings(conn, out):,} of Groww's portfolios")
        if (workdir / "groww.csv").is_file():
            shutil.copyfile(workdir / "groww.csv", out / GROWW_MAP)
    finally:
        conn.close()
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    publish_site.check_budget(size)
    return {"funds": summary["funds"], "stored": kept, "bytes": size}


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "site")
    parser.add_argument("--store", type=Path,
                        help="a previous build of the site, whose data/ to start from")
    parser.add_argument("--base", help="URL prefix; default /<repository name>")
    parser.add_argument("--push", action="store_true",
                        help="publish to the gh-pages branch after building")
    parser.add_argument("--groww-pages", type=int, default=GROWW_PAGES_A_DAY,
                        help="Groww pages to read (0 for a second build the same day)")
    parser.add_argument("--workdir", type=Path,
                        help="keep the workspace here instead of a temporary folder")
    args = parser.parse_args()

    if contact_email() == UNSET_EMAIL:
        raise SystemExit(
            "no contact email: every request carries one (MODULE_0 §2.3). Set "
            "MF_CONTACT_EMAIL (in GitHub: Settings -> Secrets and variables -> "
            "Actions -> Variables)."
        )
    base = publish_site.default_base() if args.base is None else args.base.rstrip("/")
    store_root = args.store if args.store and args.store.is_dir() else None
    if args.store and store_root is None:
        print(f"no previous site at {args.store}: every fund's history is fetched")

    workdir = args.workdir or Path(tempfile.mkdtemp(prefix="mf-build-"))
    workdir.mkdir(parents=True, exist_ok=True)
    if REPO_INBOX.is_dir():
        shutil.copytree(REPO_INBOX, workdir / "inbox", dirs_exist_ok=True,
                        ignore=shutil.ignore_patterns("README.md"))
    try:
        summary = build(args.out.resolve(), store_root, base, workdir, args.groww_pages)
    finally:
        if args.workdir is None:
            shutil.rmtree(workdir, ignore_errors=True)
    print(f"\n{summary['funds']:,} fund pages, {summary['stored']:,} funds stored,"
          f" {summary['bytes'] / 1e6:,.1f} MB (links under {base or '/'})")
    if args.push:
        publish_site.push(args.out)
        print("published to gh-pages")


if __name__ == "__main__":
    main()
