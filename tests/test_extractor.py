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
