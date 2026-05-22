"""Tests for the extraction logic, using synthetic CompanyFacts.

These cover the regressions surfaced by the AAPL smoke test in M1:
  - Picking current-period facts over prior-year-comparable facts (sort by
    `end` descending).
  - Per-period tag fallback (try each tag in items.yaml's `xbrl_tags` list).
  - Cross-tag Q4 derivation (Q1-Q3 from one tag, annual 10-K from another).
  - Unit threading (shares vs USD).
  - `period_avg` items skip Q4 derivation.
"""

from __future__ import annotations

from datetime import date

import pytest

from sec_financials.companyfacts import CompanyFacts, Fact
from sec_financials.config import Item
from sec_financials.extractor import (
    _select_flow_fact,
    discover_quarters_to_extract,
    discover_recent_fiscal_years,
    extract_quarterly,
)


# ──────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────


def _fact(
    *,
    tag: str,
    val: float,
    end: date,
    start: date | None,
    fy: int,
    fp: str,
    form: str = "10-Q",
    filed: date | None = None,
    unit: str = "USD",
    accession: str = "0001-23-000001",
) -> Fact:
    return Fact(
        tag=tag,
        unit=unit,
        val=val,
        end=end,
        start=start,
        accession=accession,
        fiscal_year=fy,
        fiscal_period=fp,
        form=form,
        filed=filed or end,
        frame=None,
    )


def _make_facts(*, facts_by_tag_unit: dict[tuple[str, str], list[Fact]]) -> CompanyFacts:
    index: dict[tuple[str, str], dict[str, list[Fact]]] = {}
    for (tag, unit), facts in facts_by_tag_unit.items():
        index.setdefault(("us-gaap", tag), {}).setdefault(unit, []).extend(facts)
    return CompanyFacts(cik=1, entity_name="TestCo", _index=index)


# ──────────────────────────────────────────────────────────────────────────
# _select_flow_fact: end DESC, filed DESC
# ──────────────────────────────────────────────────────────────────────────


def test_select_flow_picks_current_period_over_comparative():
    """Two facts with same fy/fp/duration but different ends: pick newer end.

    Mirrors the SEC behaviour where a 10-Q reports both the current quarter
    and the year-ago comparable, both tagged with the same fp.
    """
    comparative = _fact(
        tag="Revenue",
        val=100,
        end=date(2023, 3, 31),
        start=date(2023, 1, 1),
        fy=2024,
        fp="Q1",
        filed=date(2024, 5, 1),
    )
    current = _fact(
        tag="Revenue",
        val=200,
        end=date(2024, 3, 31),
        start=date(2024, 1, 1),
        fy=2024,
        fp="Q1",
        filed=date(2024, 5, 1),
    )
    chosen = _select_flow_fact(
        [comparative, current],
        fiscal_year=2024,
        fiscal_period="Q1",
        duration_range=(80, 100),
        forms=("10-Q",),
    )
    assert chosen is not None
    assert chosen.val == 200  # current, not comparative


def test_select_flow_prefers_latest_filing_on_same_period():
    """Same fy/fp/end across two filings → pick the most recently filed (restatement)."""
    original = _fact(
        tag="Revenue",
        val=100,
        end=date(2024, 3, 31),
        start=date(2024, 1, 1),
        fy=2024,
        fp="Q1",
        filed=date(2024, 5, 1),
    )
    restated = _fact(
        tag="Revenue",
        val=110,
        end=date(2024, 3, 31),
        start=date(2024, 1, 1),
        fy=2024,
        fp="Q1",
        filed=date(2025, 2, 1),  # later restatement
    )
    chosen = _select_flow_fact(
        [original, restated],
        fiscal_year=2024,
        fiscal_period="Q1",
        duration_range=(80, 100),
        forms=("10-Q",),
    )
    assert chosen is not None
    assert chosen.val == 110


# ──────────────────────────────────────────────────────────────────────────
# Cross-tag Q4 derivation
# ──────────────────────────────────────────────────────────────────────────


def _quarter_fact(tag: str, val: float, fy: int, fp: str, end: date) -> Fact:
    quarter_starts = {"Q1": date(fy - 1, 10, 1), "Q2": date(fy, 1, 1),
                      "Q3": date(fy, 4, 1), "Q4": date(fy, 7, 1)}
    return _fact(tag=tag, val=val, end=end, start=quarter_starts[fp],
                 fy=fy, fp=fp, form="10-Q")


def _annual_fact(tag: str, val: float, fy: int, end: date) -> Fact:
    return _fact(
        tag=tag,
        val=val,
        end=end,
        start=date(fy - 1, 10, 1),
        fy=fy,
        fp="FY",
        form="10-K",
    )


def test_q4_uses_tag_that_resolved_q1_q3_when_multiple_have_annual():
    """When fallback tag B resolves Q1-Q3 but tag A also has annual data,
    Q4 derivation should prefer tag B's annual to keep concepts consistent.

    Models the NVIDIA case: pure `Depreciation` is filed annually only;
    quarterly facts only come from `DepreciationDepletionAndAmortization`.
    """
    fy = 2024
    # Tag A: only annual filings (no quarterly). Different value from tag B.
    a_annual = _annual_fact("PureDep", 2400, fy, date(2024, 9, 30))
    # Tag B: quarterly + annual. Dates align with _quarter_fact helper.
    b_q1 = _quarter_fact("DDA", 611, fy, "Q1", date(2023, 12, 30))
    b_q2 = _quarter_fact("DDA", 669, fy, "Q2", date(2024, 3, 30))
    b_q3 = _quarter_fact("DDA", 751, fy, "Q3", date(2024, 6, 29))
    b_annual = _annual_fact("DDA", 2843, fy, date(2024, 9, 30))

    facts = _make_facts(
        facts_by_tag_unit={
            ("PureDep", "USD"): [a_annual],
            ("DDA", "USD"): [b_q1, b_q2, b_q3, b_annual],
        }
    )

    item = Item(
        key="depreciation",
        display_name="Depreciation",
        statement="cash_flow",
        flow_or_stock="flow",
        unit="USD",
        # Pure-D listed first (preferred), DDA as fallback.
        xbrl_tags=("PureDep", "DDA"),
    )

    rows = extract_quarterly(facts, [item], ticker="TEST", fiscal_years=[fy])
    by_q = {r.fiscal_quarter: r.values["depreciation"].value for r in rows}
    # Q1-Q3 must use DDA (since PureDep has no quarterly facts).
    assert by_q["Q1"] == 611
    assert by_q["Q2"] == 669
    assert by_q["Q3"] == 751
    # Q4 must use DDA's annual (2843) − (611+669+751), NOT PureDep's annual (2400).
    # If we mixed, Q4 would be 2400 − 2031 = 369 — wrong.
    assert by_q["Q4"] == 2843 - 611 - 669 - 751  # = 812


def test_q4_derives_across_tag_transition():
    """Q1-Q3 reported under old tag, FY 10-K under new tag → Q4 still derives."""
    fy = 2024
    q1 = _quarter_fact("OldRev", 100, fy, "Q1", date(2023, 12, 31))
    q2 = _quarter_fact("OldRev", 120, fy, "Q2", date(2024, 3, 31))
    q3 = _quarter_fact("OldRev", 130, fy, "Q3", date(2024, 6, 30))
    annual = _annual_fact("NewRev", 500, fy, date(2024, 9, 30))

    facts = _make_facts(
        facts_by_tag_unit={
            ("OldRev", "USD"): [q1, q2, q3],
            ("NewRev", "USD"): [annual],
        }
    )

    item = Item(
        key="revenue",
        display_name="Revenue",
        statement="income",
        flow_or_stock="flow",
        unit="USD",
        xbrl_tags=("OldRev", "NewRev"),
    )

    rows = extract_quarterly(facts, [item], ticker="TEST", fiscal_years=[fy])
    by_q = {r.fiscal_quarter: r.values["revenue"].value for r in rows}
    assert by_q["Q1"] == 100
    assert by_q["Q2"] == 120
    assert by_q["Q3"] == 130
    assert by_q["Q4"] == 500 - 100 - 120 - 130  # 150


# ──────────────────────────────────────────────────────────────────────────
# Unit threading (shares)
# ──────────────────────────────────────────────────────────────────────────


def test_shares_unit_found_when_threaded():
    """A shares-unit item shouldn't be missed just because the default unit is USD."""
    fy = 2024
    q1_shares = _fact(
        tag="WeightedAverageNumberOfDilutedSharesOutstanding",
        val=5_000_000,
        end=date(2023, 12, 31),
        start=date(2023, 10, 1),
        fy=fy,
        fp="Q1",
        unit="shares",
        form="10-Q",
    )
    facts = _make_facts(
        facts_by_tag_unit={
            ("WeightedAverageNumberOfDilutedSharesOutstanding", "shares"): [q1_shares],
        }
    )
    item = Item(
        key="shares_diluted",
        display_name="Diluted Shares",
        statement="income",
        flow_or_stock="period_avg",
        unit="shares",
        xbrl_tags=("WeightedAverageNumberOfDilutedSharesOutstanding",),
    )

    rows = extract_quarterly(facts, [item], ticker="TEST", fiscal_years=[fy])
    q1 = next(r for r in rows if r.fiscal_quarter == "Q1")
    assert q1.values["shares_diluted"].value == 5_000_000


# ──────────────────────────────────────────────────────────────────────────
# period_avg skips Q4 derivation
# ──────────────────────────────────────────────────────────────────────────


def test_period_avg_q4_left_blank():
    """For period_avg items, Q4 stays blank with an explanatory note."""
    fy = 2024
    facts_list = []
    for q, end in [
        ("Q1", date(2023, 12, 31)),
        ("Q2", date(2024, 3, 31)),
        ("Q3", date(2024, 6, 30)),
    ]:
        facts_list.append(
            _fact(
                tag="Shares",
                val=5_000_000,
                end=end,
                start=date(end.year, end.month - 2, 1),
                fy=fy,
                fp=q,
                unit="shares",
                form="10-Q",
            )
        )
    annual = _fact(
        tag="Shares",
        val=5_000_000,
        end=date(2024, 9, 30),
        start=date(2023, 10, 1),
        fy=fy,
        fp="FY",
        unit="shares",
        form="10-K",
    )
    facts = _make_facts(facts_by_tag_unit={("Shares", "shares"): facts_list + [annual]})

    item = Item(
        key="shares",
        display_name="Shares",
        statement="income",
        flow_or_stock="period_avg",
        unit="shares",
        xbrl_tags=("Shares",),
    )
    rows = extract_quarterly(facts, [item], ticker="TEST", fiscal_years=[fy])
    q4 = next(r for r in rows if r.fiscal_quarter == "Q4")
    assert q4.values["shares"].value is None
    assert "period_avg" in q4.values["shares"].note


# ──────────────────────────────────────────────────────────────────────────
# Fiscal year discovery
# ──────────────────────────────────────────────────────────────────────────


# ──────────────────────────────────────────────────────────────────────────
# Stock items (balance sheet)
# ──────────────────────────────────────────────────────────────────────────


def test_stock_item_pulls_balance_for_each_quarter():
    """Balance fact (no start date) is picked for each (fy, fq)."""
    fy = 2024
    balances = []
    quarter_ends = {"Q1": date(2023, 12, 30), "Q2": date(2024, 3, 30),
                    "Q3": date(2024, 6, 29)}
    for q, end in quarter_ends.items():
        balances.append(_fact(tag="Cash", val=40_000 if q == "Q1" else
                              (32_000 if q == "Q2" else 25_000),
                              end=end, start=None, fy=fy, fp=q, form="10-Q"))
    balances.append(_fact(tag="Cash", val=29_000, end=date(2024, 9, 28),
                          start=None, fy=fy, fp="FY", form="10-K"))
    facts = _make_facts(facts_by_tag_unit={("Cash", "USD"): balances})

    item = Item(
        key="cash",
        display_name="Cash",
        statement="balance_sheet",
        flow_or_stock="stock",
        unit="USD",
        xbrl_tags=("Cash",),
    )

    rows = extract_quarterly(facts, [item], ticker="TEST", fiscal_years=[fy])
    by_q = {r.fiscal_quarter: r.values["cash"].value for r in rows}
    assert by_q["Q1"] == 40_000
    assert by_q["Q2"] == 32_000
    assert by_q["Q3"] == 25_000
    assert by_q["Q4"] == 29_000  # comes from 10-K with fp=FY


def test_stock_item_ignores_flow_facts_with_same_tag():
    """Even if a tag has flow facts (start != None), stock lookup picks instant-context only."""
    fy = 2024
    flow_fact = _fact(tag="X", val=999, end=date(2024, 3, 30),
                      start=date(2024, 1, 1), fy=fy, fp="Q2", form="10-Q")
    instant_fact = _fact(tag="X", val=100, end=date(2024, 3, 30),
                         start=None, fy=fy, fp="Q2", form="10-Q")
    facts = _make_facts(facts_by_tag_unit={("X", "USD"): [flow_fact, instant_fact]})

    item = Item(key="x", display_name="X", statement="balance_sheet",
                flow_or_stock="stock", unit="USD", xbrl_tags=("X",))
    rows = extract_quarterly(facts, [item], ticker="T", fiscal_years=[fy])
    q2 = next(r for r in rows if r.fiscal_quarter == "Q2")
    assert q2.values["x"].value == 100


# ──────────────────────────────────────────────────────────────────────────
# YTD-subtraction fallback (cash flow items)
# ──────────────────────────────────────────────────────────────────────────


def _ytd_fact(tag: str, val: float, fy: int, fp: str, months: int, fy_start: date) -> Fact:
    """Build a YTD flow fact: start = FY start, end = quarter-end."""
    month_ends = {
        3: fy_start.replace(month=fy_start.month + 2 if fy_start.month <= 10 else (fy_start.month + 2 - 12)),
    }
    # Simpler: pass days
    end = date.fromordinal(fy_start.toordinal() + months * 30 + 1)
    return _fact(
        tag=tag, val=val, end=end, start=fy_start, fy=fy, fp=fp,
        form="10-Q",
    )


def test_q2_flow_ytd_subtraction_when_no_3mo():
    """If only YTD facts are reported (typical for cash flow), Q2 = 6mo − 3mo."""
    fy = 2024
    fy_start = date(2023, 10, 1)
    q1_ytd = _fact(tag="CFO", val=100, end=date(2023, 12, 31), start=fy_start,
                   fy=fy, fp="Q1", form="10-Q")  # 92 days, ≈ 3mo
    q2_ytd = _fact(tag="CFO", val=250, end=date(2024, 3, 31), start=fy_start,
                   fy=fy, fp="Q2", form="10-Q")  # 183 days, ≈ 6mo
    facts = _make_facts(facts_by_tag_unit={("CFO", "USD"): [q1_ytd, q2_ytd]})

    item = Item(key="cfo", display_name="CFO", statement="cash_flow",
                flow_or_stock="flow", unit="USD", xbrl_tags=("CFO",))
    rows = extract_quarterly(facts, [item], ticker="T", fiscal_years=[fy])
    by_q = {r.fiscal_quarter: r.values["cfo"].value for r in rows}
    assert by_q["Q1"] == 100  # 3-month direct
    assert by_q["Q2"] == 150  # 250 − 100


def test_q3_flow_ytd_subtraction():
    """Q3 standalone = 9mo YTD − 6mo YTD."""
    fy = 2024
    fy_start = date(2023, 10, 1)
    q1 = _fact(tag="CFO", val=100, end=date(2023, 12, 31), start=fy_start,
               fy=fy, fp="Q1", form="10-Q")
    q2 = _fact(tag="CFO", val=250, end=date(2024, 3, 31), start=fy_start,
               fy=fy, fp="Q2", form="10-Q")
    q3 = _fact(tag="CFO", val=420, end=date(2024, 6, 30), start=fy_start,
               fy=fy, fp="Q3", form="10-Q")  # 273 days, ≈ 9mo
    facts = _make_facts(facts_by_tag_unit={("CFO", "USD"): [q1, q2, q3]})

    item = Item(key="cfo", display_name="CFO", statement="cash_flow",
                flow_or_stock="flow", unit="USD", xbrl_tags=("CFO",))
    rows = extract_quarterly(facts, [item], ticker="T", fiscal_years=[fy])
    by_q = {r.fiscal_quarter: r.values["cfo"].value for r in rows}
    assert by_q["Q3"] == 170  # 420 − 250


def test_3mo_direct_preferred_over_ytd_subtraction():
    """When both 3-month and YTD facts exist, 3-month direct wins (no derivation)."""
    fy = 2024
    fy_start = date(2023, 10, 1)
    # 3-month standalone for Q2: start=Jan 1, end=Mar 31
    q2_3mo = _fact(tag="Rev", val=42, end=date(2024, 3, 31), start=date(2024, 1, 1),
                   fy=fy, fp="Q2", form="10-Q")
    # 6-month YTD: start=Oct 1, end=Mar 31
    q1_ytd = _fact(tag="Rev", val=30, end=date(2023, 12, 31), start=fy_start,
                   fy=fy, fp="Q1", form="10-Q")
    q2_ytd = _fact(tag="Rev", val=100, end=date(2024, 3, 31), start=fy_start,
                   fy=fy, fp="Q2", form="10-Q")
    facts = _make_facts(facts_by_tag_unit={("Rev", "USD"): [q2_3mo, q1_ytd, q2_ytd]})

    item = Item(key="rev", display_name="Rev", statement="income",
                flow_or_stock="flow", unit="USD", xbrl_tags=("Rev",))
    rows = extract_quarterly(facts, [item], ticker="T", fiscal_years=[fy])
    q2 = next(r for r in rows if r.fiscal_quarter == "Q2").values["rev"].value
    # Should be the 3-month direct value (42), not the YTD diff (100 − 30 = 70).
    assert q2 == 42


# ──────────────────────────────────────────────────────────────────────────
# In-progress fiscal year (10-Qs filed but no 10-K yet)
# ──────────────────────────────────────────────────────────────────────────


def _quarter_10q_fact(tag: str, val: float, fy: int, fp: str, end: date) -> Fact:
    """Build a Q1/Q2/Q3 10-Q fact (3-month, start = quarter start)."""
    months_before = {"Q1": 3, "Q2": 3, "Q3": 3}[fp]
    return _fact(
        tag=tag,
        val=val,
        end=end,
        start=date.fromordinal(end.toordinal() - 90),
        fy=fy,
        fp=fp,
        form="10-Q",
    )


def test_discover_includes_in_progress_year_with_filed_quarters_only():
    """5 completed years + 1 in-progress year (Q1+Q2 only) → 22 quarters."""
    facts_data: dict[tuple[str, str], list[Fact]] = {("Revenues", "USD"): []}
    # 5 completed FYs
    for fy in (2021, 2022, 2023, 2024, 2025):
        facts_data[("Revenues", "USD")].append(
            _annual_fact("Revenues", 100, fy, date(fy, 9, 30))
        )
    # In-progress FY2026: only Q1 and Q2 10-Qs filed
    facts_data[("Revenues", "USD")].append(
        _quarter_10q_fact("Revenues", 200, 2026, "Q1", date(2025, 12, 27))
    )
    facts_data[("Revenues", "USD")].append(
        _quarter_10q_fact("Revenues", 210, 2026, "Q2", date(2026, 3, 28))
    )
    facts = _make_facts(facts_by_tag_unit=facts_data)

    quarters = discover_quarters_to_extract(facts, n_completed=5)
    # Completed: 5 × 4 = 20. In-progress: 2.
    assert len(quarters) == 22
    in_progress = [q for q in quarters if q[0] == 2026]
    assert in_progress == [(2026, "Q1"), (2026, "Q2")]
    # No Q3 or Q4 of in-progress year.
    assert (2026, "Q3") not in quarters
    assert (2026, "Q4") not in quarters


def test_discover_excludes_in_progress_year_if_10k_also_exists():
    """If a year has both 10-K and 10-Qs, it's a completed year — not in-progress."""
    facts_data: dict[tuple[str, str], list[Fact]] = {("Revenues", "USD"): [
        _annual_fact("Revenues", 100, 2024, date(2024, 9, 30)),
        _quarter_10q_fact("Revenues", 25, 2024, "Q1", date(2023, 12, 30)),
    ]}
    facts = _make_facts(facts_by_tag_unit=facts_data)
    quarters = discover_quarters_to_extract(facts, n_completed=5)
    # All 4 quarters of 2024, no separate "in-progress" entries.
    fy_2024 = [q for q in quarters if q[0] == 2024]
    assert set(q[1] for q in fy_2024) == {"Q1", "Q2", "Q3", "Q4"}


def test_discover_picks_most_recent_in_progress_year_only():
    """If multiple years have 10-Qs but no 10-K, only the most recent is included."""
    facts_data: dict[tuple[str, str], list[Fact]] = {("Revenues", "USD"): [
        # No 10-Ks for 2025 or 2026
        _quarter_10q_fact("Revenues", 50, 2025, "Q1", date(2024, 12, 28)),
        _quarter_10q_fact("Revenues", 70, 2026, "Q1", date(2025, 12, 27)),
    ]}
    facts = _make_facts(facts_by_tag_unit=facts_data)
    quarters = discover_quarters_to_extract(facts, n_completed=5)
    # 2025 not included because 2026 is the most-recent in-progress year.
    assert any(q[0] == 2026 for q in quarters)
    assert not any(q[0] == 2025 for q in quarters)


def test_extract_emits_only_filed_in_progress_quarters():
    """End-to-end: extract should produce rows only for in-progress quarters with data."""
    facts_data: dict[tuple[str, str], list[Fact]] = {("Revenues", "USD"): [
        _annual_fact("Revenues", 400, 2025, date(2025, 9, 30)),
        # Provide annual Q1-Q3 for 2025 so Q4 derivation works
        _quarter_10q_fact("Revenues", 100, 2025, "Q1", date(2024, 12, 28)),
        _quarter_10q_fact("Revenues", 100, 2025, "Q2", date(2025, 3, 29)),
        _quarter_10q_fact("Revenues", 100, 2025, "Q3", date(2025, 6, 28)),
        # In-progress FY2026: only Q1 filed
        _quarter_10q_fact("Revenues", 150, 2026, "Q1", date(2025, 12, 27)),
    ]}
    facts = _make_facts(facts_by_tag_unit=facts_data)

    item = Item(key="revenue", display_name="Revenue", statement="income",
                flow_or_stock="flow", unit="USD", xbrl_tags=("Revenues",))
    rows = extract_quarterly(facts, [item], ticker="TEST")
    # 2025: 4 rows, 2026: 1 row → 5 rows total
    assert len(rows) == 5
    in_progress_rows = [r for r in rows if r.fiscal_year == 2026]
    assert len(in_progress_rows) == 1
    assert in_progress_rows[0].fiscal_quarter == "Q1"
    assert in_progress_rows[0].values["revenue"].value == 150


def test_discover_unions_years_across_probe_tags():
    """When an issuer changes tags mid-history, we union years across probes."""
    facts = _make_facts(
        facts_by_tag_unit={
            ("Revenues", "USD"): [
                _annual_fact("Revenues", 100, 2017, date(2017, 9, 30)),
                _annual_fact("Revenues", 110, 2018, date(2018, 9, 30)),
            ],
            ("RevenueFromContractWithCustomerExcludingAssessedTax", "USD"): [
                _annual_fact(
                    "RevenueFromContractWithCustomerExcludingAssessedTax",
                    300, 2023, date(2023, 9, 30),
                ),
                _annual_fact(
                    "RevenueFromContractWithCustomerExcludingAssessedTax",
                    400, 2024, date(2024, 9, 30),
                ),
            ],
        }
    )
    years = discover_recent_fiscal_years(facts, n=5)
    assert years == [2024, 2023, 2018, 2017]
