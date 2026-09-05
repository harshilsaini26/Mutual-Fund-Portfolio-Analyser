"""AMFI NAV parsing and loading. MODULE_0.md §2.2, §6, §7.

`tests/fixtures/m0/navall_sample.txt` is a **trimmed slice of the real file**,
not an imitation of it — `CLAUDE.md`: *"prefer a real file over a better
simulation."* Every line in it was published by AMFI on 2026-09-04. It carries
each trap §2.2 names, and one it does not.

No test here touches the network.
"""

from __future__ import annotations

from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.m0_data.derive.nav_adj import build_all_nav_adj
from src.m0_data.load import (
    load_parse_result,
    normalise_amc_id,
    scheme_id_collisions,
)
from src.m0_data.normalise.numbers import CoercionError, to_date, to_decimal
from src.m0_data.parse.nav.amfi import (
    AmfiParseError,
    AmfiParseResult,
    column_map,
    normalise_option,
    normalise_plan,
    parse_navall,
)
from src.m0_data.resolve.isin import is_valid_isin
from src.m0_data.schema.apply import (
    apply_migrations,
    assert_schema_is_decimal_safe,
    migration_files,
)

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "m0" / "navall_sample.txt"
AS_OF = date(2026, 9, 4)

HDFC_DIRECT = "INF179K01UT0"
HDFC_REGULAR = "INF179K01608"


@pytest.fixture(scope="module")
def lines() -> list[str]:
    return SAMPLE.read_text(encoding="utf-8").splitlines()


@pytest.fixture(scope="module")
def parsed(lines: list[str]) -> AmfiParseResult:
    return parse_navall(lines)


# --- §7.1 coercion ----------------------------------------------------------


def test_indian_grouping_survives_naive_comma_stripping() -> None:
    """§7.1 is explicit that locale parsing breaks on lakh/crore grouping."""
    assert to_decimal("1,23,456.78") == Decimal("123456.78")
    assert to_decimal("2271.324") == Decimal("2271.324")


def test_an_absent_number_is_none_and_not_zero() -> None:
    """A NAV of zero and a NAV that was not published are different facts.

    Collapsing them would put a zero into a return series, which reads as a
    total loss rather than as a day the fund did not price.
    """
    for token in ("", "-", "--", "N.A.", "NA", "N/A", "NIL"):
        assert to_decimal(token) is None
    assert to_decimal("0") == Decimal(0)


def test_a_malformed_number_raises_rather_than_returning_none() -> None:
    """`CLAUDE.md` invariant 5. Silence here would drop a real NAV."""
    with pytest.raises(CoercionError):
        to_decimal("12.3.4")
    with pytest.raises(CoercionError):
        to_date("2026-09-04")  # AMFI publishes DD-MMM-YYYY, never ISO


# --- §2.2 the file's actual shape -------------------------------------------


def test_the_real_file_has_eight_columns_not_the_six_the_spec_describes(
    lines: list[str],
) -> None:
    """DECISIONS V0-20, pinned against real bytes.

    §2.2 describes `Scheme Code;ISIN Growth;ISIN Reinvestment;Scheme Name;NAV;Date`.
    The live file adds explicit Plan and Option columns, which is the single
    most valuable difference in this module: plan no longer has to be inferred
    from a scheme name, and V0-05 was possible only because it did.
    """
    header = lines[0]
    assert header.count(";") + 1 == 8
    assert "Plan;Option" in header


def test_plan_and_option_are_read_from_the_file_not_from_the_name(
    parsed: AmfiParseResult,
) -> None:
    """The V0-05 error class, closed at source.

    Both HDFC Flexi Cap plans carry the identical scheme name. Only the Plan
    column separates them — and their NAVs differ by 10.13%.
    """
    direct = next(s for s in parsed.schemes if s.scheme_id == HDFC_DIRECT)
    regular = next(s for s in parsed.schemes if s.scheme_id == HDFC_REGULAR)

    assert direct.scheme_name == regular.scheme_name == "HDFC Flexi Cap Fund"
    assert (direct.plan, regular.plan) == ("direct", "regular")
    assert direct.option == regular.option == "growth"


def test_the_two_plans_nav_gap_is_the_error_v0_05_recorded(
    parsed: AmfiParseResult,
) -> None:
    """DECISIONS V0-05 said 2,271.32 against 2,062.38 on 2026-09-04, 10.13% apart.

    That was read off a screenshot and a supplied workbook. Here are both
    numbers from AMFI's own file, on the same date — the fixture's scheme
    master is confirmed against the authoritative source rather than against
    the thing it was derived from.
    """
    by_id = {n.scheme_id: n for n in parsed.navs}
    direct, regular = by_id[HDFC_DIRECT], by_id[HDFC_REGULAR]

    assert direct.nav_date == regular.nav_date == AS_OF
    assert direct.nav == Decimal("2271.324")
    assert regular.nav == Decimal("2062.377")

    gap = (direct.nav - regular.nav) / regular.nav * 100
    assert Decimal("10.1") < gap < Decimal("10.2")


# --- §2.2's named traps -----------------------------------------------------


def test_the_file_is_sectioned_and_context_carries_onto_each_row(
    parsed: AmfiParseResult,
) -> None:
    """§2.2: blank lines and AMC-name lines interleave with data rows.

    Neither carries a delimiter, so a CSV reader produces garbage. The AMC and
    SEBI category live outside any row and have to be carried down onto it.
    """
    hdfc = next(s for s in parsed.schemes if s.scheme_id == HDFC_DIRECT)
    assert hdfc.amc_name == "HDFC Mutual Fund"
    assert {s.amc_name for s in parsed.schemes} == {
        "Axis Mutual Fund", "Nippon India Mutual Fund", "HDFC Mutual Fund",
        "IL&FS Mutual Fund (IDF)",
    }

    axis = next(s for s in parsed.schemes if s.amc_name == "Axis Mutual Fund")
    assert axis.sebi_category is not None
    assert "Children" in axis.sebi_category


def test_one_row_can_produce_two_schemes(parsed: AmfiParseResult) -> None:
    """§2.2's two-ISIN trap, on a real row.

    `118954;INF179K01VL5;INF179K01VM3;HDFC Flexi Cap Fund;Direct Plan;IDCW Option`
    describes a payout variant and a reinvestment variant. They have different
    ISINs and different NAV series, so §4.4 requires two scheme_ids — one per
    plan+option, no exceptions.

    In the live file 4,559 rows are shaped this way; it is a quarter of the
    file, not an edge case.
    """
    payout = next(s for s in parsed.schemes if s.scheme_id == "INF179K01VL5")
    reinvest = next(s for s in parsed.schemes if s.scheme_id == "INF179K01VM3")

    assert payout.amfi_code == reinvest.amfi_code == "118954"
    assert payout.option == "idcw_payout"
    assert reinvest.option == "idcw_reinvest"


def test_only_the_priced_variant_gets_a_nav_row(parsed: AmfiParseResult) -> None:
    """The row carries ONE NAV, and it belongs to the primary variant.

    Copying it onto the reinvestment scheme would fabricate a series — and
    fabricate it wrong, since a reinvestment NAV diverges from its payout
    sibling from the first distribution onward. Absence is correct here.
    """
    priced = {n.scheme_id for n in parsed.navs}
    assert "INF179K01VL5" in priced
    assert "INF179K01VM3" not in priced


def test_a_row_with_no_primary_isin_falls_back_to_the_amfi_code(
    parsed: AmfiParseResult,
) -> None:
    """§4.4: the ISIN where available, else `AMFI:{code}:{option}`.

    806 rows in the live file have `-` in the primary ISIN column. The option
    qualifier on the fallback is load-bearing: one AMFI code can describe two
    schemes, so the code alone is not a key.
    """
    fallback = next(s for s in parsed.schemes if s.scheme_id.startswith("AMFI:109472"))
    assert fallback.isin is None
    assert fallback.scheme_id == "AMFI:109472:idcw_payout"
    assert fallback.scheme_name == "Nippon India Corporate Bond Fund"


def test_the_same_name_plan_and_option_can_be_two_different_schemes(
    parsed: AmfiParseResult,
) -> None:
    """DECISIONS V0-21 — why §11.3's fuzzy name step is not implemented.

    Codes 135762 and 135764 are both `Axis Children's Fund / Direct Plan /
    Growth Option`, with NAVs 30.3228 and 30.9027 — the lock-in and no-lock-in
    variants. 1,467 such groups exist in the live file.

    A name match cannot separate them, so resolving on name would be choosing
    between two real schemes by coin flip.
    """
    triple = [
        s for s in parsed.schemes
        if s.scheme_name == "Axis Children's Fund"
        and s.plan == "direct" and s.option == "growth"
    ]
    assert len(triple) == 2
    assert len({s.amfi_code for s in triple}) == 2

    navs = {n.nav for n in parsed.navs if n.scheme_id in {s.scheme_id for s in triple}}
    assert navs == {Decimal("30.3228"), Decimal("30.9027")}


def test_nothing_in_the_body_is_silently_dropped(lines: list[str]) -> None:
    """Same guarantee V0-15 established for the CAS parser.

    A data row the state machine did not understand is a scheme missing from
    the master, and every NAV for it is missing too. Silence about that is the
    failure mode.
    """
    result = parse_navall(lines)
    assert result.unparsed == []

    # The only warnings are the invalid-ISIN rejections below, which are
    # reported rather than dropped — that is the guarantee, not silence.
    assert all("not a valid ISIN" in text for _, text in result.warnings)

    truncated = [*lines, "999999;INF000X01AA1;-;Truncated Row"]
    assert parse_navall(truncated).unparsed == [
        (len(truncated), "999999;INF000X01AA1;-;Truncated Row")
    ]


def test_a_file_that_is_not_a_nav_file_raises(lines: list[str]) -> None:
    """§2 requires loud failure, never a fallback guess."""
    with pytest.raises(AmfiParseError):
        parse_navall(["Some Fund House", "", "not a nav file at all"])


# --- normalisation ----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Growth Option", "growth"),
        ("IDCW Option", "idcw_payout"),
        ("IDCW-Re-investment", "idcw_reinvest"),
        ("MONTHLY DCW Payout", "idcw_payout"),
        ("QUARTERLY IDCW Payout", "idcw_payout"),
        ("DAILY IDCW Option", "idcw_payout"),
        ("", "unknown"),
    ],
)
def test_option_wording_collapses_to_the_three_the_schema_stores(
    raw: str, expected: str
) -> None:
    """The DDL allows three values; the file uses many more (V0-20).

    Reinvestment is tested before payout because `IDCW-Re-investment` contains
    neither `growth` nor `payout`, and a frequency-qualified reinvestment
    option would contain both.
    """
    assert normalise_option(raw) == expected


def test_an_unstated_plan_is_unknown_rather_than_regular() -> None:
    """Some schemes predate the Direct/Regular split and print nothing.

    Calling those Regular invents the distinction the column exists to record.
    """
    assert normalise_plan("Direct Plan") == "direct"
    assert normalise_plan("Regular Plan") == "regular"
    assert normalise_plan("") == "unknown"


def test_amc_names_normalise_to_a_stable_id() -> None:
    assert normalise_amc_id("HDFC Mutual Fund") == "hdfc"
    assert normalise_amc_id("Nippon India Mutual Fund") == "nippon_india"
    assert normalise_amc_id("Axis Mutual Fund") == "axis"


# --- schema and loading -----------------------------------------------------


def test_the_migrated_schema_cannot_silently_store_a_decimal_as_a_real(
    tmp_path: Path,
) -> None:
    """SZ-13, asserted against the schema rather than assumed.

    SQLite gives a `DECIMAL` column NUMERIC affinity and rewrites a decimal
    string as a REAL on write — silently, losing both exactness and trailing
    zeros. Zone A is SQLite by V0-19, so the trap applies here too.
    """
    db = tmp_path / "w.db"
    applied = apply_migrations(str(db))
    assert applied == [p.name for p in migration_files()]
    assert_schema_is_decimal_safe(str(db))

    conn = connect(str(db))
    declared = {
        row[1]: row[2]
        for row in conn.execute("PRAGMA table_info(nav_daily)")
    }
    assert declared["nav"] == "DECIMAL_TEXT"
    assert declared["nav_adj"] == "DECIMAL_TEXT"
    conn.close()


def test_migrations_are_idempotent(tmp_path: Path) -> None:
    db = tmp_path / "w.db"
    assert len(apply_migrations(str(db))) == len(migration_files())
    assert apply_migrations(str(db)) == []


def test_a_nav_round_trips_through_the_warehouse_without_losing_precision(
    tmp_path: Path, parsed: AmfiParseResult
) -> None:
    """The whole point of DECIMAL_TEXT, on a real published NAV.

    2271.324 must come back as `Decimal('2271.324')` — not 2271.3239999999998,
    and not the string '2271.324'.
    """
    conn = _warehouse(tmp_path, parsed)
    value = conn.execute(
        "SELECT nav FROM nav_daily WHERE scheme_id = ? AND nav_date = ?",
        (HDFC_DIRECT, AS_OF),
    ).fetchone()[0]
    assert isinstance(value, Decimal)
    assert value == Decimal("2271.324")
    assert str(value) == "2271.324"
    conn.close()


def test_loading_the_same_file_twice_writes_no_second_row(
    tmp_path: Path, parsed: AmfiParseResult
) -> None:
    """The daily file re-covers everything already loaded.

    Same property V0-15 established for CAS re-import, and the same reason: a
    job that is not idempotent turns routine operation into steady duplication.
    """
    conn = _warehouse(tmp_path, parsed)
    before = _counts(conn)
    load_parse_result(conn, parsed, "file-1", AS_OF)
    conn.commit()
    assert _counts(conn) == before
    conn.close()


def test_nav_adj_is_populated_even_with_no_idcw_events(
    tmp_path: Path, parsed: AmfiParseResult
) -> None:
    """§9.1: populate it anyway, so downstream has one code path.

    With no distributions the factor stays 1 and `nav_adj == nav`. Leaving it
    null would make every consumer ask which column to read, which is the
    question that produces the bug.
    """
    conn = _warehouse(tmp_path, parsed)
    build_all_nav_adj(conn)
    conn.commit()
    rows = conn.execute("SELECT nav, nav_adj FROM nav_daily").fetchall()
    assert rows
    assert all(nav_adj is not None for _nav, nav_adj in rows)
    assert all(nav == nav_adj for nav, nav_adj in rows)
    conn.close()


def _warehouse(tmp_path: Path, parsed: AmfiParseResult):  # type: ignore[no-untyped-def]
    db = tmp_path / "warehouse.db"
    apply_migrations(str(db))
    conn = connect(str(db))
    load_parse_result(conn, parsed, "file-1", AS_OF)
    conn.commit()
    return conn


def _counts(conn) -> dict[str, int]:  # type: ignore[no-untyped-def]
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in ("amc", "scheme", "nav_daily")
    }


# --- §8.3 ISIN validation ---------------------------------------------------


def test_the_literal_string_redeemed_is_not_accepted_as_an_isin(
    parsed: AmfiParseResult,
) -> None:
    """§8.3: reject malformed ISINs rather than creating garbage instruments.

    AMFI's own file puts the word `Redeemed` in the reinvestment ISIN column of
    nine wound-up IL&FS schemes, and `HDFCNIVODG` in one more. Taking those at
    face value keys nine unrelated funds to a single scheme called `Redeemed`,
    which then overwrite one another on load — a silent loss of eight schemes.

    A present-but-invalid ISIN is a warning, because the source printed
    something it should not have. An absent one is normal and silent.
    """
    assert not any(s.scheme_id == "Redeemed" for s in parsed.schemes)
    assert not any(s.isin == "Redeemed" for s in parsed.schemes)

    ilfs = [s for s in parsed.schemes if s.amc_name.startswith("IL&FS")]
    assert ilfs, "the fixture must carry the real IL&FS rows"
    assert all(s.isin is not None for s in ilfs), "their primary ISINs are valid"

    rejections = [t for _, t in parsed.warnings if "not a valid ISIN" in t]
    assert any("'Redeemed'" in t for t in rejections)


def test_isin_validation_uses_the_check_digit_not_just_the_shape() -> None:
    """§8.3 calls for the check digit, and shape alone is not enough.

    `INF179K01UT1` has the right length and alphabet and is not an ISIN. Without
    the Luhn pass a single mistyped character produces a plausible scheme_id
    that resolves to nothing and silently quarantines a real transaction.
    """
    assert is_valid_isin("INF179K01UT0")
    assert not is_valid_isin("INF179K01UT1")
    assert is_valid_isin("US0378331005")
    assert not is_valid_isin("Redeemed")
    assert not is_valid_isin("HDFCNIVODG")
    assert not is_valid_isin(None)


def test_a_scheme_id_claimed_by_two_different_schemes_is_reported(
    parsed: AmfiParseResult,
) -> None:
    """§4.4 assumes an ISIN identifies one scheme. AMFI's file disagrees.

    `INF204KB1XN0` is published against code 142916 (`Nippon India Fixed
    Horizon Fund XXXVI- Series 9`, NAV 10.0000 on 2021-05-10) and code 143451
    (`... XXXVII- Series 9`, NAV 13.5859 on 2022-05-26). They are different
    funds, so the collision does not merely overwrite a name — it splices one
    fund's NAV history onto another's.

    An upsert would resolve this by letting the last row win, silently, which
    `CLAUDE.md` invariant 4 forbids. The collision is counted so the job
    reports `partial` and a human sees it.
    """
    collisions = scheme_id_collisions(parsed.schemes)
    assert "INF204KB1XN0" in collisions

    names = {s.scheme_name for s in collisions["INF204KB1XN0"]}
    assert len(names) == 2
    assert {s.amfi_code for s in collisions["INF204KB1XN0"]} == {"142916", "143451"}


def test_rows_that_agree_are_not_treated_as_a_collision(
    parsed: AmfiParseResult,
) -> None:
    """The same scheme described twice loses nothing when it collapses.

    Only rows that DISAGREE on name, plan or option are collisions — otherwise
    every duplicate listing would raise a false alarm.
    """
    collisions = scheme_id_collisions(parsed.schemes)
    for rows in collisions.values():
        assert len({(r.scheme_name, r.plan, r.option) for r in rows}) > 1


# --- §2 two AMFI layouts, same eight columns --------------------------------

HISTORY = SAMPLE.parent / "navhistory_sample.txt"


@pytest.fixture(scope="module")
def history() -> AmfiParseResult:
    return parse_navall(HISTORY.read_text(encoding="utf-8").splitlines())


def test_the_history_export_orders_its_columns_differently(
    lines: list[str],
) -> None:
    """DECISIONS V0-23. Same eight columns, permuted.

        NAVAll.txt  Scheme Code; ISIN Growth; ISIN Reinvestment; Scheme Name; ...
        history     Scheme Code; NAV Name;    Plan;              Option;      ...

    Column 1 is an ISIN in one file and the scheme name in the other. A parser
    written positionally against either reads the other wrong — and reads it
    wrong *quietly*: the name fails ISIN validation, the row resolves to
    nothing, and a whole export lands in the quarantine queue for a reason no
    message explains.
    """
    daily_header = lines[0]
    history_header = HISTORY.read_text(encoding="utf-8").splitlines()[0]
    assert daily_header != history_header
    assert daily_header.count(";") == history_header.count(";") == 7

    daily = column_map(daily_header)
    hist = column_map(history_header)
    assert daily["isin_primary"] == 1 and daily["name"] == 3
    assert hist["name"] == 1 and hist["isin_primary"] == 4
    assert daily != hist, "the orders genuinely differ"


def test_both_layouts_parse_to_the_same_facts(history: AmfiParseResult) -> None:
    """The point of reading the header: one parser, either file.

    HDFC Flexi Cap Direct Growth is `INF179K01UT0` in both exports, with plan
    `direct` and option `growth`, whichever column order it arrived in.
    """
    direct = [s for s in history.schemes if s.scheme_id == HDFC_DIRECT]
    assert direct, "the history sample must contain the same scheme"
    assert direct[0].plan == "direct"
    assert direct[0].option == "growth"
    assert direct[0].amfi_code == "118955"
    assert history.warnings == [] and history.unparsed == []


def test_the_history_export_carries_a_dated_series(history: AmfiParseResult) -> None:
    """A backfill is only worth running if the dates come back distinct."""
    dated = {n.nav_date: n.nav for n in history.navs if n.scheme_id == HDFC_DIRECT}
    assert dated == {
        date(2024, 1, 2): Decimal("1625.154"),
        date(2024, 1, 3): Decimal("1626.456"),
    }


def test_a_data_row_before_any_header_raises(history: AmfiParseResult) -> None:
    """Without a header the column order is unknown, so there is nothing to do.

    Guessing a layout is how the two exports get confused in the first place.
    """
    with pytest.raises(AmfiParseError, match="column order is unknown"):
        parse_navall([
            "Some Mutual Fund",
            "118955;INF179K01UT0;-;HDFC Flexi Cap Fund;Direct Plan;"
            "Growth Option;1.0;02-Jan-2024",
        ])


def test_an_html_error_page_is_not_mistaken_for_an_empty_file() -> None:
    """An unknown `mf` code returns HTTP 200 and an HTML error page.

    Not a 404, not empty — so a job that treated a parse yielding no rows as
    "this AMC published nothing" would record zero NAVs and report success.
    §2: fail loudly and log the attempted URL, never fall back to a guess.
    """
    with pytest.raises(AmfiParseError, match="not an AMFI NAV file"):
        parse_navall([
            "<!DOCTYPE html>", "<html><head><script>", "(function() {",
            "// Added print Friendly Page function", "</script></head></html>",
        ])
