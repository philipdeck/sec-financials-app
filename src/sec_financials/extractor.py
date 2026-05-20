"""Core extraction logic: turn raw companyfacts into per-quarter rows.

Period model
------------
For each item × (fiscal_year, fiscal_quarter) we produce one value plus a
source descriptor (which filing(s) it came from).

Flow items (income statement, cash flow):
  - Q1 / Q2 / Q3: take the 3-month standalone value from the 10-Q filing
    for that fiscal period. Most income statement items report this
    directly. (YTD subtraction is implemented for items that don't, used
    in M2 for cash flow.)
  - Q4: derived as `annual 10-K − (Q1+Q2+Q3)`. This is the M1 simplification
    documented in REQUIREMENTS.md §5.5 as the fallback when 8-K parsing
    isn't available. 8-K-based Q4 sourcing is a later milestone.

Stock items (balance sheet):
  - Period-end balance for each (fy, fq). (Not used in M1 — extractor will
    handle them once the balance-sheet milestone lands.)

Restated values: when a period is reported by multiple filings (original
10-Q, later 10-K/A, etc.), the most recently `filed` value wins.
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


def _extract_single_tag_quarter(
    facts: CompanyFacts,
    tag: str,
    fiscal_year: int,
    fiscal_quarter: str,
    prior_quarter_values: dict[str, float],
    unit: str = "USD",
) -> tuple[float | None, list[ValueSource], date | None, str]:
    """Find the per-tag value for one (fy, fq), handling Q4 derivation.

    `prior_quarter_values` maps "Q1"/"Q2"/"Q3" to already-resolved values
    for the same tag, used for Q4 derivation. Pass an empty dict for
    Q1–Q3.

    Returns (value, sources, period_end, note).
    """
    if fiscal_quarter in ("Q1", "Q2", "Q3"):
        fact = _three_month_value(facts, tag, fiscal_year, fiscal_quarter, unit)
        if fact is None:
            return None, [], None, ""
        src = ValueSource(
            accession=fact.accession,
            form=fact.form,
            filed=fact.filed,
            tag=tag,
            description=f"{fact.form} 3-month direct",
        )
        return fact.val, [src], fact.end, ""

    assert fiscal_quarter == "Q4"
    annual = _annual_value(facts, tag, fiscal_year, unit)
    if annual is None:
        return None, [], None, ""
    q1 = prior_quarter_values.get("Q1")
    q2 = prior_quarter_values.get("Q2")
    q3 = prior_quarter_values.get("Q3")
    if None in (q1, q2, q3) or len(prior_quarter_values) < 3:
        # Can't derive Q4 without all three earlier quarters.
        return (
            None,
            [],
            annual.end,
            f"Q4 not derivable for {tag}: missing one of Q1/Q2/Q3",
        )
    q4_val = annual.val - q1 - q2 - q3  # type: ignore[operator]
    src = ValueSource(
        accession=annual.accession,
        form=annual.form,
        filed=annual.filed,
        tag=tag,
        description="Q4 derived = 10-K annual − (Q1+Q2+Q3)",
    )
    return q4_val, [src], annual.end, ""


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

    Handles both direct-tag items and derived items. For derived items the
    Q4 derivation runs per component tag, then the components are combined.
    """
    unit = item.unit

    if item.derivation is not None:
        # Resolve each component tag independently.
        components: dict[
            str, tuple[float | None, list[ValueSource], date | None]
        ] = {}
        for tag in item.derivation.add + item.derivation.subtract:
            # For Q4 of a derived item, re-resolve Q1-Q3 PER-TAG (each
            # component contributes its own Q4 derivation).
            prior_for_tag: dict[str, float] = {}
            if fiscal_quarter == "Q4":
                for q in ("Q1", "Q2", "Q3"):
                    f = _three_month_value(facts, tag, fiscal_year, q, unit)
                    if f is not None:
                        prior_for_tag[q] = f.val

            val, sources, pe, _note = _extract_single_tag_quarter(
                facts, tag, fiscal_year, fiscal_quarter, prior_for_tag, unit
            )
            components[tag] = (val, sources, pe)

        value, sources, period_end = _combine_derivation(components, item.derivation)
        note = ""
        if value is None:
            note = f"{item.key}: no component tags reported"
        return value, period_end, sources, note

    # Direct-tag item — try the fallback list in order, per period.
    # Issuers sometimes switch concept tags mid-history (e.g. Apple moved
    # from SalesRevenueNet to RevenueFromContractWithCustomer... at ASC
    # 606 adoption). For Q1-Q3 we look at each tag independently. For
    # Q4, we use cross-tag Q1-Q3 values from the same item — otherwise the
    # mid-history transition makes Q4 underivable.
    assert item.xbrl_tags is not None

    # `period_avg` items (e.g. weighted-average diluted shares) are NOT
    # additive across quarters — the annual 10-K reports a full-year
    # weighted average that doesn't equal Q1+Q2+Q3+Q4. Leave Q4 blank in
    # M1 and record the reason. A later milestone (8-K parsing) can source
    # Q4 directly.
    if fiscal_quarter == "Q4" and item.flow_or_stock == "period_avg":
        return (
            None,
            None,
            [],
            f"{item.key}: Q4 not derivable (period_avg, requires 8-K source)",
        )

    if fiscal_quarter == "Q4":
        # Pull already-resolved Q1-Q3 values for THIS ITEM (any tag).
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

        # Find an annual 10-K value under any tag in the fallback list.
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
            src = ValueSource(
                accession=annual.accession,
                form=annual.form,
                filed=annual.filed,
                tag=tag,
                description="Q4 derived = 10-K annual − (Q1+Q2+Q3)",
            )
            return q4_val, annual.end, [src], ""
        return None, None, [], f"{item.key}: no annual 10-K value found"

    # Q1 / Q2 / Q3: try each tag in order, first match wins.
    for tag in item.xbrl_tags:
        if not facts.has_tag(tag):
            continue
        value, sources, period_end, note = _extract_single_tag_quarter(
            facts, tag, fiscal_year, fiscal_quarter, {}, unit
        )
        if value is not None:
            return value, period_end, sources, note

    return None, None, [], f"{item.key}: no reporting tag found"
