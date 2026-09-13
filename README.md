# Mutual Fund Portfolio Analyser

[![gate](https://github.com/harshilsaini26/Mutual-Fund-Portfolio-Analyser/actions/workflows/ci.yml/badge.svg)](https://github.com/harshilsaini26/Mutual-Fund-Portfolio-Analyser/actions/workflows/ci.yml)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

A self-hosted look-through analytics platform for Indian mutual fund investors. It
dissolves the funds you hold into the companies you actually own, and it tells you the
things a fund factsheet cannot: how much of two funds is the same stock, how much of your
money is a company bought twice, and how concentrated you really are once the wrappers
come off.

It runs on your machine. Your transactions never leave it.

---

## The problem

A portfolio of five equity funds looks diversified. Open them up and it is frequently one
fund bought five times — the same twenty large caps, in slightly different proportions,
with five sets of fees. Nothing in a factsheet tells you that, because a factsheet
describes one fund and the overlap is a property of the *set*.

On two real disclosures this codebase reports:

```
HDFC Flexi Cap  x  Nippon India Growth Mid Cap
    overlap        13.32%          17 shared companies of 156
    duplicated     6.66% of the portfolio held through more than one fund
```

That is the whole product in three numbers.

---

## What makes this different from a spreadsheet

Most of the engineering here is not the arithmetic. It is refusing to produce a number
that looks right and is not.

**Every figure is `Decimal`, end to end.** Floats appear only inside the XIRR solver and
convert back at the boundary. SQLite is the interesting part: a column declared `DECIMAL`
gets NUMERIC affinity and silently stores your money as a float, so every decimal column
here is declared `DECIMAL_TEXT` and a schema check enforces it. Two further traps cost a
defect each to find — `SUM()` over a text-affinity column coerces the *aggregate* to a
float even though every stored value is exact, and `ORDER BY` on one sorts as text, so
`"5000"` outranks `"25000"` and a top-20 list is not the top 20. Both are now invariants.

**Nothing is silently dropped.** A holding whose issuer cannot be identified becomes
`__UNRESOLVED__` and stays visible in every chart, pinned outside the top-N so tail
aggregation cannot fold it away. A fund with no published portfolio becomes
`__NO_DISCLOSURE__` and takes its full value with it. The unresolved percentage is
computed, stored, and rendered — it is a gate criterion, not a log line.

**Absent is not zero.** An uncomputed XIRR renders as an em dash, never `0.0%`, because
one of those is an admission and the other is a claim. A Gini coefficient is `NULL` when a
short position puts a negative weight in the pool, because the formula still returns an
ordinary-looking number that means nothing.

**Every parse is reconciled against the file's own stated total.** AMC disclosures nest
their sections differently and a parser tuned to one over-counts another by 2.9x. No list
of section labels can catch that; arithmetic can, because the file says what it adds up
to. A parse that lands more than 2% from the publisher's own figure refuses to load.

**Raise, don't clamp.** A redemption for more units than the book holds means a missing
statement, not a number to round down. A purchase with no price is a parse gap, not a free
purchase. Both raise.

---

## What it does today

```
CAS statement (PDF)                    AMC disclosure files (XLSX)
        |                                          |
   parse, FIFO lots                        parse, reconcile, resolve
   XIRR / TWRR                             to a company master
   reconcile vs the                                |
   statement's own balance                 issuer weights per scheme
        |                                          |
        +--------------- look-through -------------+
                              |
              exposure, overlap, concentration, duplication
                              |
                     six views in a browser
```

- **Ledger** — FIFO lots (statutory for Indian MF units), realised/unrealised gains,
  §112A grandfathering, XIRR, TWRR and the timing effect between them. Every folio
  reconciles against the closing balance printed on the statement, and a NAV cross-check
  catches the classic Direct-vs-Regular mis-resolution that unit counts cannot.
- **Disclosure parsing** — one configurable table reader with per-AMC configs, driven by
  header text rather than column position. HDFC and Nippon load real published files.
- **Look-through** — exposure by *issuer*, not by ISIN, so a company's equity and its
  bonds count as one exposure. Closure is asserted before any result is returned: the sum
  of exposures equals the sum of position values, or it raises.
- **Views** — a local web UI with as-of date, staleness, coverage and unresolved share
  under every single chart, in a footer that cannot be switched off.

### Honest status

This is a working system, not a finished product.

| | |
|---|---|
| AMC formats parsing **real** files | 2 of 5 (HDFC, Nippon) |
| Held schemes with a loaded disclosure | 1 of 3 |
| Modules built | M0 data, M1 ledger, M3 look-through, M6 views |
| Modules not built | M2 fund analytics, M4 risk, M5 market |
| Tax engine | not built — rates live in a human-verified config and are never invented |
| Has anyone actually used it | **no** |

Five of the eleven portfolio views in the spec are deliberately *absent* rather than
stubbed, because each needs a module that does not exist and a view that always renders
empty is a broken feature pretending to be a data problem.

---

## Getting started

Requires **Python 3.11+**.

```bash
git clone https://github.com/harshilsaini26/Mutual-Fund-Portfolio-Analyser.git
cd Mutual-Fund-Portfolio-Analyser
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e .
```

`sqlcipher3` is a build dependency for the encrypted personal ledger and needs SQLCipher
present on your system. On Debian/Ubuntu `apt install libsqlcipher-dev`, on macOS
`brew install sqlcipher`.

Importing a CAS statement needs two more, kept optional because the PDF libraries are
large and most of the tool never touches a PDF:

```bash
pip install -e ".[cas]"
```

### Run the test suite

The checks need ruff, mypy and pytest, which are not runtime dependencies. Install them
with the `dev` extra — the versions are pinned there so that your `ruff check` and CI's
mean the same thing:

```bash
pip install -e ".[dev]"
```

Everything below runs with no network and no data files, and is exactly what
[the gate](.github/workflows/ci.yml) runs on every push.

```bash
python -m pytest -q                                   # 881 tests
python -m ruff check src/ tests/ scripts/ jobs/
python -m mypy                                        # strict, 164 files
python -m scripts.verify_v0_ledger --check            # exits 1 on golden-file drift
```

### Configuration

Five environment variables, all optional except the first when fetching. There is no
`.env` loader — these are read straight from the environment, so export them or prefix the
command.

| Variable | Default | What it does |
|---|---|---|
| `MF_CONTACT_EMAIL` | `unset@example.invalid` | Sent in the `From:` header of every outbound request. Set it before fetching anything. |
| `MF_DATA_ROOT` | `./data` | Where the warehouse, the ledger and the raw archive live. |
| `MF_WAREHOUSE` | `$MF_DATA_ROOT/warehouse/canonical.db` | Points at a specific warehouse file — useful for keeping a full one and a slimmed one side by side. |
| `MF_LEDGER` | `$MF_DATA_ROOT/ledger/personal.db` | The encrypted personal ledger. |
| `MF_CAS_PASSWORD` | unset | Read **only** by the optional real-CAS test, which cannot prompt. Everything else asks interactively. |

### Load market data

Set a contact address first. Every outbound request identifies itself in a
`From:` header, per RFC 7231 — the data sources here are public but they are not yours,
and a scraper that will not say who it is has no business running.

```bash
export MF_CONTACT_EMAIL=you@example.com

python -m jobs.fetch_nav                              # today's NAVs, all schemes
python -m jobs.backfill_nav --amc hdfc --from 2024-01-01
python -m jobs.build_entity_master                    # AMFI market-cap list -> issuers
python -m jobs.load_holdings --amc hdfc               # a disclosure, end to end
```

Every fetch is rate-limited per domain, respects `robots.txt`, uses conditional GET, and
archives the raw bytes under their own SHA-256 before anything parses them. Re-running any
job is safe: the archive is content-addressed and the loads are upserts.

### Import a statement and look through it

```bash
python -m jobs.import_cas --file statement.pdf --user USER-01
python -m scripts.show_lookthrough                    # terminal report
python -m jobs.serve                                  # the browser UI, 127.0.0.1:8765
```

Both prompt for passwords rather than reading them from the environment — the CAS
password, then the key to the encrypted ledger.

No statement to hand? `python -m scripts.show_lookthrough --equal 1000000` values every
disclosed scheme equally so you can see the shape of the output. It labels itself
`ILLUSTRATIVE — NOT your ledger` and refuses to persist, because a portfolio report that
is not your portfolio must never look like one.

---

## Your data

Two databases, deliberately separated.

**Zone B — the personal ledger.** Your transactions, units, folios and positions. SQLCipher
encrypted at rest; the connection helper *refuses to open it* without a key rather than
quietly degrading. Gitignored in full, and it was gitignored before the first import ever
ran, because a secret committed once stays in history.

**Zone A — the warehouse.** NAVs, scheme master, company master, published fund
disclosures. All of it public information about funds, none of it about you.

Nothing here calls home. The one JavaScript dependency (d3, for the Sankey) is vendored
into the repository at a pinned version rather than loaded from a CDN, so the UI works
offline and no third party learns when you look at your portfolio. `SHA256SUMS` in that
directory records what was vendored, and a test fails if a file stops matching. There is
no telemetry, no account, and no server beyond the one you start yourself on loopback.

**The untrusted input is the disclosure files.** They are downloaded from AMC websites,
and an instrument name in one is a string that reaches both a web page and a spreadsheet.
Both sinks are handled: names are escaped before they are embedded in the page's JSON
(`</script>` in a name would otherwise close the element), a cell that a spreadsheet would
execute as a formula is prefixed so it stays text, and every response carries a
`Content-Security-Policy` that would block an injected script even if the escaping
regressed. The ledger file is created `0600`. See `tests/unit/test_security.py`, where
each of those is a regression test written against a demonstrated exploit.

---

## Layout

```
src/common/           Decimal + SQLite discipline, frozen contracts, types
src/m0_data/          fetch, parse, resolve, validate, load        (the warehouse)
src/m1_ledger/        CAS parsing, FIFO lots, returns, reconcile   (your positions)
src/m3_lookthrough/   exposure, overlap, concentration, duplication
src/m6_views/         envelope, builders, formatting, export, API, templates
src/m2_fund/ m4_risk/ m5_market/     contracts only — not built
jobs/                 the things you run
scripts/              fixture generation and the V0 verifier
migrations/           numbered, forward-only; zone_b/ is the encrypted half
docs/                 the specification corpus and the decision log
tests/                867 tests, hermetic
```

**One-way dependencies.** `M0 -> M1 -> M2 -> M3 -> M4/M5 -> M6`. Modules talk through
Protocol interfaces and never reach across a boundary with SQL. The view layer performs no
financial computation at all — a static check over the builders enforces it, because a
number derived in a view is a second source of truth nobody can reconcile.

---

## Documentation

`docs/` holds the specification this was built from — roughly 10,000 lines written before
the code, one module spec per module, plus:

- **`PLAN.md`** — scope, the vertical slices, and the acceptance gate for each.
- **`DECISIONS.md`** — 68 architecture decision records, append-only. Every departure from
  the spec, every defect the specs themselves contained, and what was decided instead.
  This is the most useful file in the repository if you want to know *why* anything is the
  way it is.
- **`CLAUDE.md`** — the ten non-negotiable invariants and the working agreement.

Two documents cited throughout the code are **not** published: `BUILD_ORDER.md`, a
dependency-graph analysis of the spec corpus whose recommendations are cited as `R1`-`R6`,
and `PROGRESS.md`, a working session log. Citations to them appear in docstrings and ADRs.
The reasoning each carries is restated where it is cited, so nothing depends on opening
them.

---

## Testing

867 tests, all hermetic — no network, no database beyond temporary files.

The ledger and the look-through engine are **mutation tested** (M1 26/26, M3 35/35
killed), because those two carry the correctness gates and a test suite that has never
been shown to catch a deliberate defect has not been shown to catch anything. Parsers are
not mutation tested; they get a golden fixture from a real published file, a format
cross-product test, and a schema-drift check.

A separate verifier recomputes the golden portfolio independently and fails on any drift.
It imports nothing from `src/`, on purpose.

The suite needs **no network and no data**: on a bare checkout it is 879 passed, 6
skipped, where the five are the ones that want a real warehouse or a real password-
protected statement and skip cleanly rather than failing. GitHub Actions runs exactly the
four commands above on every push.

---

## Built with Claude

Every line of this — the specification corpus, the implementation, the tests, and the
decision log — was written with [Claude Code](https://claude.com/claude-code). The
decision records are worth reading as a record of that: they include the defects found in
the specs by implementing them, the several occasions where a passing test turned out to
be asserting the wrong thing, and the bugs the mutation harness found in fixes that had
just been written.

---

## Status and licence

Personal project, published as-is. It is not investment advice, it is not audited, and it
has not been used in anger by anyone including its author. Every figure it renders carries
its own as-of date, staleness and coverage precisely so you can judge how much to trust
it — which, for now, should be "not with money that matters".

Licensed under the [MIT Licence](LICENSE).
