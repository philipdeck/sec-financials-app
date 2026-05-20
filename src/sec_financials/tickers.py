"""Ticker → CIK resolution against the SEC's ticker-mapping file.

The SEC publishes a JSON file mapping every public-issuer ticker to its
Central Index Key (CIK). We fetch it once per process and keep an
in-memory dict for lookups.
"""

from __future__ import annotations

from dataclasses import dataclass

from sec_financials.sec_client import SEC_BASE_WWW, SECClient, SECClientError

TICKER_MAP_URL = f"{SEC_BASE_WWW}/files/company_tickers.json"


class TickerNotFoundError(SECClientError):
    """Raised when the supplied ticker doesn't appear in SEC's mapping file."""


@dataclass(frozen=True)
class Company:
    """One row from the SEC ticker→CIK mapping file."""

    ticker: str  # always uppercase
    cik: int
    title: str

    @property
    def cik_padded(self) -> str:
        """CIK as a 10-digit zero-padded string, as required by the data.sec.gov URLs."""
        return f"{self.cik:010d}"


class TickerResolver:
    """Wraps the SEC ticker→CIK mapping file with lazy fetch and lookup."""

    def __init__(self, client: SECClient) -> None:
        self._client = client
        self._by_ticker: dict[str, Company] | None = None

    def _ensure_loaded(self) -> dict[str, Company]:
        if self._by_ticker is not None:
            return self._by_ticker
        raw = self._client.get_json(TICKER_MAP_URL)
        if not isinstance(raw, dict):
            raise SECClientError(
                f"Unexpected ticker file shape: top-level was {type(raw).__name__}"
            )
        by_ticker: dict[str, Company] = {}
        for row in raw.values():
            try:
                ticker = str(row["ticker"]).upper()
                cik = int(row["cik_str"])
                title = str(row["title"])
            except (KeyError, TypeError, ValueError) as e:
                # Skip malformed rows but don't fail the whole load.
                continue  # noqa: B112 — defensive against SEC schema drift
            by_ticker[ticker] = Company(ticker=ticker, cik=cik, title=title)
        self._by_ticker = by_ticker
        return by_ticker

    def resolve(self, ticker: str) -> Company:
        """Look up a ticker. Case-insensitive; whitespace trimmed.

        Raises TickerNotFoundError if the ticker isn't in the SEC's mapping.
        """
        clean = ticker.strip().upper()
        if not clean:
            raise TickerNotFoundError("Empty ticker")
        mapping = self._ensure_loaded()
        try:
            return mapping[clean]
        except KeyError:
            raise TickerNotFoundError(
                f"Ticker {clean!r} not found in SEC's company_tickers.json. "
                f"Check the spelling, or confirm the issuer files with the SEC."
            ) from None
