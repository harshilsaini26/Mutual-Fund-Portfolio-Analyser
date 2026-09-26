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
- **An aggregator's page, marked.** Holdings read from Groww's pages are
  published with a caveat naming the source (V1-79; V1-72 withheld them). Groww's
  terms of use apply to republishing them.
- **Not NSE's index levels.** They are licensed for personal use (PLAN.md §5.3
  separates that from redistribution), so the market data here withholds them
  (`PublicMarket`). Where an index fund declares the same benchmark as a fund
  (both from their Groww pages), that index fund's own price stands in for the
  index, named as such (V1-81); otherwise the fund is drawn alone, saying why.
- **Never the portfolio.** The ledger here is an empty in-memory database; the
  personal ledger file is not opened, and a test holds that.

`--push` publishes from this machine; the nightly build (`jobs.build_site`,
V1-75) is the other thing that does.
"""

from __future__ import annotations

import argparse
import json
import os
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
from src.common.types import IndexId, SchemeId, UserId
from src.m0_data.categories import FAMILIES, category_of
from src.m0_data.config import REPO_ROOT, warehouse_path
from src.m0_data.normalise.index_id import index_key
from src.m0_data.providers.warehouse import SchemeFacts, WarehouseMarketDataProvider
from src.m0_data.universe import ISIN, live_funds
from src.m1_ledger.db import apply_ledger_schema, connect_ledger
from src.m1_ledger.providers.position import SqlitePositionProvider
from src.m2_fund.windows import spans
from src.m3_lookthrough.providers.sqlite import SqliteLookThroughProvider
from src.m6_views.api.pages import STATIC, fund_context, templates
from src.m6_views.builder import Scope
from src.m6_views.builders import portfolio  # noqa: F401  — registers the builders
from src.m6_views.deps import Deps
from src.m6_views.envelope import ViewEnvelope
from src.m6_views.export.csv import ENCODING, to_csv
from src.m6_views.format import DASH, format_inr, format_pct
from src.m6_views.registry import VIEW_DEFS, VIEW_REGISTRY
from src.m6_views.states import empty_envelope, error_envelope

from jobs.fetch_groww import declared_benchmarks

#: GitHub Pages publishes at most 1 GB. The build stops well short of it.
SITE_BUDGET_BYTES = 900 * 1024 * 1024
#: About a year of trading days: below this a fund page is mostly empty panels.
MIN_PRICES = 250
PUBLIC_USER = UserId("PUBLIC")
#: What the public pages load. d3 draws only the portfolio Sankey, which the
#: public copy does not have.
STATIC_FILES = (
    "app.css", "app.js", "charts.js", "theme.js", "lenis.css",
    "vendor/echarts.v6.1.0.min.js", "vendor/lenis.v1.3.26.min.js",
    "vendor/islands.v1.js", "fonts/rubik-latin-wght-normal.woff2",
    "fonts/terminess-Regular.woff2", "fonts/terminess-Bold.woff2",
)
#: A file only this job writes, so a rebuild can tell its own output from a
#: directory it must not delete.
MARKER = ".nojekyll"

NOT_BUILT = "This picture could not be built for the public copy."

RETURN_WINDOWS = ("1y", "3y", "5y")
#: The front page's category cards (DECISIONS V1-80, after MF Zone's): the six
#: equity categories most funds sit in, each with its five highest 3-year returns.
LEADER_CATEGORIES = (
    "equity/flexi_cap", "equity/large_cap", "equity/mid_cap",
    "equity/small_cap", "equity/large_mid_cap", "equity/elss",
)
LEADERS_PER_CARD = 5


class SiteTooLarge(RuntimeError):
    """The site would not fit GitHub Pages; publishing it would fail there."""


#: The id a proxied benchmark carries: never an index id, so nothing can take
#: an index fund's price for the index's own level.
PROXY = "proxy:"
_TRI = re.compile(r"\s*[-(]?\s*(total returns? index|tri)\)?\s*$", re.I)


def trackers(warehouse: Any, declared: dict[str, str]) -> dict[str, list[str]]:
    """Each benchmark (by `index_key`) and the index funds and ETFs that declare
    it, longest price record first: the first is its proxy."""
    kinds = {f.scheme_id: category_of(f.category).key for f in live_funds(warehouse)}
    ids = [s for s in declared if kinds.get(s, "").startswith(("index/", "etf/"))]
    first = dict(warehouse.execute(
        f"SELECT scheme_id, min(nav_date) FROM nav_daily WHERE scheme_id IN"
        f" ({','.join('?' * len(ids))}) GROUP BY scheme_id", ids).fetchall())
    out: dict[str, list[str]] = defaultdict(list)
    for sid in sorted((s for s in ids if s in first), key=lambda s: (str(first[s]), s)):
        out[index_key(declared[sid])].append(sid)
    return out


class PublicMarket(WarehouseMarketDataProvider):
    """The warehouse's market data with every index level withheld (V1-72), and
    an index fund's price in place of each benchmark one tracks (V1-81).

    `index_levels_withheld` is what the fund builders read to say "left out of
    this public copy" rather than "none on record" (`builders/fund/common.py`);
    a fund with a proxy is told apart by its `proxy:` benchmark id.
    """

    index_levels_withheld = True

    def __init__(self, conn: Any, declared: dict[str, str] | None = None) -> None:
        super().__init__(conn)
        self.declared = declared or {}
        self.trackers = trackers(conn, self.declared) if self.declared else {}

    def proxy(self, scheme_id: str) -> str | None:
        """The index fund standing in for this fund's benchmark: never itself."""
        name = self.declared.get(scheme_id)
        found = self.trackers.get(index_key(name), []) if name else []
        return next((s for s in found if s != scheme_id), None)

    def benchmark_for(self, scheme_id: SchemeId) -> IndexId | None:
        proxy = self.proxy(str(scheme_id))
        return IndexId(PROXY + proxy) if proxy else None

    def index_series(
        self, index_id: IndexId, start: date, end: date
    ) -> list[IndexPoint]:
        if not index_id.startswith(PROXY):
            return []
        return [IndexPoint(index_id, p.nav_date, p.nav) for p in
                self.nav_series(SchemeId(index_id[len(PROXY):]), start, end)]

    def index_level(self, index_id: IndexId, on: date) -> Decimal | None:
        found = self.index_series(index_id, on, on)
        return found[0].level if found else None

    def scheme_facts(self, scheme_id: SchemeId) -> SchemeFacts | None:
        facts = super().scheme_facts(scheme_id)
        proxy = self.proxy(str(scheme_id))
        tracker = super().scheme_facts(SchemeId(proxy)) if proxy else None
        if facts is None or tracker is None:
            return facts
        index = _TRI.sub("", self.declared[str(scheme_id)])
        return replace(facts, benchmark_id=PROXY + str(proxy),
                       benchmark_name=f"{index} (via {tracker.name})")


def public_deps(warehouse: Any, declared: dict[str, str] | None = None) -> Deps:
    """The providers the public pages may read: never a personal ledger.
    `declared` is each fund's benchmark as its Groww page names it."""
    ledger = connect_ledger(":memory:", allow_unencrypted=True)
    apply_ledger_schema(ledger)
    return Deps(
        lookthrough=SqliteLookThroughProvider(ledger, warehouse),
        positions=SqlitePositionProvider(ledger),
        market=PublicMarket(warehouse, declared),
    )


def funds_to_publish(warehouse: Any) -> list[dict[str, str]]:
    """One page per live fund with a year of prices, as its Direct share class.

    The funds are `m0_data.universe.live_funds`, the same set the history
    backfill loads and the peer groups rank (DECISIONS V1-75). ISIN-keyed only: a
    scheme AMFI lists without one is keyed `AMFI:<code>:...`, which is no folder
    name on Windows and no clean URL.
    """
    priced = {
        str(sid) for sid, n in warehouse.execute(
            "SELECT scheme_id, count(*) FROM nav_daily GROUP BY scheme_id"
        )
        if n >= MIN_PRICES
    }
    return [
        {
            "scheme_id": fund.scheme_id,
            "name": fund.name,
            "category": fund.category,
            "detail": " · ".join(
                str(x).title() if x in (fund.plan, fund.option) else str(x)
                for x in (fund.category, fund.plan, fund.option)
                if x and x != "unknown"
            ),
        }
        for fund in live_funds(warehouse)
        if fund.scheme_id in priced and ISIN.fullmatch(fund.scheme_id)
    ]


def _return_cell(value: Any) -> dict[str, Any]:
    """One return in the front page's table: the figure as the reader sees it,
    the value it sorts on, and its direction as a symbol as well as a colour
    (§10.3). A window with too little history is a dash, never a zero."""
    if value is None:
        return {"value": "", "label": DASH, "tone": None, "symbol": None}
    number = Decimal(str(value))
    tone = "gain" if number > 0 else "loss" if number < 0 else None
    return {
        "value": str(number),
        "label": format_pct(number * 100, precision=1, signed=True),
        "tone": tone,
        "symbol": {"gain": "▲", "loss": "▼"}.get(tone or ""),
    }


def explorer_row(
    fund: dict[str, str], facts: Any, detail: ViewEnvelope | None,
    peers: ViewEnvelope | None = None,
) -> dict[str, Any]:
    """One fund's row in the front page's table (DECISIONS V1-74).

    Nothing new is computed: the returns are the fund page's own "every figure"
    rows (`fund_xray_header`), the rank is its peer panel's (`fund_peers`), and
    the size, cost and house are the scheme facts its header already shows.
    They are formatted here, in Python (§16.4).
    """
    windows: dict[str, Any] = {}
    if detail is not None and detail.state.value == "ok":
        # A column headed "5 years" shows only a window the prices span (`spans`).
        windows = {
            row["window_key"]: row.get("return_ann")
            for row in detail.payload.get("rows", [])
            if row["window_key"] in RETURN_WINDOWS
            and row.get("obs_days") is not None
            and spans(int(row["obs_days"]), row["window_key"])
        }
    size = facts.aum_inr if facts is not None else None
    ter = facts.ter if facts is not None else None
    category = category_of(fund["category"])
    rank: dict[str, Any] = {}
    if peers is not None and peers.state.value == "ok":
        rank = peers.payload.get("rank_3y") or {}
    return {
        **fund,
        "family": category.family,
        # The canonical name (DECISIONS V1-76), not whichever of AMFI's two
        # spellings this fund house happens to use.
        "category_short": category.name,
        "category_key": category.key,
        "house": (facts.amc_name if facts is not None else None) or "",
        "size_value": str(size) if size is not None else "",
        "size_label": format_inr(size, precision=0) if size is not None else DASH,
        "ter_value": str(ter) if ter is not None else "",
        "ter_label": format_pct(ter, precision=2) if ter is not None else DASH,
        "returns": [_return_cell(windows.get(key)) for key in RETURN_WINDOWS],
        # Sorted by quarter: a rank means something only within its category.
        "rank_value": str(rank["quartile"]) if rank.get("quartile") else "",
        "rank_label": rank.get("label") or DASH,
        "rank_quarter": rank.get("quarter") or "",
    }


def category_leaders(
    rows: list[dict[str, Any]], keys: tuple[str, ...] = LEADER_CATEGORIES,
    top: int = LEADERS_PER_CARD,
) -> list[dict[str, Any]]:
    """Each category's funds with the highest 3-year return, for the front page.

    Sorted here, on the Decimal behind each figure, never in SQL (CLAUDE.md
    invariant 1). A fund without three years of prices is not in a card, and the
    card says how many funds the category has in all.
    """
    three = RETURN_WINDOWS.index("3y")
    cards = []
    for key in keys:
        members = [r for r in rows if r.get("category_key") == key]
        ranked = sorted(
            (r for r in members if r["returns"][three]["value"]),
            key=lambda r: Decimal(r["returns"][three]["value"]),
            reverse=True,
        )[:top]
        if ranked:
            cards.append({"key": key, "name": members[0]["category_short"],
                          "count": len(members), "funds": ranked})
    return cards


def fund_map(funds: list[dict[str, str]]) -> list[dict[str, Any]]:
    """The front page's map (V1-81): each family, then its categories with what
    SEBI's rules say they hold and how many funds they have here. Largest first."""
    counts: dict[str, int] = defaultdict(int)
    found = {}
    for fund in funds:
        category = category_of(fund["category"])
        counts[category.key] += 1
        found[category.key] = category
    families = []
    for key, name, hint in FAMILIES:
        members = sorted((c for c in found.values() if c.family == key),
                         key=lambda c: (-counts[c.key], c.name))
        if members:
            families.append({
                "key": key, "name": name, "hint": hint,
                "count": sum(counts[c.key] for c in members),
                "categories": [{"key": c.key, "name": c.name, "about": c.about,
                                "count": counts[c.key]} for c in members],
            })
    return families


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
        if env.state.value == "error":
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
    warehouse: Any, out: Path, base: str, today: date | None = None,
    declared: dict[str, str] | None = None,
) -> dict[str, int]:
    """Render every public fund page, the index and the search list into `out`.
    `declared` is each fund's benchmark by ISIN, from the Groww crawl's map."""
    today = today or date.today()
    if out.exists():
        if any(out.iterdir()) and not (out / MARKER).exists():
            raise RuntimeError(
                f"{out} holds files this job did not write; refusing to replace it."
            )
        shutil.rmtree(out)
    out.mkdir(parents=True)

    deps = public_deps(warehouse, declared)
    engine = templates(root=base, static=True)
    shell = {"catalogue": [], "health": {}, "qs": "", "active": "", "built": today}
    funds = funds_to_publish(warehouse)
    rows: list[dict[str, Any]] = []
    houses: set[str] = set()
    with_holdings = 0
    prices_to: date | None = None
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
        # The front page's table and counts, from what this page just drew.
        facts = deps.market.scheme_facts(SchemeId(sid))
        panels = {p["env"].view_id: p["env"] for p in context["panels"]}
        rows.append(explorer_row(fund, facts, context["detail"]["env"],
                                 panels.get("fund_peers")))
        if facts is not None and facts.amc_name:
            houses.add(facts.amc_name)
        for panel in context["panels"]:
            env = panel["env"]
            if env.state.value != "ok":
                continue
            if env.view_id == "fund_portfolio":
                with_holdings += 1
            if env.view_id == "fund_header":
                prices_to = max(prices_to or env.data_as_of, env.data_as_of)
        if n % 50 == 0:
            print(f"  {n:,} of {len(funds):,} fund pages")

    by_category: dict[str, list[dict[str, str]]] = defaultdict(list)
    for fund in funds:
        by_category[fund["category"]].append(fund)
    counted: defaultdict[str, int] = defaultdict(int)
    for row in rows:
        counted[row["family"]] += 1
    families = [
        {"key": key, "name": name, "hint": hint, "count": counted[key]}
        for key, name, hint in FAMILIES
        if counted[key]
    ]
    stats = {
        "houses": len(houses),
        "holdings": with_holdings,
        "prices_to": prices_to,
        "categories": len({r["category_key"] for r in rows}),
    }
    (out / "index.html").write_text(
        engine.get_template("home.html").render({
            **shell, "count": len(funds), "stats": stats,
            "leaders": category_leaders(rows), "fund_map": fund_map(funds),
        }),
        encoding="utf-8",
    )
    (out / "funds").mkdir(exist_ok=True)
    (out / "funds" / "index.html").write_text(
        engine.get_template("explorer.html").render({
            **shell,
            "active": "funds",
            "categories": sorted(by_category.items()),
            "category_options": sorted(
                {(r["category_key"], r["category_short"]) for r in rows},
                key=lambda kv: kv[1].lower(),
            ),
            "count": len(funds),
            "funds": rows,
            "families": families,
            "stats": stats,
        }),
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


class GitFailed(RuntimeError):
    """A git command failed; its own message is kept, since a bare exit status
    left a failed publish unexplained."""


def _git(*args: str, cwd: Path = REPO_ROOT) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if done.returncode != 0:
        # The arguments are not echoed: in CI the push URL carries a token.
        raise GitFailed(
            f"git {args[0]} exited {done.returncode}: {done.stderr.strip()[-600:]}"
        )
    return done.stdout.strip()


def default_base() -> str:
    """`/<repository name>`: where GitHub Pages serves a project site."""
    remote = _git("remote", "get-url", "origin")
    return "/" + remote.rstrip("/").rsplit("/", 1)[-1].removesuffix(".git")


def push(out: Path) -> None:
    """Publish `out` as the only commit on `gh-pages`, replacing the last.

    An orphan commit force-pushed, so the branch never accumulates history and
    the repository does not grow with every rebuild. From this machine, the
    user's own git identity and credentials. In GitHub Actions the workflow hands
    in `SITE_PUSH_URL` with its short-lived token, because this commit is made in
    a new repository that does not inherit the checkout's credentials.

    Pushed twice at most: one 250 MB push failed once for no reason git gave.
    """
    remote = os.environ.get("SITE_PUSH_URL") or _git("remote", "get-url", "origin")
    author = [f"user.name={_git('config', 'user.name')}",
              f"user.email={_git('config', 'user.email')}"]
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        shutil.copytree(out, work, dirs_exist_ok=True)
        _git("init", "-q", "-b", "gh-pages", cwd=work)
        _git("add", "-A", cwd=work)
        _git(*[a for pair in author for a in ("-c", pair)], "commit", "-q", "-m",
             f"Public fund explorer, built {date.today().isoformat()}", cwd=work)
        try:
            _git("push", "-q", "--force", remote, "gh-pages", cwd=work)
        except GitFailed as first:
            print(f"  ! push failed, trying once more: {first}")
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
    summary = build_site(warehouse, args.out, base,
                         declared=declared_benchmarks(REPO_ROOT / "data" / "groww.csv"))
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
