"""Stock-split detection and back-adjustment for share-count items.

The SEC's `companyfacts` preserves the original pre-split share counts
for periods that aren't restated by a later filing — typically anything
older than the comparable-period window in the most recent 10-K (1–2
fiscal years before the split). To produce a comparable time series we
detect splits heuristically and back-adjust all earlier values by the
cumulative multiplier.

Algorithm
---------
Walk the per-item share-count time series newest → oldest. At each
transition (newer / older), if the ratio matches a clean split factor
(2, 3, 4, 5, 6, 7, 8, 10 or their reciprocals for reverse splits)
within ±3%, declare a split and multiply ALL earlier values by that
factor. Compounding splits work naturally because each detection
operates on the already-adjusted values from earlier passes.

Limitations
-----------
- Cannot detect non-integer events like a 10% stock dividend.
- Won't fire on splits with very uneven quarter-to-quarter share counts
  (e.g. an issuer mid-buyback that happens to also do a split — rare).
- Filers that properly populate `StockSplitConversionRatio` could be
  handled directly, but XBRL coverage of that tag is unreliable in
  practice, so we don't use it.
"""

from __future__ import annotations

from typing import Sequence

from sec_financials.config import Item
from sec_financials.extractor import ExtractedValue, QuarterRow

# Clean split ratios we'll detect. Forward splits expand share count;
# the reciprocals (1/2, 1/3, …) catch reverse splits.
_FORWARD_SPLITS: tuple[int, ...] = (2, 3, 4, 5, 6, 7, 8, 10)
_TOLERANCE = 0.03  # ±3% of the target ratio


def _detect_split_ratio(newer: float, older: float) -> float | None:
    """Return the split multiplier if (newer/older) matches a clean split.

    Returns the factor by which older-period values should be multiplied
    to be comparable to the newer-period values (i.e. the split ratio
    itself for a forward split, or 1/K for a reverse).
    """
    if older == 0:
        return None
    ratio = newer / older
    for k in _FORWARD_SPLITS:
        if abs(ratio - k) / k < _TOLERANCE:
            return float(k)
        inv = 1 / k
        if abs(ratio - inv) / inv < _TOLERANCE:
            return inv
    return None


def adjust_for_splits(
    rows: list[QuarterRow], items: Sequence[Item]
) -> list[QuarterRow]:
    """Apply retroactive split adjustment to share-count items.

    Mutates the `values` dict of each affected QuarterRow in place
    (QuarterRow itself is frozen, but its `values` is a mutable dict).
    Returns the same list for chaining convenience.

    Each adjusted row gets a `; split-adjusted` suffix appended to the
    relevant item's note. The source descriptors are left unchanged
    so the audit trail in the sidecar still points at the original
    filing — the row's notes column flags that the value was restated.
    """
    share_items = [i for i in items if i.unit == "shares"]
    if not share_items or not rows:
        return rows

    for item in share_items:
        # Chronological list of (row_index, value). `rows` is already
        # sorted oldest → newest by extract_quarterly.
        ts: list[tuple[int, float | None]] = []
        for idx, row in enumerate(rows):
            ev = row.values.get(item.key)
            ts.append((idx, ev.value if ev is not None else None))

        non_none = [(i, v) for i, v in ts if v is not None]
        if len(non_none) < 2:
            continue

        # Working copy keyed by row index.
        adjusted: dict[int, float] = dict(non_none)

        # Walk pairs newest-to-oldest, detect splits, propagate backward.
        for k in range(len(non_none) - 1, 0, -1):
            newer_idx, _ = non_none[k]
            older_idx, _ = non_none[k - 1]
            newer_v = adjusted[newer_idx]
            older_v = adjusted[older_idx]
            split = _detect_split_ratio(newer_v, older_v)
            if split is None:
                continue
            # Multiply every value with row_index < newer_idx by the split.
            for i in list(adjusted.keys()):
                if i < newer_idx:
                    adjusted[i] *= split

        # Write back any changed values, with a note marker.
        for orig_idx, orig_v in non_none:
            new_v = adjusted[orig_idx]
            if new_v == orig_v:
                continue
            row = rows[orig_idx]
            ev = row.values[item.key]
            note_extra = f"{item.key}: split-adjusted"
            new_note = f"{ev.note}; {note_extra}" if ev.note else note_extra
            row.values[item.key] = ExtractedValue(
                value=new_v,
                period_end=ev.period_end,
                sources=ev.sources,
                note=new_note,
            )

    return rows
