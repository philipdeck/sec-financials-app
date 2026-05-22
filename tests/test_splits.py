"""Tests for stock-split detection and back-adjustment."""

from __future__ import annotations

from datetime import date

import pytest

from sec_financials.config import Item
from sec_financials.extractor import ExtractedValue, QuarterRow
from sec_financials.splits import _detect_split_ratio, adjust_for_splits


# ──────────────────────────────────────────────────────────────────────────
# _detect_split_ratio
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "newer,older,expected",
    [
        # Exact clean splits
        (2000, 1000, 2.0),
        (3000, 1000, 3.0),
        (4000, 1000, 4.0),
        (10000, 1000, 10.0),
        # Within 3% tolerance
        (10100, 1000, 10.0),
        (9800, 1000, 10.0),
        # Reverse splits
        (500, 1000, 0.5),
        (100, 1000, 0.1),
        # No split — small organic change
        (1010, 1000, None),
        (980, 1000, None),
        # No split — unusual ratio that doesn't match any clean factor
        (1500, 1000, None),
        # Edge: dividing by zero is caught
        (1000, 0, None),
    ],
)
def test_detect_split_ratio(newer, older, expected):
    assert _detect_split_ratio(newer, older) == expected


# ──────────────────────────────────────────────────────────────────────────
# adjust_for_splits
# ──────────────────────────────────────────────────────────────────────────


def _share_item() -> Item:
    return Item(
        key="shares_diluted",
        display_name="Shares",
        statement="income",
        flow_or_stock="period_avg",
        unit="shares",
        xbrl_tags=("WeightedAverageNumberOfDilutedSharesOutstanding",),
    )


def _row(fy: int, fq: str, value: float | None) -> QuarterRow:
    ev = ExtractedValue(value=value, period_end=date(fy, 6, 30))
    return QuarterRow(
        ticker="TEST",
        fiscal_year=fy,
        fiscal_quarter=fq,
        period_end=date(fy, 6, 30),
        values={"shares_diluted": ev},
    )


def _values(rows: list[QuarterRow]) -> list[float | None]:
    return [r.values["shares_diluted"].value for r in rows]


def test_no_split_leaves_values_unchanged():
    rows = [
        _row(2023, "Q1", 1000),
        _row(2023, "Q2", 998),
        _row(2023, "Q3", 1005),
        _row(2023, "Q4", 1002),
    ]
    adjust_for_splits(rows, [_share_item()])
    assert _values(rows) == [1000, 998, 1005, 1002]


def test_detects_simple_10_for_1_split():
    """One forward split between Q2 and Q3 — values before Q3 ×10."""
    rows = [
        _row(2023, "Q1", 1000),
        _row(2023, "Q2", 1010),
        _row(2023, "Q3", 10100),  # split happened here
        _row(2023, "Q4", 10050),
    ]
    adjust_for_splits(rows, [_share_item()])
    assert _values(rows) == [10000, 10100, 10100, 10050]


def test_detects_compound_splits():
    """Two splits in series (4:1 then 10:1) — earliest value gets ×40."""
    rows = [
        _row(2022, "Q1", 632),     # pre-4:1 split
        _row(2022, "Q2", 2532),    # post-4:1
        _row(2024, "Q4", 2489),    # still post-4:1, pre-10:1
        _row(2025, "Q2", 24848),   # post-both-splits
        _row(2025, "Q3", 24774),
    ]
    adjust_for_splits(rows, [_share_item()])
    vals = _values(rows)
    # FY2022 Q1: 632 × 4 × 10 = 25,280
    assert vals[0] == 25_280
    # FY2022 Q2: 2532 × 10 = 25,320
    assert vals[1] == 25_320
    # FY2024 Q4: 2489 × 10 = 24,890
    assert vals[2] == 24_890
    # FY2025 Q2 onwards: unchanged
    assert vals[3] == 24_848
    assert vals[4] == 24_774


def test_handles_blank_values():
    """None values in the middle don't break detection."""
    rows = [
        _row(2023, "Q1", 1000),
        _row(2023, "Q2", None),    # missing
        _row(2023, "Q3", 10100),   # 10× → split detected vs the most recent prior non-None (Q1)
        _row(2023, "Q4", 10000),
    ]
    adjust_for_splits(rows, [_share_item()])
    vals = _values(rows)
    assert vals[0] == 10_000  # adjusted
    assert vals[1] is None     # still missing
    assert vals[2] == 10_100   # unchanged
    assert vals[3] == 10_000


def test_reverse_split_detected_and_applied():
    """1-for-10 reverse split: earlier values multiplied by 1/10."""
    rows = [
        _row(2023, "Q1", 10000),
        _row(2023, "Q2", 1000),  # reverse split (×0.1) happened here
        _row(2023, "Q3", 990),
    ]
    adjust_for_splits(rows, [_share_item()])
    vals = _values(rows)
    assert vals[0] == 1000.0   # 10000 × 0.1
    assert vals[1] == 1000
    assert vals[2] == 990


def test_does_not_touch_non_share_items():
    """Items with unit != 'shares' must be left alone."""
    revenue_item = Item(
        key="revenue",
        display_name="Revenue",
        statement="income",
        flow_or_stock="flow",
        unit="USD",
        xbrl_tags=("Revenues",),
    )
    # 10× jump that WOULD look like a split if this were a share-count item
    rows = [
        QuarterRow(
            ticker="T",
            fiscal_year=2023,
            fiscal_quarter="Q1",
            period_end=date(2023, 3, 31),
            values={"revenue": ExtractedValue(value=1000, period_end=date(2023, 3, 31))},
        ),
        QuarterRow(
            ticker="T",
            fiscal_year=2023,
            fiscal_quarter="Q2",
            period_end=date(2023, 6, 30),
            values={"revenue": ExtractedValue(value=10000, period_end=date(2023, 6, 30))},
        ),
    ]
    adjust_for_splits(rows, [revenue_item])
    assert rows[0].values["revenue"].value == 1000  # untouched
    assert rows[1].values["revenue"].value == 10000


def test_adjusted_rows_get_a_note():
    rows = [
        _row(2023, "Q1", 1000),
        _row(2023, "Q2", 10000),
    ]
    adjust_for_splits(rows, [_share_item()])
    # Q1 was adjusted; should have a note. Q2 was not.
    assert "split-adjusted" in rows[0].values["shares_diluted"].note
    assert rows[1].values["shares_diluted"].note == ""


def test_empty_rows_safe():
    adjust_for_splits([], [_share_item()])  # should not crash


def test_single_non_none_value_unchanged():
    """One non-None value → nothing to compare against, leave it alone."""
    rows = [
        _row(2023, "Q1", None),
        _row(2023, "Q2", 1000),
        _row(2023, "Q3", None),
    ]
    adjust_for_splits(rows, [_share_item()])
    assert _values(rows) == [None, 1000, None]
