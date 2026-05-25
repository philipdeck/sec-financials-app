"""Write the main CSV + sources sidecar, zipped together.

Main CSV schema (per REQUIREMENTS.md §5.3):

  Identifier columns (left, 7 fixed):
    Ticker, Qtr Num, FiscalQ, Fiscal Date, Reporting Date,
    ConcatDate, Concat

  Reporting columns (middle, 27, in items.yaml order):
    one column per item, using item.display_name as the header

  Guidance columns (right of reporting, 6 fixed, placeholders):
    FY Rev Guide, NQ Rev Guide, FY GM Guide, NQ GM Guide,
    FY EPS Guide, NQ EPS Guide

  Notes column (far right):
    "notes"

Sources sidecar (long format, snake_case headers): unchanged from earlier
revisions — it's an audit/debug artifact, not analyst output.
"""

from __future__ import annotations

import calendar
import csv
import io
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Sequence

from sec_financials.config import Item
from sec_financials.extractor import QuarterRow

_MILLIONS = 1_000_000

# Excel's date epoch is 1899-12-30 (the "1900 leap year" quirk shifts it
# from 1900-01-01 by two days). Apr 30 2026 == serial 46142.
_EXCEL_EPOCH = date(1899, 12, 30)

# Guidance column headers. Values are blank until 8-K parsing is built.
_GUIDANCE_COLUMNS: tuple[str, ...] = (
    "FY Rev Guide",
    "NQ Rev Guide",
    "FY GM Guide",
    "NQ GM Guide",
    "FY EPS Guide",
    "NQ EPS Guide",
)


# ──────────────────────────────────────────────────────────────────────────
# Date helpers
# ──────────────────────────────────────────────────────────────────────────


def round_to_nearest_month_end(d: date) -> date:
    """Round `d` to the nearest end-of-month date.

    Ties (equidistant) go to the current month's end.

      Apr 27 → Apr 30   (3 days forward vs 27 back)
      Sep 3  → Aug 31   (3 days back vs 27 forward)
      Mar 31 → Mar 31   (already a month-end)
    """
    last_day_curr = calendar.monthrange(d.year, d.month)[1]
    end_of_curr = date(d.year, d.month, last_day_curr)
    if d.month == 1:
        prev_y, prev_m = d.year - 1, 12
    else:
        prev_y, prev_m = d.year, d.month - 1
    end_of_prev = date(prev_y, prev_m, calendar.monthrange(prev_y, prev_m)[1])
    days_to_curr = (end_of_curr - d).days
    days_to_prev = (d - end_of_prev).days
    return end_of_curr if days_to_curr <= days_to_prev else end_of_prev


def qtr_num_from_date(d: date) -> int:
    """Serial calendar-quarter index keyed off the rounded month-end.

    qtr_num = 1 for any quarter ending Mar 31 – May 31 2020;
            = 20 for quarter ending Dec 31 2024;
            = 25 for quarter ending Apr 30 2026.

    Formula: count months since March 2020 (inclusive) and divide by 3.
    """
    months_from_mar_2020 = (d.year - 2020) * 12 + (d.month - 3)
    return months_from_mar_2020 // 3 + 1


def excel_serial(d: date) -> int:
    """Excel-compatible date serial number (1899-12-30 = 0)."""
    return (d - _EXCEL_EPOCH).days


def _fiscal_q_int(fiscal_quarter: str) -> int:
    """Convert 'Q1'/'Q2'/'Q3'/'Q4' to an int 1-4."""
    return int(fiscal_quarter.removeprefix("Q"))


def _earliest_filing_date(row: QuarterRow) -> date | None:
    """Earliest source filing date across all of the row's extracted values.

    Used for the 'Reporting Date' column. The user opted for "first
    filing date" to support predicting future filing cadences from
    historical patterns.
    """
    dates: list[date] = []
    for ev in row.values.values():
        for src in ev.sources:
            dates.append(src.filed)
    return min(dates) if dates else None


# ──────────────────────────────────────────────────────────────────────────
# Value formatting
# ──────────────────────────────────────────────────────────────────────────


def _format_value(v: float | None) -> str:
    """Render a numeric value in MILLIONS of the reported unit, blank if None.

    SEC values are filed as raw whole numbers (e.g. $111,439,000,000). We
    divide by 1,000,000 for the output so the CSV reads as
    `111439` (= $111.4 billion) rather than `111439000000`. This applies to
    both USD- and shares-unit columns — both benefit from compact display.

    Trailing zeros and trailing decimal point are stripped; integer
    millions render without a fractional part.
    """
    if v is None:
        return ""
    v_m = v / _MILLIONS
    if v_m == 0:
        return "0"
    if v_m == int(v_m):
        return str(int(v_m))
    # Up to 6 decimal places (= dollar resolution), strip trailing zeros.
    return f"{v_m:.6f}".rstrip("0").rstrip(".")


# ──────────────────────────────────────────────────────────────────────────
# Main CSV
# ──────────────────────────────────────────────────────────────────────────


def build_main_csv(rows: Sequence[QuarterRow], items: Sequence[Item]) -> str:
    """Build the main CSV as a string with the new identifier schema."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")

    header = [
        "Ticker",
        "Qtr Num",
        "FiscalQ",
        "Fiscal Date",
        "Reporting Date",
        "ConcatDate",
        "Concat",
        *(item.display_name for item in items),
        *_GUIDANCE_COLUMNS,
        "notes",
    ]
    writer.writerow(header)

    for row in rows:
        ticker_up = row.ticker.upper()
        fiscal_date = (
            round_to_nearest_month_end(row.period_end)
            if row.period_end is not None
            else None
        )
        qtr_num = qtr_num_from_date(fiscal_date) if fiscal_date is not None else None
        reporting_date = _earliest_filing_date(row)
        concat_date = (
            f"{ticker_up}{excel_serial(fiscal_date)}"
            if fiscal_date is not None
            else ""
        )
        concat = f"{ticker_up}{qtr_num}" if qtr_num is not None else ""

        writer.writerow(
            [
                ticker_up,
                qtr_num if qtr_num is not None else "",
                _fiscal_q_int(row.fiscal_quarter),
                fiscal_date.isoformat() if fiscal_date else "",
                reporting_date.isoformat() if reporting_date else "",
                concat_date,
                concat,
                *(
                    _format_value(row.values[item.key].value)
                    if row.values.get(item.key) is not None
                    else ""
                    for item in items
                ),
                *("" for _ in _GUIDANCE_COLUMNS),  # placeholders
                row.notes,
            ]
        )
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────────────────
# Sources sidecar
# ──────────────────────────────────────────────────────────────────────────


def build_sources_csv(rows: Sequence[QuarterRow], items: Sequence[Item]) -> str:
    """Build the long-format sources sidecar CSV as a string.

    Keeps snake_case identifier columns to remain machine-friendly for
    audit tooling; analyst-friendly relabeling lives only in the main CSV.
    """
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")

    writer.writerow(
        [
            "ticker",
            "fiscal_year",
            "fiscal_quarter",
            "period_end",
            "metric",
            "value",
            "tag",
            "accession_number",
            "form_type",
            "filed_date",
            "description",
        ]
    )

    for row in rows:
        for item in items:
            ev = row.values.get(item.key)
            if ev is None:
                continue
            if not ev.sources:
                if ev.value is None:
                    writer.writerow(
                        [
                            row.ticker,
                            row.fiscal_year,
                            row.fiscal_quarter,
                            row.period_end.isoformat() if row.period_end else "",
                            item.key,
                            "",
                            "",
                            "",
                            "",
                            "",
                            ev.note or "no source",
                        ]
                    )
                continue
            for src in ev.sources:
                writer.writerow(
                    [
                        row.ticker,
                        row.fiscal_year,
                        row.fiscal_quarter,
                        row.period_end.isoformat() if row.period_end else "",
                        item.key,
                        _format_value(ev.value),
                        src.tag,
                        src.accession,
                        src.form,
                        src.filed.isoformat(),
                        src.description,
                    ]
                )
    return buf.getvalue()


# ──────────────────────────────────────────────────────────────────────────
# Zip packaging (unchanged)
# ──────────────────────────────────────────────────────────────────────────


def build_zip_bytes(
    rows: Sequence[QuarterRow],
    items: Sequence[Item],
    ticker: str,
    *,
    today: datetime | None = None,
) -> tuple[bytes, str]:
    """Build the main+sidecar zip in memory and return (bytes, filename)."""
    today = today or datetime.now(UTC)
    date_stamp = today.strftime("%Y%m%d")
    ticker_up = ticker.upper()

    main_name = f"{ticker_up}_financials_{date_stamp}.csv"
    sidecar_name = f"{ticker_up}_sources_{date_stamp}.csv"
    zip_name = f"{ticker_up}_financials_{date_stamp}.zip"

    main_csv = build_main_csv(rows, items)
    sidecar_csv = build_sources_csv(rows, items)

    bom = "﻿"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(main_name, (bom + main_csv).encode("utf-8"))
        zf.writestr(sidecar_name, (bom + sidecar_csv).encode("utf-8"))
    return buf.getvalue(), zip_name


def write_zip(
    rows: Sequence[QuarterRow],
    items: Sequence[Item],
    ticker: str,
    out_dir: Path,
    *,
    today: datetime | None = None,
) -> Path:
    """CLI helper: write the zip to disk under `out_dir`."""
    out_dir.mkdir(parents=True, exist_ok=True)
    data, zip_name = build_zip_bytes(rows, items, ticker, today=today)
    zip_path = out_dir / zip_name
    zip_path.write_bytes(data)
    return zip_path
