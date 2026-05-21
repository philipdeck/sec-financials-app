"""Write the main CSV + sources sidecar, zipped together.

Main CSV layout (per REQUIREMENTS.md §5.3): one row per fiscal quarter,
identifier + period columns on the left, one column per maintained item,
notes on the far right.

Sources sidecar (long format): one row per (period, item, source filing)
to support auditing of any number in the main CSV.
"""

from __future__ import annotations

import csv
import io
import zipfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from sec_financials.config import Item
from sec_financials.extractor import QuarterRow


_MILLIONS = 1_000_000


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


def build_main_csv(rows: Sequence[QuarterRow], items: Sequence[Item]) -> str:
    """Build the main CSV as a string."""
    buf = io.StringIO(newline="")
    writer = csv.writer(buf, lineterminator="\n")

    header = [
        "ticker",
        "fiscal_year",
        "fiscal_quarter",
        "period_end",
        *(item.key for item in items),
        "notes",
    ]
    writer.writerow(header)

    for row in rows:
        writer.writerow(
            [
                row.ticker,
                row.fiscal_year,
                row.fiscal_quarter,
                row.period_end.isoformat() if row.period_end else "",
                *(
                    _format_value(row.values.get(item.key).value)
                    if row.values.get(item.key) is not None
                    else ""
                    for item in items
                ),
                row.notes,
            ]
        )
    return buf.getvalue()


def build_sources_csv(rows: Sequence[QuarterRow], items: Sequence[Item]) -> str:
    """Build the long-format sources sidecar CSV as a string."""
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
                # Emit one blank-source row so missing values are still auditable.
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


def build_zip_bytes(
    rows: Sequence[QuarterRow],
    items: Sequence[Item],
    ticker: str,
    *,
    today: datetime | None = None,
) -> tuple[bytes, str]:
    """Build the main+sidecar zip in memory and return (bytes, filename).

    Used by both the CLI (which writes to disk) and the web UI (which
    streams the bytes as a download). No filesystem side effects.
    """
    today = today or datetime.now(UTC)
    date_stamp = today.strftime("%Y%m%d")
    ticker_up = ticker.upper()

    main_name = f"{ticker_up}_financials_{date_stamp}.csv"
    sidecar_name = f"{ticker_up}_sources_{date_stamp}.csv"
    zip_name = f"{ticker_up}_financials_{date_stamp}.zip"

    main_csv = build_main_csv(rows, items)
    sidecar_csv = build_sources_csv(rows, items)

    # UTF-8 with BOM, per REQUIREMENTS.md §5.3 (Excel-friendly).
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
    """Write the main + sidecar CSVs to a zip file in `out_dir`.

    Returns the path to the created zip. Thin wrapper around
    `build_zip_bytes` for the CLI.
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    data, zip_name = build_zip_bytes(rows, items, ticker, today=today)
    zip_path = out_dir / zip_name
    zip_path.write_bytes(data)
    return zip_path
