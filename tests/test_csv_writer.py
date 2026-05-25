"""Tests for the new identifier-column helpers in csv_writer.

Covers round_to_nearest_month_end, qtr_num_from_date, excel_serial.
The values come from the user's spec in REQUIREMENTS.md §5.3:
  - NVDA Q1 FY2027 ending Apr 27 2026 → rounded Apr 30 2026
  - Excel serial for Apr 30 2026 → 46142
  - Qtr Num for Dec 31 2024 → 20
  - Qtr Num for Apr 30 2026 → 25
"""

from __future__ import annotations

from datetime import date

import pytest

from sec_financials.csv_writer import (
    _GUIDANCE_COLUMNS,
    _fiscal_q_int,
    build_main_csv,
    excel_serial,
    qtr_num_from_date,
    round_to_nearest_month_end,
)


# ──────────────────────────────────────────────────────────────────────────
# round_to_nearest_month_end
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Already month-end → unchanged
        (date(2024, 9, 30), date(2024, 9, 30)),
        (date(2026, 1, 31), date(2026, 1, 31)),
        # NVDA's fiscal Q1 FY2027 ends Apr 27 2026 → Apr 30 (3 days forward)
        (date(2026, 4, 27), date(2026, 4, 30)),
        # Early in month rounds backward
        (date(2024, 9, 3), date(2024, 8, 31)),
        (date(2025, 1, 5), date(2024, 12, 31)),
        # Midmonth: 15 → tie / 16 → round forward (we go forward on ties)
        (date(2024, 3, 16), date(2024, 3, 31)),
        # Apple's Dec 30 → Dec 31
        (date(2023, 12, 30), date(2023, 12, 31)),
        # Late February in a non-leap year
        (date(2023, 2, 27), date(2023, 2, 28)),
    ],
)
def test_round_to_nearest_month_end(raw: date, expected: date):
    assert round_to_nearest_month_end(raw) == expected


# ──────────────────────────────────────────────────────────────────────────
# qtr_num_from_date
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rounded,expected",
    [
        # Anchor: Mar 31 2020 starts Qtr Num 1
        (date(2020, 3, 31), 1),
        (date(2020, 4, 30), 1),
        (date(2020, 5, 31), 1),
        # Jun-Aug 2020 = 2
        (date(2020, 6, 30), 2),
        (date(2020, 8, 31), 2),
        # Sep-Nov 2020 = 3
        (date(2020, 9, 30), 3),
        # Dec 2020 - Feb 2021 = 4
        (date(2020, 12, 31), 4),
        (date(2021, 2, 28), 4),
        # User-supplied anchors from the spec
        (date(2024, 12, 31), 20),
        (date(2026, 4, 30), 25),
    ],
)
def test_qtr_num_from_date(rounded: date, expected: int):
    assert qtr_num_from_date(rounded) == expected


# ──────────────────────────────────────────────────────────────────────────
# excel_serial
# ──────────────────────────────────────────────────────────────────────────


def test_excel_serial_matches_spec_example():
    # User: "Nvidia for Q1 of fiscal 2027 would be NVDA46142, as the date
    # of 30 April 2026 was the fiscal end date rounded to the nearest
    # month end."
    assert excel_serial(date(2026, 4, 30)) == 46142


def test_excel_serial_a_few_known_anchors():
    # Excel itself: 1900-01-01 = serial 1 (because of the 1900 leap-year bug,
    # serial 60 corresponds to the non-existent Feb 29 1900, then Mar 1 = 61).
    assert excel_serial(date(1900, 1, 1)) == 2
    # Jan 1 2000 = 36526 in Excel
    assert excel_serial(date(2000, 1, 1)) == 36526


# ──────────────────────────────────────────────────────────────────────────
# _fiscal_q_int
# ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("s,n", [("Q1", 1), ("Q2", 2), ("Q3", 3), ("Q4", 4)])
def test_fiscal_q_int(s: str, n: int):
    assert _fiscal_q_int(s) == n


# ──────────────────────────────────────────────────────────────────────────
# build_main_csv: header structure
# ──────────────────────────────────────────────────────────────────────────


def test_main_csv_header_starts_with_seven_identifier_columns():
    # Empty rows is fine — header is independent of data.
    csv_str = build_main_csv(rows=[], items=[])
    header = csv_str.splitlines()[0].split(",")
    assert header[:7] == [
        "Ticker",
        "Qtr Num",
        "FiscalQ",
        "Fiscal Date",
        "Reporting Date",
        "ConcatDate",
        "Concat",
    ]


def test_main_csv_header_includes_guidance_columns_then_notes():
    csv_str = build_main_csv(rows=[], items=[])
    header = csv_str.splitlines()[0].split(",")
    # Guidance group of 6 + notes at the end
    assert header[-7:] == [*_GUIDANCE_COLUMNS, "notes"]
    assert len(_GUIDANCE_COLUMNS) == 6


def test_main_csv_header_uses_display_names_for_metric_columns():
    from sec_financials.config import Item

    items = [
        Item(
            key="revenue",
            display_name="Revenue",
            statement="income",
            flow_or_stock="flow",
            unit="USD",
            xbrl_tags=("Revenues",),
        ),
        Item(
            key="cost_of_sales",
            display_name="COS",
            statement="income",
            flow_or_stock="flow",
            unit="USD",
            xbrl_tags=("CostOfRevenue",),
        ),
    ]
    csv_str = build_main_csv(rows=[], items=items)
    header = csv_str.splitlines()[0].split(",")
    # 7 identifier cols + 2 metric cols + 6 guidance + 1 notes
    assert len(header) == 7 + 2 + 6 + 1
    assert header[7:9] == ["Revenue", "COS"]


# ──────────────────────────────────────────────────────────────────────────
# build_main_csv: row content
# ──────────────────────────────────────────────────────────────────────────


def test_main_csv_row_computes_identifiers_for_nvda_q1_fy2027():
    """Spec example: NVDA Q1 FY2027 ending Apr 27 2026.
    Fiscal Date = 2026-04-30, Qtr Num = 25, ConcatDate = NVDA46142,
    Concat = NVDA25.
    """
    from sec_financials.config import Item
    from sec_financials.extractor import ExtractedValue, QuarterRow

    items = [
        Item(
            key="revenue",
            display_name="Revenue",
            statement="income",
            flow_or_stock="flow",
            unit="USD",
            xbrl_tags=("Revenues",),
        ),
    ]
    row = QuarterRow(
        ticker="NVDA",
        fiscal_year=2027,
        fiscal_quarter="Q1",
        period_end=date(2026, 4, 27),
        values={
            "revenue": ExtractedValue(
                value=44_062_000_000, period_end=date(2026, 4, 27)
            )
        },
    )
    csv_str = build_main_csv(rows=[row], items=items)
    data_row = csv_str.splitlines()[1].split(",")
    assert data_row[0] == "NVDA"            # Ticker
    assert data_row[1] == "25"              # Qtr Num
    assert data_row[2] == "1"               # FiscalQ
    assert data_row[3] == "2026-04-30"      # Fiscal Date (rounded)
    # Reporting Date is blank — no sources on the stub ExtractedValue
    assert data_row[4] == ""
    assert data_row[5] == "NVDA46142"       # ConcatDate
    assert data_row[6] == "NVDA25"          # Concat
    assert data_row[7] == "44062"           # Revenue, in millions


def test_main_csv_row_blank_identifiers_when_period_end_missing():
    """A row with no period_end should produce empty identifier-date columns."""
    from sec_financials.extractor import QuarterRow

    row = QuarterRow(
        ticker="TEST",
        fiscal_year=2024,
        fiscal_quarter="Q4",
        period_end=None,
        values={},
    )
    csv_str = build_main_csv(rows=[row], items=[])
    data_row = csv_str.splitlines()[1].split(",")
    assert data_row[0] == "TEST"
    assert data_row[1] == ""       # Qtr Num (no period_end)
    assert data_row[2] == "4"      # FiscalQ still derivable from "Q4"
    assert data_row[3] == ""       # Fiscal Date
    assert data_row[5] == ""       # ConcatDate
    assert data_row[6] == ""       # Concat


def test_main_csv_reporting_date_is_earliest_source_filed():
    """When multiple sources contribute, Reporting Date is the EARLIEST."""
    from sec_financials.config import Item
    from sec_financials.extractor import ExtractedValue, QuarterRow, ValueSource

    items = [
        Item(
            key="x",
            display_name="X",
            statement="income",
            flow_or_stock="flow",
            unit="USD",
            xbrl_tags=("X",),
        ),
        Item(
            key="y",
            display_name="Y",
            statement="income",
            flow_or_stock="flow",
            unit="USD",
            xbrl_tags=("Y",),
        ),
    ]
    sources_x = (
        ValueSource(
            accession="a1", form="10-Q", filed=date(2024, 5, 5),
            tag="X", description="",
        ),
    )
    sources_y = (
        ValueSource(
            accession="a2", form="10-Q", filed=date(2024, 2, 1),  # earlier
            tag="Y", description="",
        ),
    )
    row = QuarterRow(
        ticker="T",
        fiscal_year=2024,
        fiscal_quarter="Q2",
        period_end=date(2024, 3, 30),
        values={
            "x": ExtractedValue(value=1, period_end=date(2024, 3, 30), sources=sources_x),
            "y": ExtractedValue(value=2, period_end=date(2024, 3, 30), sources=sources_y),
        },
    )
    csv_str = build_main_csv(rows=[row], items=items)
    data_row = csv_str.splitlines()[1].split(",")
    # Reporting Date column (index 4) should be the EARLIEST filing: Feb 1, not May 5
    assert data_row[4] == "2024-02-01"
