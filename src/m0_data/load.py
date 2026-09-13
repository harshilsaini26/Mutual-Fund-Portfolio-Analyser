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
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal

from src.m0_data.parse.holdings.base import READER_VERSION
from src.m0_data.parse.mcap.amfi import McapParseResult
from src.m0_data.parse.nav.amfi import AmfiParseResult, StagedNav, StagedScheme
from src.m0_data.resolve.cascade import RESOLVER_VERSION


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
                s.scheme_id,
                s.amfi_code,
                s.isin,
                normalise_amc_id(s.amc_name),
                s.scheme_name,
                s.plan,
                s.option,
                s.option_raw,
                s.sebi_category,
                seen_on,
                seen_on,
                source_file_id,
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


def load_navs_where_absent(
    conn: sqlite3.Connection, navs: list[StagedNav], source_file_id: str | None
) -> int:
    """Insert only the dates not already on record. Returns rows actually added.

    The counterpart to `load_navs`, and the difference is which source wins.
    `load_navs` upserts because AMFI restates and the later publisher file is
    the correction. This one is for a **mirror** (S6, mfapi): it may fill dates
    the publisher's own export did not reach, but it must never overwrite a
    value AMFI stated.

    That is not hypothetical. Measured on HDFC Flexi Cap, mfapi and AMFI agree
    on 2,116 of 2,117 overlapping dates and disagree on 2026-03-12 —
    `2111.846` against `2111.779`. One in two thousand, and precisely the size
    of discrepancy that moves an XIRR without moving anything a reader would
    notice. `DO NOTHING` keeps the publisher's number. DECISIONS V1-19.
    """
    added = 0
    for n in navs:
        cursor = conn.execute(
            """
            INSERT INTO nav_daily (scheme_id, nav_date, nav, source_file_id)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(scheme_id, nav_date) DO NOTHING
            """,
            (n.scheme_id, n.nav_date, n.nav, source_file_id),
        )
        # SQLite reports -1 when the ON CONFLICT clause skipped the row.
        added += max(cursor.rowcount, 0)
    return added


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


# --- entity layer (MODULE_0.md §4.3) ----------------------------------------


def issuer_id_for(isin: str) -> str:
    """One issuer per ISIN, at seed time.

    A real security master would map many ISINs to one issuer — equity, prefs
    and NCDs of the same company are one exposure (§8.1). AMFI's list is one
    row per listed company, so at seed time the two coincide, and the
    `instrument.issuer_id` indirection is what lets a later source collapse
    them without rewriting any holding.

    Prefixed rather than bare so a real issuer can never collide with a
    synthetic one: `__CASH__` and an ISIN share no namespace.
    """
    return f"AMFI:{isin}"


def load_mcap(
    conn: sqlite3.Connection,
    result: McapParseResult,
    source_file_id: str | None,
) -> dict[str, int]:
    """Seed `issuer`, `instrument` and the `amfi_mcap` classification.

    Classification is written **point-in-time** — `valid_from` is the list's
    own period end, never today. `CLAUDE.md` invariant 6 and §2.2 S4: applying
    the current list to a 2021 holding creates phantom drift or masks real
    drift, and the only defence is that the basis date travels with the value.

    Companies with no market cap get an issuer and an instrument but no
    classification. They are real securities that simply cannot be ranked.
    """
    counts = {"issuer": 0, "instrument": 0, "classification": 0}
    for row in result.rows:
        issuer_id = issuer_id_for(row.isin)
        conn.execute(
            "INSERT INTO issuer (issuer_id, canonical_name, is_listed, is_synthetic,"
            " source_file_id) VALUES (?, ?, 1, 0, ?)"
            " ON CONFLICT(issuer_id) DO UPDATE SET"
            " canonical_name = excluded.canonical_name,"
            " source_file_id = excluded.source_file_id",
            (issuer_id, row.company_name, source_file_id),
        )
        counts["issuer"] += 1

        conn.execute(
            "INSERT INTO instrument (isin, issuer_id, instrument_type, ticker_nse,"
            " ticker_bse, source_file_id) VALUES (?, ?, 'equity', ?, ?, ?)"
            " ON CONFLICT(isin) DO UPDATE SET"
            " issuer_id = excluded.issuer_id,"
            " ticker_nse = excluded.ticker_nse,"
            " ticker_bse = excluded.ticker_bse,"
            " source_file_id = excluded.source_file_id",
            (row.isin, issuer_id, row.nse_symbol, row.bse_symbol, source_file_id),
        )
        counts["instrument"] += 1

        if row.bucket is not None:
            conn.execute(
                "INSERT INTO issuer_classification (issuer_id, taxonomy, value,"
                " valid_from, source, source_file_id)"
                " VALUES (?, 'amfi_mcap', ?, ?, 'amfi', ?)"
                " ON CONFLICT(issuer_id, taxonomy, valid_from) DO UPDATE SET"
                " value = excluded.value, source_file_id = excluded.source_file_id",
                (issuer_id, row.bucket, result.basis_date, source_file_id),
            )
            counts["classification"] += 1
    return counts


# --- holdings (MODULE_0.md §4.6) --------------------------------------------


def next_revision(
    conn: sqlite3.Connection,
    scheme_id: str,
    as_of: date,
    source_file_id: str,
    resolver_version: str = RESOLVER_VERSION,
    parser_version: str = READER_VERSION,
) -> int | None:
    """§4.6. A RESTATED disclosure is a new revision. A re-run is not.

    `CLAUDE.md` invariant 2: never UPDATE a fact row. AMCs do restate — a
    correction, or a file re-uploaded after a formatting fix — and the earlier
    version is still what we reported at the time, so it keeps its rows and
    loses `is_current`.

    But a revision must mean "the AMC published something different", not "the
    job ran twice". `source_file_id` is the sha256 of the bytes, so identical
    bytes already loaded return None and the caller skips — the same
    content-addressed idempotence the archive and every other loader has.
    Without this, a nightly re-run would stack revisions of an unchanged file
    until the revision number told you only how many times cron fired.

    **`resolver_version` is the other half of that sameness, and it was missing
    (V1-29).** `issuer_id` is derived, not disclosed, so identical bytes read by
    an improved cascade are a different disclosure as far as the warehouse is
    concerned. Skipping on the bytes alone meant a resolver fix could not reach
    a holding already loaded: the job reported the better figure and wrote
    nothing. Both must match for a run to be a repeat.
    """
    current = conn.execute(
        "SELECT revision, source_file_id, resolver_version, parser_version"
        " FROM holding_disclosure"
        " WHERE scheme_id=? AND as_of_date=? AND is_current=1",
        (scheme_id, as_of),
    ).fetchone()
    if (
        current
        and current[1] == source_file_id
        and current[2] == resolver_version
        and current[3] == parser_version
    ):
        return None

    row = conn.execute(
        "SELECT MAX(revision) FROM holding_disclosure WHERE scheme_id=? AND as_of_date=?",
        (scheme_id, as_of),
    ).fetchone()
    return (row[0] or 0) + 1


def load_holdings(
    conn: sqlite3.Connection,
    scheme_id: str,
    as_of: date,
    rows: list[dict[str, object]],
    header: dict[str, object],
    source_file_id: str,
    resolver_version: str = RESOLVER_VERSION,
    parser_version: str = READER_VERSION,
) -> dict[str, int]:
    """Write one disclosure and its rows, as a new revision.

    `rows` carry already-normalised values — market value in rupees absolute,
    `pct_normalised` summing to exactly 100, and a resolved `issuer_id`. The
    loader does no arithmetic: everything it writes was computed by a step that
    can be re-run without re-fetching (§3's layer invariant).
    """
    revision = next_revision(
        conn, scheme_id, as_of, source_file_id, resolver_version, parser_version
    )
    if revision is None:
        existing = conn.execute(
            "SELECT revision, row_count FROM holding_disclosure"
            " WHERE scheme_id=? AND as_of_date=? AND is_current=1",
            (scheme_id, as_of),
        ).fetchone()
        return {"holding": existing[1], "revision": existing[0], "skipped": 1}
    now = datetime.now(UTC)

    conn.execute(
        "UPDATE holding SET is_current = 0 WHERE scheme_id=? AND as_of_date=?",
        (scheme_id, as_of),
    )
    conn.execute(
        "UPDATE holding_disclosure SET is_current = 0 WHERE scheme_id=? AND as_of_date=?",
        (scheme_id, as_of),
    )

    for position, row in enumerate(rows, start=1):
        conn.execute(
            """
            INSERT INTO holding (
                scheme_id, as_of_date, revision, row_number, isin, issuer_id,
                instrument_raw_name, quantity, market_value, pct_to_nav,
                pct_normalised, instrument_class, credit_rating, reported_sector,
                resolution_method, resolution_conf, source_file_id, ingested_at,
                is_current
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 1)
            """,
            (
                scheme_id,
                as_of,
                revision,
                position,
                row.get("isin"),
                row["issuer_id"],
                row["instrument_raw_name"],
                row.get("quantity"),
                row["market_value"],
                row.get("pct_to_nav"),
                row["pct_normalised"],
                row["instrument_class"],
                row.get("credit_rating"),
                row.get("reported_sector"),
                row["resolution_method"],
                row.get("resolution_conf"),
                source_file_id,
                now,
            ),
        )

    conn.execute(
        """
        INSERT INTO holding_disclosure (
            scheme_id, as_of_date, revision, source_file_id, row_count,
            pct_sum_raw, weight_residual, unresolved_mv_pct, total_mv,
            aum_reported, mv_vs_aum_pct, reported_unit, validation_status,
            validation_notes, resolver_version, parser_version, source_tier,
            is_current, ingested_at
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?, 1, ?)
        """,
        (
            scheme_id,
            as_of,
            revision,
            source_file_id,
            len(rows),
            header.get("pct_sum_raw"),
            header.get("weight_residual"),
            header["unresolved_mv_pct"],
            header["total_mv"],
            header.get("aum_reported"),
            header.get("mv_vs_aum_pct"),
            header.get("reported_unit"),
            header["validation_status"],
            header.get("validation_notes"),
            resolver_version,
            parser_version,
            # V1-43. Defaulted rather than required: every caller before the
            # coverage tier existed was reading an AMC's own file, and a
            # missing tier meaning `amc_direct` keeps those callers honest
            # instead of making them all say so.
            header.get("source_tier", "amc_direct"),
            now,
        ),
    )
    return {"holding": len(rows), "revision": revision}


#: How old an AUM witness may be before it stops being one.
#:
#: AAUM is quarterly and arrives about ten days after a quarter ends, so a
#: witness is normally 0-100 days old and two quarters is already unusual. A
#: year means four quarters have been published and none was loaded, at which
#: point the figure is not describing the fund V2 is checking. Refusing is
#: better than passing: V2 records "no AUM on record" and says so, where a
#: stale witness silently widens or narrows a check nobody knows is degraded.
WITNESS_MAX_AGE_DAYS = 365


@dataclass(frozen=True)
class AumWitness:
    """A scheme's AUM from OUTSIDE the disclosure being checked. V1-50.

    Three fields, not a bare number, because V2 needs all three and a caller
    left to infer any of them infers wrong:

    - `amount` is rupees absolute.
    - `basis` decides the tolerance. AMFI's quarterly average sits ~10% from a
      month-end portfolio through ordinary market movement where a point-in-time
      balance sits within 3%, and applying the wrong one gives either a false
      quarantine or a check that has quietly stopped checking (V1-49).
    - `as_of` is how old it is, which `basis` alone does not say. A figure can
      be the right KIND of number and still be two quarters out of date.
    """

    amount: Decimal
    basis: str
    as_of: date

    def age_days(self, at: date) -> int:
        return (at - self.as_of).days


def aum_for(conn: sqlite3.Connection, scheme_id: str, as_of: date) -> AumWitness | None:
    """The scheme's own AUM on or before `as_of`, for §10's V2.

    V2 is THE units check -- §7.2's 100x error fails it by two orders of
    magnitude -- and it only runs when there is an AUM to reconcile against.
    That makes this the one witness independent of the disclosure itself, so it
    lives beside the loader rather than inside one job: `jobs/fetch_groww.py`
    passed `None` here for a whole slice and disabled V2 on the very path where
    the market-value unit is ASSERTED rather than read from a header.

    **Bounded by `WITNESS_MAX_AGE_DAYS`.** The first version took the newest row
    on or before the date with no floor at all, so a 2027 disclosure would have
    reconciled against a June 2026 average -- nine months of market movement --
    at a tolerance chosen for one quarter of drift, and `validation_notes` would
    have recorded only `(quarterly_average)` with no hint of the age.

    None when `scheme_aum` has not been built or holds nothing recent enough,
    which V2 records as "no AUM on record" rather than treating as a pass.
    """
    if not _has_table(conn, "scheme_aum"):
        return None
    row = conn.execute(
        "SELECT aum_inr, basis, as_of_date FROM scheme_aum"
        " WHERE scheme_id=? AND as_of_date<=? AND aum_inr IS NOT NULL"
        " ORDER BY as_of_date DESC LIMIT 1",
        (scheme_id, as_of),
    ).fetchone()
    if not row:
        return None

    witness_as_of = (
        row[2] if isinstance(row[2], date) else date.fromisoformat(str(row[2]))
    )
    if (as_of - witness_as_of).days > WITNESS_MAX_AGE_DAYS:
        return None
    return AumWitness(row[0], str(row[1]), witness_as_of)


def _has_table(conn: sqlite3.Connection, name: str) -> bool:
    return bool(
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
    )
