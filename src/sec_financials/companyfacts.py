"""Fetch and lightly index SEC EDGAR `companyfacts` data.

The companyfacts endpoint returns every XBRL fact filed by an issuer,
organized as `facts.<taxonomy>.<concept>.units.<unit>` -> list of fact
entries. This module wraps the raw JSON in lookup-friendly types; period
mapping and YTD subtraction live in the extractor, not here.

Endpoint:  https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

from sec_financials.sec_client import SEC_BASE_DATA, SECClient, SECClientError


def companyfacts_url(cik_padded: str) -> str:
    return f"{SEC_BASE_DATA}/api/xbrl/companyfacts/CIK{cik_padded}.json"


@dataclass(frozen=True)
class Fact:
    """One value reported by an issuer for a single concept × period.

    Mirrors the SEC's per-fact shape but with parsed dates and explicit
    typing. `start` is None for stock (balance-sheet) facts; `frame` is
    present only for SEC-canonical period reports.
    """

    tag: str
    unit: str
    val: float
    end: date
    start: date | None
    accession: str
    fiscal_year: int
    fiscal_period: str  # "FY" / "Q1" / "Q2" / "Q3"
    form: str  # "10-K" / "10-Q" / "8-K" / etc.
    filed: date
    frame: str | None  # e.g. "CY2024Q1", optional

    @property
    def duration_days(self) -> int | None:
        """Length of the reporting period in days, or None for stock items."""
        if self.start is None:
            return None
        return (self.end - self.start).days


@dataclass(frozen=True)
class CompanyFacts:
    """All XBRL facts for a single issuer, indexed for tag/unit lookup.

    Use `facts_for(tag, unit)` to retrieve the time series for a concept.
    """

    cik: int
    entity_name: str
    # Two-level index: (taxonomy, tag) -> {unit: [Fact, ...]}
    # The taxonomy is almost always "us-gaap" for us; "dei" carries entity
    # metadata. We index across taxonomies so callers don't have to care.
    _index: dict[tuple[str, str], dict[str, list[Fact]]]

    def facts_for(
        self, tag: str, unit: str = "USD", taxonomy: str = "us-gaap"
    ) -> list[Fact]:
        """Return the time series for a concept tag, or `[]` if not present.

        The returned list is in the SEC's natural order (chronological per
        filing). Callers that need most-recent-filing-wins behaviour should
        sort by `filed` themselves.
        """
        return list(self._index.get((taxonomy, tag), {}).get(unit, ()))

    def has_tag(self, tag: str, taxonomy: str = "us-gaap") -> bool:
        return (taxonomy, tag) in self._index


def _parse_date(s: Any) -> date:
    """Parse an ISO-8601 date (YYYY-MM-DD)."""
    if not isinstance(s, str):
        raise SECClientError(f"Expected ISO date string, got {type(s).__name__}")
    return date.fromisoformat(s)


def _build_index(
    facts_block: Any,
) -> dict[tuple[str, str], dict[str, list[Fact]]]:
    """Walk the `facts` block and build the (taxonomy, tag) → unit → [Fact] index."""
    if not isinstance(facts_block, dict):
        return {}

    index: dict[tuple[str, str], dict[str, list[Fact]]] = {}
    for taxonomy, tags in facts_block.items():
        if not isinstance(tags, dict):
            continue
        for tag, payload in tags.items():
            units = payload.get("units") if isinstance(payload, dict) else None
            if not isinstance(units, dict):
                continue
            unit_to_facts: dict[str, list[Fact]] = {}
            for unit, entries in units.items():
                if not isinstance(entries, list):
                    continue
                facts: list[Fact] = []
                for entry in entries:
                    if not isinstance(entry, dict):
                        continue
                    try:
                        facts.append(
                            Fact(
                                tag=tag,
                                unit=unit,
                                val=float(entry["val"]),
                                end=_parse_date(entry["end"]),
                                start=(
                                    _parse_date(entry["start"])
                                    if entry.get("start") is not None
                                    else None
                                ),
                                accession=str(entry.get("accn", "")),
                                fiscal_year=int(entry.get("fy", 0)),
                                fiscal_period=str(entry.get("fp", "")),
                                form=str(entry.get("form", "")),
                                filed=_parse_date(entry["filed"]),
                                frame=(
                                    str(entry["frame"])
                                    if entry.get("frame") is not None
                                    else None
                                ),
                            )
                        )
                    except (KeyError, ValueError, TypeError):
                        # Skip malformed entries; don't fail the whole load.
                        continue
                if facts:
                    unit_to_facts[unit] = facts
            if unit_to_facts:
                index[(taxonomy, tag)] = unit_to_facts
    return index


def fetch_company_facts(client: SECClient, cik_padded: str) -> CompanyFacts:
    """Fetch and parse the companyfacts JSON for a given CIK.

    Args:
        client: A configured SECClient.
        cik_padded: 10-digit zero-padded CIK string (e.g. "0000320193").

    Returns:
        A CompanyFacts object with the full fact index.

    Raises:
        SECClientError: on HTTP failure or malformed top-level JSON.
    """
    url = companyfacts_url(cik_padded)
    raw = client.get_json(url)
    if not isinstance(raw, dict):
        raise SECClientError(
            f"companyfacts response was not a JSON object (got {type(raw).__name__})"
        )

    try:
        cik = int(raw.get("cik", 0))
    except (TypeError, ValueError):
        cik = 0
    entity_name = str(raw.get("entityName", ""))
    facts_block = raw.get("facts", {})
    index = _build_index(facts_block)

    return CompanyFacts(cik=cik, entity_name=entity_name, _index=index)
