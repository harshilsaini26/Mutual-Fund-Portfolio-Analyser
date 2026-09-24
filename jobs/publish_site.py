"""Build the public fund explorer: a static copy of the fund pages for GitHub Pages.

    python -m jobs.publish_site                  # build into site/; nothing leaves
    python -m jobs.publish_site --base ""        # a copy to preview locally
    python -m jobs.publish_site --push           # build, then publish to gh-pages

DECISIONS V1-72. The repository is public, so the site is too, and Pages serves
only files: no Python, no database, no response headers. So every fund page is
rendered here, by the same builders, macro and templates as the server's
(`pages.fund_context`), and written out as HTML beside its CSVs.

What it carries is decided, not incidental:

- **Fund prices and fund houses' own disclosures.** AMFI's NAVs are published
  for programmatic use, and portfolio disclosures are mandated public documents.
- **Not NSE's index levels.** They are licensed for personal use (PLAN.md §5.3
  separates that from redistribution), so the market data here withholds them
  (`WithoutIndexLevels`) and every builder draws the fund alone, saying why. The
  benchmark's *name* stays: that is a fact about the fund.
- **Not an aggregator's page.** Holdings read from one are withheld with a note.
- **Never the portfolio.** The ledger here is an empty in-memory database; the
  personal ledger file is not opened, and a test holds that.

Publishing is the user's act: `--push` is the only thing that sends anything,
and it is never run by another job.
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from collections import defaultdict
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from src.common.contracts.market import IndexPoint
from src.common.decimals import connect
from src.common.types import IndexId, UserId
from src.m0_data.config import REPO_ROOT, warehouse_path
from src.m0_data.providers.warehouse import WarehouseMarketDataProvider
from src.m1_ledger.db import apply_ledger_schema, connect_ledger
from src.m1_ledger.providers.position import SqlitePositionProvider
from src.m3_lookthrough.providers.sqlite import SqliteLookThroughProvider
from src.m6_views.api.pages import STATIC, fund_context, templates
from src.m6_views.builder import Scope
from src.m6_views.builders import portfolio  # noqa: F401  — registers the builders
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.export.csv import ENCODING, to_csv
from src.m6_views.registry import VIEW_DEFS, VIEW_REGISTRY
from src.m6_views.states import empty_envelope, error_envelope

#: GitHub Pages publishes at most 1 GB. The build stops well short of it.
SITE_BUDGET_BYTES = 900 * 1024 * 1024
#: About a year of trading days: below this a fund page is mostly empty panels.
MIN_PRICES = 250
PUBLIC_USER = UserId("PUBLIC")
ISIN = re.compile(r"IN[A-Z0-9]{10}")
#: What the public pages load. d3 draws only the portfolio Sankey, which the
#: public copy does not have.
STATIC_FILES = ("app.css", "app.js", "charts.js", "vendor/echarts.v6.1.0.min.js")
#: A file only this job writes, so a rebuild can tell its own output from a
#: directory it must not delete.
MARKER = ".nojekyll"

AGGREGATOR_WITHHELD = (
    "In the self-hosted app, this fund's holdings come from an aggregator's page "
    "rather than the fund house's own disclosure, so the public copy leaves them out."
)
NOT_BUILT = "This picture could not be built for the public copy."


class SiteTooLarge(RuntimeError):
    """The site would not fit GitHub Pages; publishing it would fail there."""


class WithoutIndexLevels(WarehouseMarketDataProvider):
    """The warehouse's market data with every index level withheld.

    `index_levels_withheld` is what the fund builders read to say "left out of
    this public copy" rather than "none on record" (`builders/fund/common.py`).
    """

    index_levels_withheld = True

    def index_series(
        self, index_id: IndexId, start: date, end: date
    ) -> list[IndexPoint]:
        return []

    def index_level(self, index_id: IndexId, on: date) -> Decimal | None:
        return None


def public_deps(warehouse: Any) -> Deps:
    """The providers the public pages may read: never a personal ledger."""
    ledger = connect_ledger(":memory:", allow_unencrypted=True)
    apply_ledger_schema(ledger)
    return Deps(
        lookthrough=SqliteLookThroughProvider(ledger, warehouse),
        positions=SqlitePositionProvider(ledger),
        market=WithoutIndexLevels(warehouse),
    )


def funds_to_publish(warehouse: Any) -> list[dict[str, str]]:
    """One page per fund with a year of prices, as the share class a reader
    most likely means: Direct before Regular, Growth before IDCW (the same
    preference search uses)."""
    priced = {
        str(sid) for sid, n in warehouse.execute(
            "SELECT scheme_id, count(*) FROM nav_daily GROUP BY scheme_id"
        )
        if n >= MIN_PRICES
    }
    best: dict[str, tuple[tuple[bool, bool, str], dict[str, str]]] = {}
    schemes = warehouse.execute(
        "SELECT scheme_id, scheme_name, fund_name, plan, option, sebi_category,"
        " amc_id, scheme_family FROM scheme WHERE status = 'active'"
    )
    for sid, scheme_name, fund_name, plan, option, category, amc, family in schemes:
        # ISIN-keyed only: a scheme AMFI lists without one is keyed
        # `AMFI:<code>:...`, which is no folder name on Windows and no clean URL.
        if str(sid) not in priced or not ISIN.fullmatch(str(sid)):
            continue
        key = f"{amc}|{family or sid}"
        rank = (plan != "direct", option != "growth", str(sid))
        if key not in best or rank < best[key][0]:
            best[key] = (rank, {
                "scheme_id": str(sid),
                "name": str(fund_name or scheme_name),
                "category": str(category or "Other"),
                "detail": " · ".join(
                    str(x).title() if x in (plan, option) else str(x)
                    for x in (category, plan, option)
                    if x and x != "unknown"
                ),
            })
    return sorted((f for _, f in best.values()), key=lambda f: f["name"].lower())


def _build(
    deps: Deps, view_id: str, scope: Scope, params: dict[str, Any]
) -> ViewEnvelope:
    """`app.build_view`'s boundary, without the server: a builder that raises
    degrades its own panel, never the page."""
    try:
        return VIEW_REGISTRY[view_id](deps).build(scope, params)
    except Exception as exc:
        return error_envelope(view_id, VIEW_DEFS[view_id].question, scope, exc)


def _adapter(folder: Path, url: str, scope: Scope) -> Any:
    """What the public copy changes in each panel before it is drawn: no period
    tabs (no server to fetch them from), the export link pointing at a CSV
    written beside the page, and what it withholds."""

    def adapt(env: ViewEnvelope) -> ViewEnvelope:
        if env.view_id == "fund_portfolio" and env.payload.get("tier") == "aggregator":
            env = empty_envelope(env.view_id, env.question, scope, AGGREGATOR_WITHHELD)
        elif env.state.value == "error":
            # An exception's text is for the person running the build, not for
            # the public page.
            print(f"  ! {scope.scope_id} {env.view_id}: {env.state_reason}")
            env = empty_envelope(env.view_id, env.question, scope, NOT_BUILT)
        name = f"{env.view_id}.csv"
        env = replace(
            env,
            payload={k: v for k, v in env.payload.items() if k != "tabs"},
            export_url=f"{url}{name}",
        )
        (folder / name).write_text(to_csv(env), encoding=ENCODING, newline="")
        return env

    return adapt


def build_site(
    warehouse: Any, out: Path, base: str, today: date | None = None
) -> dict[str, int]:
    """Render every public fund page, the index and the search list into `out`."""
    today = today or date.today()
    if out.exists():
        if any(out.iterdir()) and not (out / MARKER).exists():
            raise RuntimeError(
                f"{out} holds files this job did not write; refusing to replace it."
            )
        shutil.rmtree(out)
    out.mkdir(parents=True)

    deps = public_deps(warehouse)
    engine = templates(root=base, static=True)
    shell = {"catalogue": [], "health": {}, "qs": "", "active": "", "built": today}
    funds = funds_to_publish(warehouse)
    for n, fund in enumerate(funds, start=1):
        sid = fund["scheme_id"]
        folder = out / "fund" / sid
        folder.mkdir(parents=True)
        scope = Scope(PUBLIC_USER, today, "scheme", sid)
        context = fund_context(
            lambda v, s, p: _build(deps, v, s, p), scope, "max", base,
            _adapter(folder, f"{base}/fund/{sid}/", scope),
        )
        (folder / "index.html").write_text(
            engine.get_template("fund.html").render({**shell, **context}),
            encoding="utf-8",
        )
        if n % 50 == 0:
            print(f"  {n:,} of {len(funds):,} fund pages")

    by_category: dict[str, list[dict[str, str]]] = defaultdict(list)
    for fund in funds:
        by_category[fund["category"]].append(fund)
    (out / "index.html").write_text(
        engine.get_template("explorer.html").render(
            {**shell, "categories": sorted(by_category.items()), "count": len(funds)}
        ),
        encoding="utf-8",
    )
    (out / "404.html").write_text(
        engine.get_template("notfound.html").render(shell), encoding="utf-8"
    )
    (out / "search.json").write_text(
        json.dumps(
            [{"name": f["name"], "detail": f["detail"],
              "url": f"{base}/fund/{f['scheme_id']}/"} for f in funds],
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    for name in STATIC_FILES:
        target = out / "static" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(STATIC / name, target)
    (out / MARKER).write_text("", encoding="utf-8")

    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    check_budget(size)
    return {"funds": len(funds), "bytes": size}


def check_budget(size: int, budget: int = SITE_BUDGET_BYTES) -> None:
    if size > budget:
        raise SiteTooLarge(
            f"the site is {size / 1e6:,.0f} MB and GitHub Pages allows 1 GB; "
            f"the build stops at {budget / 1e6:,.0f} MB. A lighter page profile "
            f"is the fix (DECISIONS V1-72)."
        )


def _git(*args: str, cwd: Path = REPO_ROOT) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def default_base() -> str:
    """`/<repository name>`: where GitHub Pages serves a project site."""
    remote = _git("remote", "get-url", "origin")
    return "/" + remote.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def push(out: Path) -> None:
    """Publish `out` as the only commit on `gh-pages`, replacing the last.

    An orphan commit force-pushed, so the branch never accumulates history and
    the repository does not grow with every rebuild. The user's own git
    identity and credentials; nothing here holds a token.
    """
    remote = _git("remote", "get-url", "origin")
    author = [f"user.name={_git('config', 'user.name')}",
              f"user.email={_git('config', 'user.email')}"]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copytree(out, work, dirs_exist_ok=True)
        _git("init", "-q", "-b", "gh-pages", cwd=work)
        _git("add", "-A", cwd=work)
        _git(*[a for pair in author for a in ("-c", pair)], "commit", "-q", "-m",
             f"Public fund explorer, built {date.today().isoformat()}", cwd=work)
        _git("push", "-q", "--force", remote, "gh-pages", cwd=work)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--out", type=Path, default=REPO_ROOT / "site")
    parser.add_argument("--base", help="URL prefix; default /<repository name>")
    parser.add_argument("--push", action="store_true",
                        help="publish to the gh-pages branch after building")
    args = parser.parse_args()

    base = default_base() if args.base is None else args.base.rstrip("/")
    warehouse = connect(str(warehouse_path()))
    # The public copy reads; it never writes. SQLite enforces it from here on.
    warehouse.execute("PRAGMA query_only = ON")
    print(f"building the public copy into {args.out} (links under {base or '/'})")
    summary = build_site(warehouse, args.out, base)
    print(f"{summary['funds']:,} fund pages, {summary['bytes'] / 1e6:,.1f} MB")
    if args.push:
        push(args.out)
        print("published to gh-pages. GitHub serves it once Pages is set to "
              "deploy from that branch (Settings -> Pages).")
    else:
        print("not published: add --push to publish it.")
    sys.exit(0)


if __name__ == "__main__":
    main()
