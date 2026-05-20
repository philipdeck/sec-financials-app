"""Core extraction logic: turn raw companyfacts into per-quarter rows.

Period model
------------
For each item × (fiscal_year, fiscal_quarter) we produce one value plus a
source descriptor (which filing(s) it came from). The handling depends on
the item's `flow_or_stock` classification (REQUIREMENTS.md §5.2):

Flow items — income statement, cash flow
  - Q1 / Q2 / Q3:
      1. Prefer 3-month standalone (start = quarter start, ~90-day span)
         from the 10-Q. Most income statement items report this directly.
      2. Fallback: YTD subtraction.
           Q1 → 3mo YTD itself.
           Q2 → 6mo YTD (Q2 10-Q) − 3mo YTD (Q1 10-Q).
           Q3 → 9mo YTD (Q3 10-Q) − 6mo YTD (Q2 10-Q).
         This is the standard pattern for cash flow items, which the SEC
         only publishes as YTD in 10-Qs (no 3-month column).
  - Q4: derived as `annual 10-K − (Q1+Q2+Q3)`. M1 fallback per
    REQUIREMENTS.md §5.5. 8-K-based Q4 sourcing is a later milestone.

Stock items — balance sheet
  - Q1 / Q2 / Q3: balance fact for the matching (fy, fp) from a 10-Q.
  - Q4: balance fact for (fy, fp=FY) from a 10-K.
  - No derivation — balances are point-in-time.

Period-average items — weighted averages (e.g. diluted shares)
  - Q1 / Q2 / Q3: same as flow.
  - Q4: blank with a note (annual weighted-avg ≠ Σ quarters).

Restated values: when a period is reported by multiple filings, the most
recently `filed` value wins.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Sequence

from sec_financials.companyfacts import CompanyFacts, Fact
from sec_financials.config import Derivation, Item

# Quarter durations: a 3-month standalone fact spans roughly one quarter.
# These windows are generous to absorb leap years and fiscal-period quirks.
_DURATION_3MO = (80, 100)
_DURATION_6MO = (160, 200)
_DURATION_9MO = (250, 290)
_DURATION_FY = (350, 380)

# Tags we try first when probing what fiscal years an issuer has filed for.
_FISCAL_YEAR_PROBE_TAGS = (
    "Revenues",
    "RevenueFromContractWithCustomerExcludingAssessedTax",
    "SalesRevenueNet",
    "Assets",
)


@dataclass(frozen=True)
class ValueSource:
    """Where one extracted value came from."""

    accession: str
    form: str
    filed: date
    tag: str
    description: str  # e.g. "10-Q direct" / "Q4 derived = 10-K − (Q1+Q2+Q3)"


@dataclass(frozen=True)
class ExtractedValue:
    """One extracted value plus its provenance."""

    value: float | None
    period_end: date | None
    sources: tuple[ValueSource, ...] = ()
    note: str = ""  # populated when something unusual happened (e.g. missing)


@dataclass(frozen=True)
class QuarterRow:
    """One row of the output CSV: one fiscal quarter × all items."""

    ticker: str
    fiscal_year: int
    fiscal_quarter: str  # "Q1" .. "Q4"
    period_end: date | None
    values: dict[str, ExtractedValue] = field(default_factory=dict)

    @property
    def notes(self) -> str:
        """Aggregated note string for the row's far-right CSV column."""
        parts = [v.note for v in self.values.values() if v.note]
        return "; ".join(parts)


# ──────────────────────────────────────────────────────────────────────────
# Fiscal-year discovery
# ──────────────────────────────────────────────────────────────────────────


def discover_recent_fiscal_years(facts: CompanyFacts, n: int = 5) -> list[int]:
    """Return the `n` most recent completed fiscal years for the issuer.

    Walks a small set of reliable concept tags looking for 10-K FY filings,
    unioning the years across tags. We can't stop at the first tag with
    data: when issuers switch concept tags mid-history (e.g. ASC 606
    revenue migration), the legacy tag only carries the older years.
    """
    years: set[int] = set()
    for tag in _FISCAL_YEAR_PROBE_TAGS:
        for f in facts.facts_for(tag):
            if f.form == "10-K" and f.fiscal_period == "FY" and f.fiscal_year > 0:
                years.add(f.fiscal_year)
    return sorted(years, reverse=True)[:n]


# ──────────────────────────────────────────────────────────────────────────
# Low-level fact selection
# ──────────────────────────────────────────────────────────────────────────


def _select_flow_fact(
    facts: Sequence[Fact],
    *,
    fiscal_year: int,
    fiscal_period: str,
    duration_range: tuple[int, int],
    forms: tuple[str, ...],
) -> Fact | None:
    """Pick the best-matching flow fact, preferring most recent filing.

    A "best-matching" fact has the requested fy / fp, a span in the
    requested duration window, and a form in the allowed set.
    """
    lo, hi = duration_range
    candidates: list[Fact] = []
    for f in facts:
        if f.fiscal_year != fiscal_year or f.fiscal_period != fiscal_period:
            continue
        if f.form not in forms:
            continue
        d = f.duration_days
        if d is None or not (lo <= d <= hi):
            continue
        candidates.append(f)
    if not candidates:
        return None
    # Sort by (end DESC, filed DESC):
    # - end matters most: a 10-Q reports both the current period and the
    #   prior-year comparable under the same fy/fp tag, distinguished only
    #   by their end date. The newer end is the current period.
    # - filed breaks ties when the same period is reported in multiple
    #   filings (e.g. original 10-Q plus later 10-K/A restatement).
    candidates.sort(key=lambda f: (f.end, f.filed), reverse=True)
    return candidates[0]


def _select_stock_fact(
    facts: Sequence[Fact],
    *,
    fiscal_year: int,
    fiscal_period: str,
    forms: tuple[str, ...],
) -> Fact | None:
    """Pick the best-matching balance-sheet (point-in-time) fact.

    Stock facts have no `start` date (instant context in XBRL). We sort
    by (end DESC, filed DESC) — same logic as flow facts, for the same
    reasons (prior-period comparatives are tagged with the same fy/fp
    but an older end; restatements share end but differ in filed date).
    """
    candidates: list[Fact] = []
    for f in facts:
        if f.fiscal_year != fiscal_year or f.fiscal_period != fiscal_period:
            continue
        if f.form not in forms:
            continue
        if f.start is not None:
            continue  # not a stock fact
        candidates.append(f)
    if not candidates:
        return None
    candidates.sort(key=lambda f: (f.end, f.filed), reverse=True)
    return candidates[0]


# ──────────────────────────────────────────────────────────────────────────
# Per-(item, period) extraction
# ──────────────────────────────────────────────────────────────────────────


def _three_month_value(
    facts: CompanyFacts,
    tag: str,
    fiscal_year: int,
    fiscal_quarter: str,
    unit: str = "USD",
) -> Fact | None:
    """3-month standalone value from a 10-Q for the given (fy, fq)."""
    return _select_flow_fact(
        facts.facts_for(tag, unit),
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_quarter,
        duration_range=_DURATION_3MO,
        forms=("10-Q", "10-Q/A"),
    )


def _annual_value(
    facts: CompanyFacts, tag: str, fiscal_year: int, unit: str = "USD"
) -> Fact | None:
    """Annual (FY) value from a 10-K for the given fiscal year."""
    return _select_flow_fact(
        facts.facts_for(tag, unit),
        fiscal_year=fiscal_year,
        fiscal_period="FY",
        duration_range=_DURATION_FY,
        forms=("10-K", "10-K/A"),
    )


def _ytd_value(
    facts: CompanyFacts,
    tag: str,
    fiscal_year: int,
    fiscal_period: str,
    months: int,
    unit: str = "USD",
) -> Fact | None:
    """YTD flow value from a 10-Q: start = FY start, end = quarter end.

    `months` is the expected YTD length in months (3, 6, or 9).
    """
    duration_range = {3: _DURATION_3MO, 6: _DURATION_6MO, 9: _DURATION_9MO}[months]
    return _select_flow_fact(
        facts.facts_for(tag, unit),
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_period,
        duration_range=duration_range,
        forms=("10-Q", "10-Q/A"),
    )


def _balance_value(
    facts: CompanyFacts,
    tag: str,
    fiscal_year: int,
    fiscal_quarter: str,
    unit: str = "USD",
) -> Fact | None:
    """Stock (balance-sheet) value at the end of a fiscal quarter.

    Q1/Q2/Q3 balances come from the corresponding 10-Q. Q4 balance comes
    from the 10-K (the SEC tags Q4-end balances with fp=FY in the 10-K).
    """
    if fiscal_quarter == "Q4":
        return _select_stock_fact(
            facts.facts_for(tag, unit),
            fiscal_year=fiscal_year,
            fiscal_period="FY",
            forms=("10-K", "10-K/A"),
        )
    return _select_stock_fact(
        facts.facts_for(tag, unit),
        fiscal_year=fiscal_year,
        fiscal_period=fiscal_quarter,
        forms=("10-Q", "10-Q/A"),
    )


def _src_from_fact(fact: Fact, description: str) -> ValueSource:
    return ValueSource(
        accession=fact.accession,
        form=fact.form,
        filed=fact.filed,
        tag=fact.tag,
        description=description,
    )


def _resolve_flow_tag_quarter(
    facts: CompanyFacts,
    tag: str,
    fiscal_year: int,
    fiscal_quarter: str,
    prior_quarter_values: dict[str, float],
    unit: str = "USD",
) -> tuple[float | None, list[ValueSource], date | None, str]:
    """Resolve a per-tag flow value for one (fy, fq).

    For Q1–Q3: try 3-month standalone first; if missing, try YTD
    subtraction (used by most cash-flow line items, which are filed
    YTD-only).

    For Q4: derive as annual − (Q1+Q2+Q3) using the supplied priors.
    `prior_quarter_values` must contain Q1/Q2/Q3 for Q4 to derive.

    Returns (value, sources, period_end, note).
    """
    # Q1-Q3: try 3-month direct, then YTD subtraction.
    if fiscal_quarter in ("Q1", "Q2", "Q3"):
        direct = _three_month_value(facts, tag, fiscal_year, fiscal_quarter, unit)
        if direct is not None:
            return direct.val, [_src_from_fact(direct, "10-Q 3-month direct")], direct.end, ""

        # YTD-subtraction fallback. SEC cash flow line items are typically
        # YTD-only in 10-Qs (no 3-month column).
        if fiscal_quarter == "Q1":
            # Q1 YTD == 3-month standalone == Q1 value. If 3-month direct
            # missed, there's no useful YTD fallback at Q1.
            return None, [], None, ""

        if fiscal_quarter == "Q2":
            ytd_6mo = _ytd_value(facts, tag, fiscal_year, "Q2", months=6, unit=unit)
            ytd_3mo = _ytd_value(facts, tag, fiscal_year, "Q1", months=3, unit=unit)
            if ytd_6mo is None or ytd_3mo is None:
                return None, [], None, ""
            return (
                ytd_6mo.val - ytd_3mo.val,
                [
                    _src_from_fact(ytd_6mo, "Q2 derived = 6mo YTD − 3mo YTD"),
                    _src_from_fact(ytd_3mo, "Q2 derived = 6mo YTD − 3mo YTD"),
                ],
                ytd_6mo.end,
                "",
            )

        # Q3
        ytd_9mo = _ytd_value(facts, tag, fiscal_year, "Q3", months=9, unit=unit)
        ytd_6mo = _ytd_value(facts, tag, fiscal_year, "Q2", months=6, unit=unit)
        if ytd_9mo is None or ytd_6mo is None:
            return None, [], None, ""
        return (
            ytd_9mo.val - ytd_6mo.val,
            [
                _src_from_fact(ytd_9mo, "Q3 derived = 9mo YTD − 6mo YTD"),
                _src_from_fact(ytd_6mo, "Q3 derived = 9mo YTD − 6mo YTD"),
            ],
            ytd_9mo.end,
            "",
        )

    # Q4 — derive from annual − (Q1+Q2+Q3) using supplied priors.
    assert fiscal_quarter == "Q4"
    annual = _annual_value(facts, tag, fiscal_year, unit)
    if annual is None:
        return None, [], None, ""
    q1 = prior_quarter_values.get("Q1")
    q2 = prior_quarter_values.get("Q2")
    q3 = prior_quarter_values.get("Q3")
    if None in (q1, q2, q3) or len(prior_quarter_values) < 3:
        return (
            None,
            [],
            annual.end,
            f"Q4 not derivable for {tag}: missing one of Q1/Q2/Q3",
        )
    q4_val = annual.val - q1 - q2 - q3  # type: ignore[operator]
    return (
        q4_val,
        [_src_from_fact(annual, "Q4 derived = 10-K annual − (Q1+Q2+Q3)")],
        annual.end,
        "",
    )


def _resolve_stock_tag_quarter(
    facts: CompanyFacts,
    tag: str,
    fiscal_year: int,
    fiscal_quarter: str,
    unit: str = "USD",
) -> tuple[float | None, list[ValueSource], date | None, str]:
    """Resolve a per-tag balance-sheet value for one (fy, fq)."""
    fact = _balance_value(facts, tag, fiscal_year, fiscal_quarter, unit)
    if fact is None:
        return None, [], None, ""
    desc = "10-K balance" if fiscal_quarter == "Q4" else "10-Q balance"
    return fact.val, [_src_from_fact(fact, desc)], fact.end, ""


def _combine_derivation(
    components: dict[str, tuple[float | None, list[ValueSource], date | None]],
    derivation: Derivation,
) -> tuple[float | None, list[ValueSource], date | None]:
    """Combine per-tag results into a derived item value.

    `components` maps each tag (from add∪subtract) to its (value, sources, period_end).
    Missing components contribute zero, as documented in items.yaml.
    """
    total: float = 0.0
    any_value = False
    all_sources: list[ValueSource] = []
    period_end: date | None = None

    for tag in derivation.add:
        val, sources, pe = components.get(tag, (None, [], None))
        if val is not None:
            total += val
            any_value = True
            all_sources.extend(sources)
            period_end = period_end or pe
    for tag in derivation.subtract:
        val, sources, pe = components.get(tag, (None, [], None))
        if val is not None:
            total -= val
            any_value = True
            all_sources.extend(sources)
            period_end = period_end or pe

    if not any_value:
        return None, [], None
    return total, all_sources, period_end


# ──────────────────────────────────────────────────────────────────────────
# Top-level extraction
# ──────────────────────────────────────────────────────────────────────────


def extract_quarterly(
    facts: CompanyFacts,
    items: Sequence[Item],
    ticker: str,
    *,
    fiscal_years: Sequence[int] | None = None,
    n_years: int = 5,
) -> list[QuarterRow]:
    """Build per-quarter rows for every item over the last `n_years` years.

    Args:
        facts: Parsed companyfacts for one issuer.
        items: The items to extract (one column per item).
        ticker: Echoed into each row.
        fiscal_years: Explicit list of fiscal years. If None, discovered
            from the issuer's filings.
        n_years: How many recent years to discover when `fiscal_years` is
            None. Ignored otherwise.

    Returns:
        Rows sorted oldest → newest, four quarters per fiscal year.
    """
    if fiscal_years is None:
        fiscal_years = discover_recent_fiscal_years(facts, n=n_years)
    years_asc = sorted(set(fiscal_years))

    rows: list[QuarterRow] = []

    for fy in years_asc:
        # We resolve quarters in order so that Q4 derivations can reference
        # Q1–Q3 values for each tag.
        # Layout: per_item_quarter[item.key][fq] = ExtractedValue
        per_item_quarter: dict[str, dict[str, ExtractedValue]] = {
            i.key: {} for i in items
        }

        for fq in ("Q1", "Q2", "Q3", "Q4"):
            row_period_end: date | None = None
            row_values: dict[str, ExtractedValue] = {}

            for item in items:
                value, period_end, sources, note = _extract_for_item(
                    facts=facts,
                    item=item,
                    fiscal_year=fy,
                    fiscal_quarter=fq,
                    prior_per_item=per_item_quarter,
                )
                extracted = ExtractedValue(
                    value=value,
                    period_end=period_end,
                    sources=tuple(sources),
                    note=note,
                )
                row_values[item.key] = extracted
                per_item_quarter[item.key][fq] = extracted
                if period_end is not None and row_period_end is None:
                    row_period_end = period_end

            rows.append(
                QuarterRow(
                    ticker=ticker.upper(),
                    fiscal_year=fy,
                    fiscal_quarter=fq,
                    period_end=row_period_end,
                    values=row_values,
                )
            )

    return rows


def _extract_for_item(
    *,
    facts: CompanyFacts,
    item: Item,
    fiscal_year: int,
    fiscal_quarter: str,
    prior_per_item: dict[str, dict[str, ExtractedValue]],
) -> tuple[float | None, date | None, list[ValueSource], str]:
    """Extract one cell: (item, fy, fq).

    Dispatches on item.flow_or_stock to pick the right resolution path,
    and on item.derivation to combine multi-tag components.
    """
    unit = item.unit
    is_stock = item.flow_or_stock == "stock"

    if item.derivation is not None:
        return _extract_derived_item(
            facts=facts,
            item=item,
            fiscal_year=fiscal_year,
            fiscal_quarter=fiscal_quarter,
            unit=unit,
            is_stock=is_stock,
        )

    assert item.xbrl_tags is not None

    # ── Stock items: balance lookup, no Q4 derivation. ────────────────────
    if is_stock:
        for tag in item.xbrl_tags:
            if not facts.has_tag(tag):
                continue
            value, sources, period_end, note = _resolve_stock_tag_quarter(
                facts, tag, fiscal_year, fiscal_quarter, unit
            )
            if value is not None:
                return value, period_end, sources, note
        return None, None, [], f"{item.key}: no balance found"

    # ── period_avg items: Q4 blank, Q1-Q3 use flow path. ─────────────────
    if fiscal_quarter == "Q4" and item.flow_or_stock == "period_avg":
        return (
            None,
            None,
            [],
            f"{item.key}: Q4 not derivable (period_avg, requires 8-K source)",
        )

    # ── Flow items: Q4 = cross-tag annual − (cross-tag Q1+Q2+Q3). ────────
    # The Q1-Q3 values come from `prior_per_item` (already resolved for
    # THIS item, possibly across different tags — required when an issuer
    # switched concept tags mid-history, e.g. ASC 606).
    if fiscal_quarter == "Q4":
        cross_tag_priors: dict[str, float] = {}
        for q in ("Q1", "Q2", "Q3"):
            ev = prior_per_item.get(item.key, {}).get(q)
            if ev is not None and ev.value is not None:
                cross_tag_priors[q] = ev.value
        if len(cross_tag_priors) < 3:
            return (
                None,
                None,
                [],
                f"{item.key}: can't derive Q4, Q1-Q3 not all resolved",
            )
        for tag in item.xbrl_tags:
            if not facts.has_tag(tag):
                continue
            annual = _annual_value(facts, tag, fiscal_year, unit)
            if annual is None:
                continue
            q4_val = (
                annual.val
                - cross_tag_priors["Q1"]
                - cross_tag_priors["Q2"]
                - cross_tag_priors["Q3"]
            )
            return (
                q4_val,
                annual.end,
                [_src_from_fact(annual, "Q4 derived = 10-K annual − (Q1+Q2+Q3)")],
                "",
            )
        return None, None, [], f"{item.key}: no annual 10-K value found"

    # ── Flow items, Q1/Q2/Q3: try each tag in order. ─────────────────────
    for tag in item.xbrl_tags:
        if not facts.has_tag(tag):
            continue
        value, sources, period_end, note = _resolve_flow_tag_quarter(
            facts, tag, fiscal_year, fiscal_quarter, {}, unit
        )
        if value is not None:
            return value, period_end, sources, note

    return None, None, [], f"{item.key}: no reporting tag found"


def _extract_derived_item(
    *,
    facts: CompanyFacts,
    item: Item,
    fiscal_year: int,
    fiscal_quarter: str,
    unit: str,
    is_stock: bool,
) -> tuple[float | None, date | None, list[ValueSource], str]:
    """Resolve each component tag, then combine via add/subtract."""
    assert item.derivation is not None

    components: dict[str, tuple[float | None, list[ValueSource], date | None]] = {}
    for tag in item.derivation.add + item.derivation.subtract:
        if is_stock:
            val, sources, pe, _note = _resolve_stock_tag_quarter(
                facts, tag, fiscal_year, fiscal_quarter, unit
            )
        else:
            # For derived flow Q4, gather this tag's own Q1-Q3 via the
            # resolver (which itself handles 3mo direct + YTD fallback).
            prior_for_tag: dict[str, float] = {}
            if fiscal_quarter == "Q4":
                for q in ("Q1", "Q2", "Q3"):
                    v, _s, _pe, _n = _resolve_flow_tag_quarter(
                        facts, tag, fiscal_year, q, {}, unit
                    )
                    if v is not None:
                        prior_for_tag[q] = v
            val, sources, pe, _note = _resolve_flow_tag_quarter(
                facts, tag, fiscal_year, fiscal_quarter, prior_for_tag, unit
            )
        components[tag] = (val, sources, pe)

    value, sources, period_end = _combine_derivation(components, item.derivation)
    note = ""
    if value is None:
        note = f"{item.key}: no component tags reported"
    return value, period_end, sources, note
