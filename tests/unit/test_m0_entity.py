"""Entity master and the resolution cascade. MODULE_0.md §4.3, §7.4, §8.

`tests/fixtures/m0/amfi_mcap_sample.xlsx` is trimmed from the real
*Average Market Capitalization 30-Jun-2026* workbook — real ISINs, real market
caps, and both formula columns preserved. `CLAUDE.md`: prefer a real file over
a better simulation.

The load-bearing tests here are about what the cascade **refuses** to do. A
resolver that resolves everything is worse than one that resolves less and
says so: a wrong `issuer_id` moves a holding onto another company and nothing
downstream can detect it.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterator
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from src.common.decimals import connect
from src.common.types import UNRESOLVED, IssuerId
from src.m0_data.load import issuer_id_for, load_mcap
from src.m0_data.normalise.family import family_key
from src.m0_data.normalise.names import normalise_name
from src.m0_data.parse.mcap.amfi import (
    LARGE_CAP_MAX_RANK,
    MID_CAP_MAX_RANK,
    McapParseError,
    basis_date_from_name,
    bucket_for,
    parse_mcap_xlsx,
)
from src.m0_data.resolve.cascade import (
    ISSUER_SEGMENT,
    load_isin_prefix_index,
    load_issuer_index,
    resolve,
)
from src.m0_data.resolve.fuzzy import (
    AUTO_ACCEPT,
    JACCARD_MIN,
    is_auto_acceptable,
    token_jaccard,
    token_set_ratio,
)
from src.m0_data.resolve.isin import (
    is_valid_isin,
    isin_check_digit,
)
from src.m0_data.resolve.queue import accept, enqueue, pending, queue_id_for
from src.m0_data.resolve.synthetic import match_synthetic
from src.m0_data.schema.apply import apply_migrations

SAMPLE = Path(__file__).resolve().parents[1] / "fixtures" / "m0" / "amfi_mcap_sample.xlsx"
SAMPLE_NAME = "AverageMarketCapitalization30Jun2026.xlsx"
BASIS = date(2026, 6, 30)

RELIANCE = "INE002A01018"
FUTURE_RETAIL = "INE752P01024"
MAHINDRA = "INE101A01026"


@pytest.fixture
def conn(tmp_path: Path) -> Iterator[sqlite3.Connection]:
    db = tmp_path / "canonical.db"
    apply_migrations(str(db))
    connection = connect(str(db))
    parsed = parse_mcap_xlsx(SAMPLE.read_bytes(), SAMPLE_NAME)
    load_mcap(connection, parsed, "file-1")
    connection.commit()
    yield connection
    connection.close()


# --- §4.3 the mandatory seed -------------------------------------------------


def test_the_synthetic_seed_includes_no_disclosure(conn: sqlite3.Connection) -> None:
    """DECISIONS V1-01. §4.3's seed lists eight; nine are required.

    `CLAUDE.md` invariant 4 routes a missing disclosure to `__NO_DISCLOSURE__`,
    and `src/common/types.py` declares it, but §4.3's `INSERT` omits it. A
    scheme that published no portfolio at all is a different fact from a
    holding that could not be resolved — collapsing them reports a fund as
    100% unresolved when it simply had not disclosed.
    """
    seeded = {
        r[0] for r in conn.execute("SELECT issuer_id FROM issuer WHERE is_synthetic = 1")
    }
    assert "__NO_DISCLOSURE__" in seeded
    assert "__UNRESOLVED__" in seeded
    assert len(seeded) == 9


# --- §7.4 name normalisation -------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Reliance Industries Ltd", "reliance industries"),
        ("Reliance Industries Limited", "reliance industries"),
        ("The Ramco Cements Ltd.", "ramco cements"),
        ("Mahindra & Mahindra Ltd.", "mahindra mahindra"),
        ("Dr. Reddy's Laboratories Ltd.", "dr reddy s laboratories"),
    ],
)
def test_names_fold_to_a_comparable_form(raw: str, expected: str) -> None:
    """§7.4. Ltd/Limited/& carry no information about which company this is,
    and a disclosure will use all the spellings across a year."""
    assert normalise_name(raw) == expected


# --- §8.4 synthetic rules ----------------------------------------------------


@pytest.mark.parametrize(
    ("name", "bucket"),
    [
        ("TREPS", "__TREPS__"),
        ("Net Receivables/(Payables)", "__RECV__"),
        ("Cash & Bank Balance", "__CASH__"),
        ("Margin with Broker", "__MARGIN__"),
        ("Nifty 50 Future", "__DERIV__"),
        ("Units of HDFC Liquid Fund", "__MFUNIT__"),
        ("7.26% GOI 2033", "__GSEC__"),
    ],
)
def test_every_synthetic_row_has_a_bucket(name: str, bucket: str) -> None:
    """§8.4: without these, subtotals and cash flood the review queue.

    A queue nobody can work through is a queue that stops being worked
    through, and then every unresolved holding stays unresolved.
    """
    assert match_synthetic(name) == bucket


def test_a_state_loan_is_not_a_government_security() -> None:
    """§8.4's note, which is easy to lose and expensive to lose.

    `__GSEC__` is for sovereign paper only. Burying `7.26% Maharashtra SDL
    2032` there hides a real state-government exposure, and burying an NCD
    there hides corporate credit risk entirely.
    """
    assert match_synthetic("7.26% Maharashtra SDL 2032") is None
    assert match_synthetic("8.15% Tata Capital NCD 2029") is None


# --- §8.2 the cascade --------------------------------------------------------


def test_a_known_isin_resolves_to_its_issuer(conn: sqlite3.Connection) -> None:
    """Step 0, and ~95% of equity rows because SEBI mandates the column."""
    result = resolve(conn, "Reliance Industries Ltd", RELIANCE, "equity")
    assert result.issuer_id == issuer_id_for(RELIANCE)
    assert result.method == "isin"
    assert result.confidence == Decimal("1.0")
    assert not result.needs_review


def test_a_known_isin_beats_a_synthetic_name_rule(conn: sqlite3.Connection) -> None:
    """DECISIONS V1-02, departure 1 — found against the real universe.

    §8.2 runs the synthetic rules BEFORE ISIN. §8.4's derivative pattern
    matches the bare word `future`, which captures seven real listed companies
    in the AMFI list — the whole Future Group. Under the spec's ordering a fund
    holding Future Retail, with a valid ISIN on the row, has that equity
    bucketed as `__DERIV__`: the exposure vanishes from the look-through and
    the derivative bucket inflates by the same amount.

    An ISIN that resolves to a known instrument is harder evidence than a word
    in a name.
    """
    assert match_synthetic("Future Retail Ltd.") == "__DERIV__"

    result = resolve(conn, "Future Retail Ltd.", FUTURE_RETAIL, "equity")
    assert result.method == "isin"
    assert result.issuer_id == issuer_id_for(FUTURE_RETAIL)


def test_a_real_derivative_row_still_reaches_the_rule(conn: sqlite3.Connection) -> None:
    """Reordering must not reopen the flood §8.4 prevents.

    It does not: TREPS, cash, receivables and derivative rows carry no ISIN, so
    step 0 never sees them and they still fall to the name rules.
    """
    result = resolve(conn, "Nifty 50 Future", None, None)
    assert result.method == "rule"
    assert result.issuer_id == "__DERIV__"


def test_an_invalid_isin_falls_through_rather_than_creating_an_instrument(
    conn: sqlite3.Connection,
) -> None:
    """§8.3: reject malformed ISINs rather than creating garbage instruments.

    `INE002A01019` fails the check digit by one. Trusting it would mint an
    issuer keyed on a typo, which then never matches anything again.
    """
    result = resolve(conn, "Reliance Industries Ltd", "INE002A01019", "equity")
    assert result.method != "isin"
    assert result.issuer_id != issuer_id_for("INE002A01019")


def test_a_valid_but_unknown_isin_is_provisional_and_flagged(
    conn: sqlite3.Connection,
) -> None:
    """The check digit passed, so this is a real security we have not seen.

    Dropping it loses a holding we have strong evidence about; accepting it
    silently means a new issuer appears in the master with nobody told. It
    resolves provisionally AND asks for review.
    """
    result = resolve(conn, "Some Newly Listed Co Ltd", "INE0LRY01011", "equity")
    assert result.method == "provisional"
    assert result.needs_review
    assert result.issuer_id == UNRESOLVED


def test_an_unknown_name_is_unresolved_never_guessed(
    conn: sqlite3.Connection,
) -> None:
    """`CLAUDE.md` invariant 4: never silently drop. It carries `__UNRESOLVED__`
    and stays visible in every aggregate rather than vanishing from a join."""
    result = resolve(conn, "Zzyzx Holdings Private Ltd", None, "equity")
    assert result.issuer_id == UNRESOLVED
    assert result.method == "unresolved"
    assert result.needs_review


# --- V1-37, a disclosure describes a scheme and not an ISIN ------------------


def test_a_share_class_suffix_is_dropped_and_a_fund_name_is_not() -> None:
    """The whole-segment rule, which is the whole safety argument.

    AMFI writes a share class as a suffix after a separator, and the base name
    never contains a separator AMFI did not put there. So a segment made
    ENTIRELY of share-class vocabulary is dropped and everything else is kept.

    `growth` is in the vocabulary, and `Nippon India Growth Mid Cap Fund` keeps
    its `Growth` — that segment also holds `mid`, `cap` and `fund`. Substring
    removal would have split that family in half, its Growth classes losing a
    word its IDCW classes keep.
    """
    same = [
        "HDFC Flexi Cap Fund - Growth Option - Direct Plan",
        "HDFC Flexi Cap Fund - Growth Plan",
        "HDFC Flexi Cap Fund - IDCW Option - Direct Plan",
        "HDFC Flexi Cap Fund - IDCW Plan",
    ]
    assert len({family_key(n) for n in same}) == 1
    assert family_key(same[0]) == "hdfc flexi cap fund"

    # A fund whose NAME contains a qualifier word keeps it.
    assert family_key("Nippon India Growth Mid Cap Fund") == (
        "nippon india growth mid cap fund"
    )
    assert family_key("Kotak Pioneer Fund- Direct Plan- Growth Option") == (
        "kotak pioneer fund"
    )
    assert family_key(
        "Kotak Pioneer Fund- Regular Plan- Reinvestment of Income"
        " Distribution cum capital withdrawal option"
    ) == "kotak pioneer fund"


def test_two_funds_whose_names_differ_by_one_qualifier_word_stay_apart() -> None:
    """Measured, not imagined. Removing the substring `regular` merged
    `ICICI Prudential Regular Savings Fund` into `ICICI Prudential Savings
    Fund` — two different funds, one key, and a portfolio that would have been
    filed against the wrong ISIN.

    The segment rule keeps them apart because `Regular Savings Fund` is not a
    qualifier-only segment.
    """
    a = family_key("ICICI Prudential Regular Savings Fund - Direct Plan - Bonus")
    b = family_key("ICICI Prudential Savings Fund - Bonus")
    assert a != b
    assert "regular" in a


def test_capital_is_droppable_in_a_qualifier_segment_and_not_in_a_name() -> None:
    """`capital` had to join the vocabulary for Kotak's IDCW classes. It is
    safe precisely because a segment must be qualifiers all the way through:
    `Capital Protection Oriented` never is."""
    assert "capital" not in family_key(
        "Kotak Pioneer Fund- Direct Plan- Reinvestment of Income"
        " Distribution cum capital withdrawal option"
    )
    assert "capital" in family_key(
        "ICICI Prudential Capital Protection Oriented Fund - Growth"
    )


# --- §8.4, units of another scheme -------------------------------------------


def test_a_fund_wrapper_is_recognised_however_the_amc_spells_it() -> None:
    """§8.4's pattern wanted `units of ... fund` or `etf ... units`, and across
    532 security rows in four real disclosures it never once matched.

    What AMCs actually publish: `ICICI Prudential Gold ETF`,
    `Ishares Nasdaq 100 UCITS ETF USD`, `Geninnov Global Master Fund`. None
    says "units", so 9,064 Cr of fund-inside-a-fund counted as a failure to
    resolve rather than as the thing it plainly is.
    """
    for name in (
        "ICICI Prudential Gold ETF",
        "Ishares Nasdaq 100 UCITS ETF USD",
        "Geninnov Global Master Fund",
        "Nippon India ETF Nifty BeES",
        "Units of Mutual Fund",
    ):
        assert match_synthetic(name, "equity") == "__MFUNIT__", name


def test_an_operating_company_is_not_a_fund_because_it_manages_them() -> None:
    """The one clause that carries risk is `a name ending in Fund`, and this is
    what it must not catch.

    `SBI Funds Management Limited` is a real equity holding in both HDFC's and
    ICICI's disclosures. Swept into `__MFUNIT__` it would leave the issuer
    analysis entirely, because synthetic issuers are excluded from overlap and
    concentration — so the error would not look like an error, it would look
    like a company nobody owns.

    The plural defeats the word boundary and the trailing words defeat the
    anchor. Both spellings are taken verbatim from the two files.
    """
    for name in (
        "SBI Funds Management Limited",
        "SBI Funds Management Ltd.",
        "Nippon Life India Asset Management Ltd",
        "HDFC Asset Management Company Limited",
    ):
        assert match_synthetic(name, "equity") is None, name


def test_the_loaders_own_classification_settles_a_fund_unit() -> None:
    """Structure before vocabulary, as V1-30 argued for sovereign paper.

    ICICI's Gold ETF sits under a `Units of Mutual Fund` heading, so the loader
    had already classed it `mfunit` — and it still resolved to
    `__UNRESOLVED__`, because `CLASS_FALLBACK` stopped at cash and derivatives.
    A row whose section says what it is does not need its name to agree.
    """
    assert match_synthetic("Something With No Useful Name", "mfunit") == "__MFUNIT__"
    # REITs and InvITs classify as `other` and keep their real issuers.
    assert match_synthetic("Embassy Office Parks REIT", "other") is None


# --- §8.4, sovereign paper only ----------------------------------------------


def test_central_government_paper_is_recognised_by_its_isin_not_its_name() -> None:
    """ICICI writes dated sovereign paper as exactly `Government Securities`.

    That string contains no `government of india`, no `goi`, no `g-sec` token
    and no `treasury bill`, so §8.4's vocabulary rule missed it entirely while
    the T-bills beside it — named `91 Days Treasury Bills` — resolved fine.
    1,837 Cr of sovereign debt sat in `__UNRESOLVED__` on one fund because of
    how it was spelled. Structure does not have that failure mode.
    """
    assert match_synthetic("Government Securities", "debt", "IN0020260025") == "__GSEC__"
    # Even with a name that says nothing at all.
    assert match_synthetic("", "debt", "IN0020250018") == "__GSEC__"
    # Treasury bills carry the same segment and keep working.
    assert match_synthetic("91 Days Treasury Bills", "debt", "IN002026X156") == "__GSEC__"


def test_a_state_development_loan_is_never_swept_into_the_sovereign_bucket() -> None:
    """§8.4's note, as a regression test.

    A state government is a real borrower with a real exposure, and burying
    `State Government of Maharashtra` in `__GSEC__` would hide it — synthetic
    issuers are excluded from overlap and concentration, so the exposure would
    not merely be mislabelled, it would leave the analysis altogether.

    This is why the pattern is `IN0020` and not `IN` plus two digits. The wider
    form would have taken eleven state governments and 1,553 Cr in one fund.
    """
    for isin, name in (
        ("IN2220240435", "State Government of Maharashtra"),
        ("IN2920250171", "State Government of Rajasthan"),
        ("IN2120250138", "State Government of Madhya Pradesh"),
        ("IN4520250684", "State Government of Telangana"),
    ):
        assert match_synthetic(name, "debt", isin) is None, f"{name} was bucketed"


def test_a_corporate_isin_is_not_government_however_it_is_spelled() -> None:
    """`INE` is a company. The rule must key on the segment, not on `IN`."""
    assert match_synthetic("Some Government Contractor Ltd", "debt", "INE040A08419") != (
        "__GSEC__"
    )


# --- §8.1's premise, for instruments that are not equity ---------------------


def _an_equity_isin(conn: sqlite3.Connection) -> tuple[str, str]:
    """Any ISIN in the master, with the issuer it belongs to."""
    row = conn.execute(
        "SELECT isin, issuer_id FROM instrument ORDER BY isin LIMIT 1"
    ).fetchone()
    return str(row[0]), str(row[1])


def test_a_bond_resolves_to_the_issuer_its_equity_resolves_to(
    conn: sqlite3.Connection,
) -> None:
    """§8.1: the exposure unit is the ISSUER. Before V1-29 that held only for
    equity, because a company's bonds and certificates of deposit carry
    different ISINs from its shares and only the share ISIN is in the master.

    Measured on ICICI Multi-Asset: HDFC Bank appeared six times under six
    ISINs and resolved once. `INE040A16JC3` (a certificate of deposit, 1,349
    Cr) and `INE040A08419` (an AT1 bond) sat in `__UNRESOLVED__` while
    `INE040A01034` resolved cleanly — the same company, split by instrument
    type, which is exactly what the by-issuer promise says will not happen.
    """
    equity_isin, issuer_id = _an_equity_isin(conn)
    # Same issuer segment, different security type and serial. Built rather
    # than hard-coded so this does not depend on which fixture row sorts first.
    body = equity_isin[:ISSUER_SEGMENT] + "16JC"
    debt_isin = body + str(isin_check_digit(body))
    assert is_valid_isin(debt_isin)
    assert debt_isin != equity_isin

    result = resolve(conn, "Some Bank Ltd. ( Tier II Bond )", debt_isin, "debt")
    assert result.issuer_id == issuer_id
    assert result.method == "isin_prefix"
    # Not 1.0: the segment identifies the issuer, but this instrument itself
    # has never been seen.
    assert result.confidence == Decimal("0.9")


def test_an_unknown_issuer_segment_is_still_only_provisional(
    conn: sqlite3.Connection,
) -> None:
    """The new step must not turn every valid ISIN into a resolution. A segment
    nobody has seen is exactly the case `provisional` exists for."""
    result = resolve(conn, "Some Newly Listed Co Ltd", "INE0LRY01011", "equity")
    assert result.method == "provisional"
    assert result.issuer_id == UNRESOLVED


def test_an_ambiguous_issuer_segment_is_not_guessed(
    conn: sqlite3.Connection,
) -> None:
    """Two issuers behind one segment means the master has split a company's
    share classes. Picking whichever sorts first would be silently wrong and
    nothing downstream could tell, so the segment is omitted from the index
    entirely and the row falls through to `provisional`.
    """
    equity_isin, issuer_id = _an_equity_isin(conn)
    segment = equity_isin[:ISSUER_SEGMENT]
    # A second issuer sharing the segment, as a share-class pair would.
    conn.execute(
        "INSERT INTO issuer (issuer_id, canonical_name, is_synthetic)"
        " VALUES (?, ?, 0)",
        (f"TEST:{segment}B", "Other Share Class Ltd"),
    )
    twin_body = segment + "01ZZ"
    twin = twin_body + str(isin_check_digit(twin_body))
    conn.execute(
        "INSERT INTO instrument (isin, issuer_id, instrument_type)"
        " VALUES (?, ?, 'equity')",
        (twin, f"TEST:{segment}B"),
    )

    prefixes = load_isin_prefix_index(conn)
    assert segment not in prefixes, "an ambiguous segment must not be resolvable"

    body = segment + "16JC"
    debt_isin = body + str(isin_check_digit(body))
    result = resolve(conn, "Ambiguous Co Ltd Bond", debt_isin, "debt", None, prefixes)
    assert result.method == "provisional"
    assert result.issuer_id == UNRESOLVED
    assert issuer_id  # the original is untouched


def test_a_synthetic_rule_still_beats_the_issuer_segment(
    conn: sqlite3.Connection,
) -> None:
    """Step 1b sits AFTER the rules, unlike step 0.

    An exact ISIN earned its precedence over the name rules with a measurement
    (V1-02 departure 1). A segment match is inferred from how ISINs are
    allocated, so it does not get to pull a derivative or a cash row into the
    issuer space on weaker evidence than that.
    """
    equity_isin, _ = _an_equity_isin(conn)
    body = equity_isin[:ISSUER_SEGMENT] + "16JC"
    debt_isin = body + str(isin_check_digit(body))
    result = resolve(conn, "Nifty 50 Index Future", debt_isin, "derivative")
    assert result.method == "rule"


def test_resolution_is_deterministic(conn: sqlite3.Connection) -> None:
    """`CLAUDE.md` invariant 10: a rebuild reproduces byte-identical output."""
    index = load_issuer_index(conn)
    first = resolve(conn, "Reliance Industries Limited", None, "equity", index)
    second = resolve(conn, "Reliance Industries Limited", None, "equity", index)
    assert first == second


def test_synthetic_issuers_are_not_fuzzy_candidates(conn: sqlite3.Connection) -> None:
    """`Cash & Bank Balance` normalises to `cash bank balance`.

    Left in the candidate index it becomes a plausible fuzzy match for any row
    mentioning a bank — and a bank equity matched onto `__CASH__` is a real
    holding that disappears from the look-through.
    """
    index = load_issuer_index(conn)
    assert not any(v.startswith("__") for v in index.values())


# --- the fuzzy guard §8.2 lacks ---------------------------------------------


def test_token_set_ratio_scores_a_subset_as_a_perfect_match() -> None:
    """DECISIONS V1-02. This is why §8.2's threshold alone cannot gate.

    When one name's tokens are a subset of the other's, the intersection IS the
    shorter name, so the comparison scores 100. "Is contained in" and "is equal
    to" are indistinguishable to this algorithm — by construction, not by
    accident, and not as an artefact of using difflib.
    """
    a, b = normalise_name("Tech Mahindra Ltd"), normalise_name("Mahindra & Mahindra Ltd")
    assert token_set_ratio(a, b) == pytest.approx(100.0)
    assert token_set_ratio(a, b) >= AUTO_ACCEPT


def test_the_jaccard_guard_rejects_what_the_score_accepts() -> None:
    """The fix is a second condition, not a higher bar.

    On a 400-name sample from the real AMFI universe, 18 names would have been
    auto-accepted onto the wrong issuer at a score threshold of 95. At
    `jaccard >= 0.7` that count is zero, at every threshold from 90 to 95 — so
    §8.2's own 92 is kept. The number was never the problem.
    """
    a, b = normalise_name("Tech Mahindra Ltd"), normalise_name("Mahindra & Mahindra Ltd")
    assert token_jaccard(a, b) < JACCARD_MIN
    assert not is_auto_acceptable(token_set_ratio(a, b), token_jaccard(a, b))


def test_a_reordered_name_is_still_accepted() -> None:
    """The guard must not reject what token_set_ratio exists to catch.

    Word order and stripped suffixes are the whole point of the algorithm.
    """
    a, b = normalise_name("Reliance Industries Ltd"), "industries reliance"
    assert is_auto_acceptable(token_set_ratio(a, b), token_jaccard(a, b))


def test_tech_mahindra_does_not_resolve_to_mahindra_and_mahindra(
    conn: sqlite3.Connection,
) -> None:
    """The defect, end to end, against a master holding only the wrong answer.

    Mahindra & Mahindra is in the fixture; Tech Mahindra is not. Under §8.2 as
    written this resolves to M&M with `confidence=1.0` — a misattribution of
    roughly a lakh crore, presented as certainty. It must reach the queue.
    """
    result = resolve(conn, "Tech Mahindra Ltd", None, "equity")
    assert result.issuer_id != issuer_id_for(MAHINDRA)
    assert result.method == "unresolved"
    assert result.needs_review


# --- §8.5 the review queue ---------------------------------------------------


def test_the_queue_is_ordered_by_materiality_not_arrival(
    conn: sqlite3.Connection,
) -> None:
    """§8.5. The top twenty rows usually account for most unresolved value.

    Ordered by arrival, a Rs 3,000 position sits ahead of a Rs 3 crore one and
    the queue stops being worth opening.
    """
    enqueue(conn, "Small Position Ltd", "small position", date(2026, 6, 30),
            market_value=Decimal("3000"))
    enqueue(conn, "Large Position Ltd", "large position", date(2026, 6, 30),
            market_value=Decimal("30000000"))
    conn.commit()

    rows = pending(conn)
    assert [r.raw_name for r in rows] == ["Large Position Ltd", "Small Position Ltd"]


def test_the_same_name_is_one_decision_not_twenty(conn: sqlite3.Connection) -> None:
    """Keyed on the normalised name, so materiality accumulates across schemes.

    That sum is what makes the priority ordering mean "resolving this recovers
    the most exposure".
    """
    for _ in range(3):
        enqueue(conn, "Repeated Ltd", "repeated", date(2026, 6, 30),
                market_value=Decimal("1000"))
    conn.commit()

    rows = pending(conn)
    assert len(rows) == 1
    assert rows[0].occurrence_count == 3
    assert rows[0].total_mv_inr == Decimal("3000")


def test_accepting_a_queue_entry_writes_the_alias_that_stops_it_recurring(
    conn: sqlite3.Connection,
) -> None:
    """§8.5's "the queue shrinks monotonically" depends entirely on this.

    Without the alias the same name returns on the next disclosure and the
    reviewer answers the same question every month.
    """
    qid = enqueue(conn, "Reliance Inds Ltd", normalise_name("Reliance Inds Ltd"),
                  date(2026, 6, 30), market_value=Decimal("500000"))
    accept(conn, qid, IssuerId(issuer_id_for(RELIANCE)))
    conn.commit()

    assert pending(conn) == []
    again = resolve(conn, "Reliance Inds Ltd", None, "equity")
    assert again.method == "alias"
    assert again.issuer_id == issuer_id_for(RELIANCE)


def test_the_queue_id_is_deterministic() -> None:
    """The same name always lands on the same row, across runs and processes."""
    assert queue_id_for("reliance industries") == queue_id_for("reliance industries")
    assert queue_id_for("a") != queue_id_for("b")


# --- §2.2 S4 the market-cap list --------------------------------------------


def test_the_statutory_buckets_are_rank_boundaries() -> None:
    """§2.2 S4: 1-100 Large, 101-250 Mid, 251+ Small.

    Ranks, not absolute sizes — which is why the list must be re-read each
    half-year rather than a threshold stored once.
    """
    assert bucket_for(1) == bucket_for(LARGE_CAP_MAX_RANK) == "large"
    assert bucket_for(LARGE_CAP_MAX_RANK + 1) == bucket_for(MID_CAP_MAX_RANK) == "mid"
    assert bucket_for(MID_CAP_MAX_RANK + 1) == "small"


def test_the_basis_date_comes_from_the_filename() -> None:
    """§7.5 rule 2, and §7.5 forbids defaulting when it cannot be found.

    `mcap_basis` is what stops a 2026 classification being applied to a 2021
    holding, so a wrong one is silent look-ahead bias.
    """
    assert basis_date_from_name(SAMPLE_NAME) == BASIS
    with pytest.raises(McapParseError):
        basis_date_from_name("AverageMarketCapitalization.xlsx")


def test_the_rank_and_average_columns_are_formulas_and_are_recomputed() -> None:
    """DECISIONS V1-02. Reading them yields formula text, not numbers.

    `Sr. No.` holds `=RANK(J3,...)` and column J holds `=AVERAGE(E3,G3,I3)` —
    the figure rank operates on. Reading column E alone, as the first header
    containing "market cap", would silently rank on BSE only.
    """
    parsed = parse_mcap_xlsx(SAMPLE.read_bytes(), SAMPLE_NAME)
    assert parsed.rows
    ranked = sorted((r for r in parsed.rows if r.rank), key=lambda r: r.rank or 0)
    assert [r.rank for r in ranked] == list(range(1, len(ranked) + 1))
    assert all(isinstance(r.market_cap, Decimal) for r in ranked)


def test_our_bucket_is_cross_checked_against_amfis_own_column() -> None:
    """The file states the answer in column K; we compute it anyway and compare.

    On the FULL 30-Jun-2026 list — 5,427 companies — the two agree everywhere:
    zero disagreements, and the split is exactly 100 large, 150 mid, 5,177
    small. That agreement is what says we read the right columns, and it is
    how the `=AVERAGE(E,G,I)` versus BSE-only mistake was caught: ranking on
    column E alone put Bosch at #99 instead of #101.

    A trimmed fixture cannot reproduce that. Five rows rank 1..5, so Future
    Retail is locally "large" and AMFI's column says "small" — its rank in the
    full universe. So what this asserts is the **mechanism**: the disagreement
    is detected and reported rather than silently reconciled in either
    direction.
    """
    parsed = parse_mcap_xlsx(SAMPLE.read_bytes(), SAMPLE_NAME)
    compared = [r for r in parsed.rows if r.bucket and r.stated_bucket]
    assert compared, "the fixture must retain AMFI's categorisation column"

    # Rows whose local rank happens to match the real one agree; the rest are
    # reported. Neither side is quietly preferred.
    disagreements = [t for _, t in parsed.warnings if "AMFI says" in t]
    assert disagreements, "a subset ranking must surface as a disagreement"
    assert any("INE752P01024" in t for t in disagreements)
    assert all(
        r.bucket == r.stated_bucket
        for r in compared
        if not any(r.isin in t for t in disagreements)
    )


def test_loading_is_idempotent(conn: sqlite3.Connection) -> None:
    """The list is re-read every half-year and overlaps everything already in."""
    before = _counts(conn)
    load_mcap(conn, parse_mcap_xlsx(SAMPLE.read_bytes(), SAMPLE_NAME), "file-1")
    conn.commit()
    assert _counts(conn) == before


def test_classification_is_stored_point_in_time(conn: sqlite3.Connection) -> None:
    """`CLAUDE.md` invariant 6. `valid_from` is the list's period end, not today.

    Applying today's list to a 2021 holding creates phantom drift or masks real
    drift, and the basis date travelling with the value is the only defence.
    """
    rows = conn.execute(
        "SELECT DISTINCT valid_from, taxonomy FROM issuer_classification"
    ).fetchall()
    assert rows
    for valid_from, taxonomy in rows:
        assert taxonomy == "amfi_mcap"
        assert str(valid_from).startswith(str(BASIS))


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        for t in ("issuer", "instrument", "issuer_classification")
    }
