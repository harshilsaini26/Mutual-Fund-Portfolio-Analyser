"""Staged rows -> Zone A. MODULE_0.md §3 (L1 -> L2) and §4.4-4.5.

Every write carries `source_file_id`, so `PLAN.md` §4.2 holds: any number in the
warehouse traces back to a row in an archived file whose bytes are on disk under
their own hash.

Loads are idempotent by construction — `INSERT ... ON CONFLICT DO UPDATE` keyed
on the natural key — because the daily job re-reads a file that overlaps
everything already loaded, exactly as the CAS importer does (V0-15). Re-running
a load must not create a second anything.
"""

from __future__ import annotations

import re
import sqlite3
from collections import defaultdict
from datetime import date

from src.m0_data.parse.nav.amfi import AmfiParseResult, StagedNav, StagedScheme


def normalise_amc_id(amc_name: str) -> str:
    """`HDFC Mutual Fund` -> `hdfc`. MODULE_0.md §7.4.

    The trailing "Mutual Fund" is dropped because it is on every one of them and
    carries no information; what remains is the house name, which is what a
    person means by the AMC.
    """
    text = amc_name.strip().lower()
    text = re.sub(r"\bmutual\s+fund\b", "", text)
    text = re.sub(r"\basset\s+management(\s+(company|ltd\.?|limited))?\b", "", text)
    text = re.sub(r"[^a-z0-9]+", "_", text).strip("_")
    return text or "unknown"


def scheme_id_collisions(schemes: list[StagedScheme]) -> dict[str, list[StagedScheme]]:
    """Distinct source rows that would land on the same `scheme_id`.

    §4.4 assumes an ISIN identifies exactly one scheme. AMFI's own file breaks
    that: five ISINs appear against two scheme codes with **different scheme
    names** — `INF204KB1XN0` is both `Nippon India Fixed Horizon Fund XXXVI-
    Series 9` and `... XXXVII- Series 9`, and there are similar pairs among the
    wound-up Reliance FMPs.

    An upsert resolves those by letting the last row win, silently, which
    `CLAUDE.md` invariant 4 forbids — the losing scheme is dropped and nothing
    records it. The collisions are returned so the caller can count them into
    `job_run.warnings` and the job reports `partial` rather than `ok`.

    Rows that agree on name, plan and option are not collisions: they are the
    same scheme described twice, and collapsing them loses nothing.
    """
    by_id: dict[str, list[StagedScheme]] = defaultdict(list)
    for s in schemes:
        by_id[s.scheme_id].append(s)
    return {
        scheme_id: rows
        for scheme_id, rows in by_id.items()
        if len({(r.scheme_name, r.plan, r.option) for r in rows}) > 1
    }


def load_schemes(
    conn: sqlite3.Connection,
    schemes: list[StagedScheme],
    source_file_id: str | None,
    seen_on: date,
) -> tuple[int, int]:
    """Upsert `amc` and `scheme`. Returns (amc_rows, scheme_rows).

    `first_seen` is preserved on conflict and `last_seen` advances: the pair is
    how a wound-up scheme is detected later without deleting it, and §4.4 is
    explicit that schemes are never deleted — doing so puts survivorship bias
    into every historical comparison.

    Identity fields (name, plan, option) are updated on conflict rather than
    left alone. AMFI corrects spellings, and the scheme_id is the ISIN, so the
    row is the same scheme either way; refusing the correction would pin the
    warehouse to whatever the first file happened to say.
    """
    amcs = {normalise_amc_id(s.amc_name): s.amc_name for s in schemes}
    for amc_id, amc_name in sorted(amcs.items()):
        conn.execute(
            "INSERT INTO amc (amc_id, amc_name) VALUES (?, ?) "
            "ON CONFLICT(amc_id) DO UPDATE SET amc_name = excluded.amc_name",
            (amc_id, amc_name),
        )

    for s in schemes:
        conn.execute(
            """
            INSERT INTO scheme (
                scheme_id, amfi_code, isin, amc_id, scheme_name, plan, option,
                option_raw, sebi_category, first_seen, last_seen, source_file_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(scheme_id) DO UPDATE SET
                amfi_code      = excluded.amfi_code,
                isin           = excluded.isin,
                amc_id         = excluded.amc_id,
                scheme_name    = excluded.scheme_name,
                plan           = excluded.plan,
                option         = excluded.option,
                option_raw     = excluded.option_raw,
                sebi_category  = excluded.sebi_category,
                last_seen      = excluded.last_seen,
                source_file_id = excluded.source_file_id
            """,
            (
                s.scheme_id, s.amfi_code, s.isin, normalise_amc_id(s.amc_name),
                s.scheme_name, s.plan, s.option, s.option_raw, s.sebi_category,
                seen_on, seen_on, source_file_id,
            ),
        )
    return len(amcs), len(schemes)


def load_navs(
    conn: sqlite3.Connection, navs: list[StagedNav], source_file_id: str | None
) -> int:
    """Upsert `nav_daily`. `nav_adj` is left null here and built by §9.1.

    A NAV that is re-published for a date already loaded overwrites it. AMFI
    does restate, and the later file is the correction — keeping the first
    value would make the warehouse disagree with the source it cites.
    """
    for n in navs:
        conn.execute(
            """
            INSERT INTO nav_daily (scheme_id, nav_date, nav, source_file_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(scheme_id, nav_date) DO UPDATE SET
                nav = excluded.nav, source_file_id = excluded.source_file_id
            """,
            (n.scheme_id, n.nav_date, n.nav, source_file_id),
        )
    return len(navs)


def load_parse_result(
    conn: sqlite3.Connection,
    result: AmfiParseResult,
    source_file_id: str | None,
    seen_on: date,
) -> dict[str, int]:
    """Load a whole parse, schemes before NAVs.

    Order matters: `nav_daily.scheme_id` references `scheme`, so a NAV row for
    an unknown scheme would violate the foreign key. That is the right way
    round — a NAV with no scheme is not a fact anyone can use.
    """
    collisions = scheme_id_collisions(result.schemes)
    for scheme_id, rows in collisions.items():
        codes = ", ".join(sorted(r.amfi_code for r in rows))
        result.warnings.append(
            (rows[0].row_number, f"scheme_id {scheme_id} claimed by codes {codes}")
        )

    amc_rows, scheme_rows = load_schemes(conn, result.schemes, source_file_id, seen_on)
    nav_rows = load_navs(conn, result.navs, source_file_id)
    return {
        "amc": amc_rows,
        "scheme": scheme_rows,
        "nav_daily": nav_rows,
        "collisions": len(collisions),
        "warnings": len(result.warnings) + len(result.unparsed),
    }
