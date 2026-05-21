"""Shared end-to-end pipeline: ticker -> zip bytes.

Used by both the CLI (writes the bytes to disk) and the web app (streams
them to the browser). Keeps the orchestration logic in one place so the
two entry points stay consistent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from sec_financials.companyfacts import fetch_company_facts
from sec_financials.config import Item, ItemsConfig
from sec_financials.csv_writer import build_zip_bytes
from sec_financials.extractor import extract_quarterly
from sec_financials.sec_client import SECClient
from sec_financials.tickers import TickerResolver


@dataclass(frozen=True)
class GeneratedReport:
    """In-memory result of one ticker extraction."""

    ticker: str
    entity_name: str
    cik: int
    row_count: int
    zip_bytes: bytes
    zip_filename: str


def generate_report(
    ticker: str,
    items_config: ItemsConfig,
    *,
    statement: str = "income",
    client: SECClient | None = None,
) -> GeneratedReport:
    """Run the full extraction pipeline for one ticker.

    Args:
        ticker: User-supplied ticker (case-insensitive, trimmed).
        items_config: Loaded items.yaml.
        statement: One of "income" / "balance_sheet" / "cash_flow" / "all"
            to filter which items appear in the output.
        client: Optional pre-built SECClient. If None, a default one is
            created (which reads SEC_USER_AGENT from the env).

    Returns:
        A GeneratedReport with the zip bytes and metadata.

    Raises:
        SECClientError / TickerNotFoundError: as the underlying calls do.
        ValueError: if the requested statement filter matches no items.
    """
    items = _select_items(items_config, statement)
    owns_client = client is None
    client = client or SECClient()
    try:
        resolver = TickerResolver(client)
        company = resolver.resolve(ticker)
        facts = fetch_company_facts(client, company.cik_padded)
        rows = extract_quarterly(facts, items, ticker=company.ticker)
        zip_bytes, zip_name = build_zip_bytes(rows, items, ticker=company.ticker)
        return GeneratedReport(
            ticker=company.ticker,
            entity_name=facts.entity_name or company.title,
            cik=company.cik,
            row_count=len(rows),
            zip_bytes=zip_bytes,
            zip_filename=zip_name,
        )
    finally:
        if owns_client:
            client.close()


def _select_items(items_config: ItemsConfig, statement: str) -> Sequence[Item]:
    if statement == "all":
        items = items_config.items
    else:
        items = items_config.by_statement(statement)  # type: ignore[arg-type]
    if not items:
        raise ValueError(f"No items found for statement={statement!r}")
    return items
