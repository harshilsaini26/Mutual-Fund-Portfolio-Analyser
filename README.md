# Mutual Fund Portfolio Analyser

[![gate](https://github.com/harshilsaini26/Mutual-Fund-Portfolio-Analyser/actions/workflows/ci.yml/badge.svg)](https://github.com/harshilsaini26/Mutual-Fund-Portfolio-Analyser/actions/workflows/ci.yml)
[![licence: MIT](https://img.shields.io/badge/licence-MIT-blue.svg)](LICENSE)
[![python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue.svg)](https://www.python.org/downloads/)

Self-hosted look-through analytics for Indian mutual funds. It dissolves the funds you
hold into the companies you actually own: how much of two funds is the same stock, how
much of your money is one company bought twice, and how concentrated you really are once
the wrappers come off.

It runs on your machine. Your transactions never leave it.

---

## The problem

Five equity funds look diversified. Opened up they are frequently one fund bought five
times — the same twenty large caps in slightly different proportions, with five sets of
fees. No factsheet tells you this, because a factsheet describes one fund and overlap is a
property of the *set*.

On two real disclosures this codebase reports:

```
HDFC Flexi Cap  x  Nippon India Growth Mid Cap
    overlap        13.32%          17 shared companies of 156
    duplicated     6.66% of the portfolio held through more than one fund
```

That is the whole product in three numbers.

---

## Why this isn't a spreadsheet

Most of the engineering here is not the arithmetic. It is refusing to produce a number
that looks right and is not.

- **`Decimal` end to end**, floats only inside the XIRR solver. SQLite is the trap: a
  column declared `DECIMAL` takes NUMERIC affinity and silently stores money as a float.
  Every decimal column is `DECIMAL_TEXT`, enforced by a schema check. Three more cost a
  defect each to find — `SUM()` coerces the *aggregate* to a float, `ORDER BY` sorts as
  text so `"5000"` outranks `"25000"`, and any SQL arithmetic promotes through a REAL.
- **Nothing is silently dropped.** Unidentifiable issuers become `__UNRESOLVED__`, funds
  with no published portfolio `__NO_DISCLOSURE__`. Both stay visible, pinned outside the
  top-N so tail aggregation cannot fold them away, and the unresolved percentage is a gate
  criterion rather than a log line.
- **Absent is not zero.** An uncomputed XIRR renders as an em dash, never `0.0%` — one is
  an admission, the other a claim. Gini is `NULL` when a short position puts a negative
  weight in the pool.
- **Every parse reconciles against the file's own stated total.** A parser tuned to one
  AMC over-counts another by 2.9x; no list of section labels catches that, arithmetic
  does. More than 2% from the publisher's own figure refuses to load.
- **Raise, don't clamp.** A redemption for more units than the book holds means a missing
  statement, not a number to round down.

---

## What it does

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

- **Ledger** — FIFO lots, realised/unrealised gains, §112A grandfathering, XIRR and TWRR.
  Every folio reconciles against the statement's own closing balance, and a NAV
  cross-check catches the Direct-vs-Regular mis-resolution that unit counts cannot.
- **Disclosure parsing** — one configurable table reader, per-AMC configs, driven by header
  text rather than column position.
- **Look-through** — exposure by *issuer*, not ISIN, so a company's equity and its bonds
  count once. Closure is asserted before any result returns: Σ exposures equals Σ position
  values, or it raises.
- **Views** — a local web UI with as-of date, staleness, coverage and unresolved share
  under every chart, in a footer that cannot be switched off.

### Honest status

A working system, not a finished product.

| | |
|---|---|
| AMC formats parsing **real** files | 2 of 5 (HDFC, Nippon) |
| Held schemes with a loaded disclosure | 1 of 3 |
| Modules built | M0 data, M1 ledger, M3 look-through, M6 views |
| Not built | M2 fund analytics, M4 risk, M5 market, tax engine |
| Has anyone actually used it | **no** |

Five of the eleven portfolio views in the spec are deliberately *absent* rather than
stubbed: each needs a module that does not exist, and a view that always renders empty is
a broken feature pretending to be a data problem.

---

## Getting started

Python 3.11+, plus SQLCipher for the encrypted ledger — `apt install libsqlcipher-dev` on
Debian/Ubuntu, `brew install sqlcipher` on macOS.

```bash
git clone https://github.com/harshilsaini26/Mutual-Fund-Portfolio-Analyser.git
cd Mutual-Fund-Portfolio-Analyser
python -m venv .venv && source .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev,cas]" -c requirements.lock
```

`requirements.lock` pins the entire graph, transitive packages included; CI installs with
it and fails if anything resolves that the lock does not mention. The extras are optional:
`cas` for PDF import, `dev` for the checks below.

### The gate

```bash
python -m pytest -q                                   # 882 tests, hermetic
python -m ruff check src/ tests/ scripts/ jobs/
python -m mypy                                        # strict, 164 files
python -m scripts.verify_v0_ledger --check            # exits 1 on golden-file drift
```

No network, no data files. [CI](.github/workflows/ci.yml) runs exactly these on every push.

### Use it

```bash
export MF_CONTACT_EMAIL=you@example.com               # sent in From:, per RFC 7231

python -m jobs.fetch_nav                              # today's NAVs, all schemes
python -m jobs.build_entity_master                    # AMFI market-cap list -> issuers
python -m jobs.load_holdings --amc hdfc               # a disclosure, end to end

python -m jobs.import_cas --file statement.pdf --user USER-01
python -m jobs.serve                                  # browser UI on 127.0.0.1:8765
```

Passwords are always prompted, never read from the environment. Every fetch is
rate-limited per domain, respects `robots.txt`, uses conditional GET, and archives the raw
bytes under their SHA-256 before anything parses them; re-running any job is safe.

No statement to hand? `python -m scripts.show_lookthrough --equal 1000000` values every
disclosed scheme equally so you can see the shape of the output. It labels itself
`ILLUSTRATIVE — NOT your ledger` and refuses to persist.

### Configuration

Five environment variables, all optional except the first when fetching. There is no
`.env` loader — export them or prefix the command.

| Variable | Default | What it does |
|---|---|---|
| `MF_CONTACT_EMAIL` | `unset@example.invalid` | `From:` header on every outbound request |
| `MF_DATA_ROOT` | `./data` | where warehouse, ledger and raw archive live |
| `MF_WAREHOUSE` | `$MF_DATA_ROOT/warehouse/canonical.db` | points at a specific warehouse file |
| `MF_LEDGER` | `$MF_DATA_ROOT/ledger/personal.db` | the encrypted personal ledger |
| `MF_CAS_PASSWORD` | unset | the optional real-CAS test only; everything else prompts |

---

## Your data

**Zone B — the personal ledger.** Transactions, units, folios, positions. SQLCipher
encrypted at rest and created `0600`; the connection helper *refuses to open it* without a
key rather than quietly degrading. Gitignored since before the first import ever ran,
because a secret committed once stays in history.

**Zone A — the warehouse.** NAVs, scheme and company master, published disclosures. All
public information about funds, none of it about you.

Nothing calls home. d3, the one JavaScript dependency, is vendored at a pinned version
with a recorded `SHA256SUMS` rather than loaded from a CDN. No telemetry, no account, no
server beyond the one you start on loopback.

**The untrusted input is the disclosure files** — an instrument name in one reaches both a
web page and a spreadsheet. Names are escaped before embedding in the page's JSON, cells a
spreadsheet would execute as formulas are prefixed to stay text, and every response
carries a CSP that blocks injected script even if the escaping regressed. Each is a
regression test in `tests/unit/test_security.py`, written against a demonstrated exploit.

---

## Layout

```
src/common/           Decimal + SQLite discipline, frozen contracts, types
src/m0_data/          fetch, parse, resolve, validate, load        (the warehouse)
src/m1_ledger/        CAS parsing, FIFO lots, returns, reconcile   (your positions)
src/m3_lookthrough/   exposure, overlap, concentration, duplication
src/m6_views/         envelope, builders, formatting, export, API, templates
src/m2_fund/ m4_risk/ m5_market/     contracts only — not built
jobs/   scripts/   migrations/   docs/   tests/
```

**One-way dependencies:** `M0 -> M1 -> M2 -> M3 -> M4/M5 -> M6`, through Protocol
interfaces, never reaching across a boundary with SQL. The view layer performs no
financial computation at all — a static check over the builders enforces it, because a
number derived in a view is a second source of truth nobody can reconcile.

The ledger and look-through engine are **mutation tested** (M1 26/26, M3 35/35 killed) —
they carry the correctness gates, and a suite never shown to catch a deliberate defect has
not been shown to catch anything. A separate verifier recomputes the golden portfolio and
imports nothing from `src/`, on purpose.

---

## Documentation

`docs/` holds the roughly 10,000-line specification this was built from — written before
the code, one spec per module — plus:

- **`PLAN.md`** — scope, the vertical slices, and the acceptance gate for each.
- **`DECISIONS.md`** — append-only architecture decision records: every departure from
  the spec, every defect the specs themselves contained, and what was decided instead. The
  most useful file here if you want to know *why* anything is the way it is.
- **`CLAUDE.md`** — the ten non-negotiable invariants and the working agreement.

---

## Built with Claude

Every line — specification, implementation, tests and decision log — was written with
[Claude Code](https://claude.com/claude-code). The decision records double as a record of
that: defects found in the specs by implementing them, passing tests that turned out to
assert the wrong thing, and bugs the mutation harness found in freshly written fixes.

---

## Status and licence

Personal project, published as-is. Not investment advice, not audited, and not used in
anger by anyone including its author. Every figure carries its own as-of date, staleness
and coverage precisely so you can judge how much to trust it — which, for now, should be
"not with money that matters".

Licensed under the [MIT Licence](LICENSE).
