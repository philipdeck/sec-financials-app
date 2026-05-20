"""CLI entry point.

Usage:
    sec-financials TICKER [--out DIR] [--items PATH]

Produces `{TICKER}_financials_{YYYYMMDD}.zip` in --out (default: cwd),
containing the main wide-format CSV plus the long-format sources sidecar.

Requires the SEC_USER_AGENT environment variable. A `.env` file in the
working directory (or any ancestor) is loaded automatically if present.

M1 scope: income statement items only. Balance sheet and cash flow follow
in later milestones.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sec_financials.companyfacts import fetch_company_facts
from sec_financials.config import load_items
from sec_financials.csv_writer import write_zip
from sec_financials.extractor import extract_quarterly
from sec_financials.sec_client import SECClient, SECClientError
from sec_financials.tickers import TickerResolver


def _load_dotenv_if_present(start: Path) -> None:
    """Tiny dotenv-style loader. Searches `start` and its ancestors for `.env`.

    Lines of the form `KEY=value` or `KEY="value"` are set as env vars,
    only when the key is not already set. Comments and blanks are ignored.
    Quoted values strip a single pair of matching quotes.
    """
    for parent in (start, *start.parents):
        candidate = parent / ".env"
        if candidate.is_file():
            for raw_line in candidate.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if (
                    len(value) >= 2
                    and value[0] == value[-1]
                    and value[0] in ("'", '"')
                ):
                    value = value[1:-1]
                os.environ.setdefault(key, value)
            return


def _default_items_path() -> Path:
    """Find `config/items.yaml` relative to CWD (most common) or repo root."""
    cwd = Path.cwd()
    for parent in (cwd, *cwd.parents):
        candidate = parent / "config" / "items.yaml"
        if candidate.is_file():
            return candidate
    return cwd / "config" / "items.yaml"  # will FileNotFoundError downstream


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="sec-financials",
        description=(
            "Pull quarterly financial statement data from SEC EDGAR for a "
            "US stock ticker and write a CSV (plus sources sidecar) zip."
        ),
    )
    p.add_argument("ticker", help="US stock ticker (e.g. AAPL, MSFT)")
    p.add_argument(
        "--out",
        type=Path,
        default=Path.cwd(),
        help="Output directory for the .zip (default: current directory)",
    )
    p.add_argument(
        "--items",
        type=Path,
        default=None,
        help="Path to items.yaml (default: ./config/items.yaml or nearest ancestor)",
    )
    p.add_argument(
        "--statement",
        choices=("income", "balance_sheet", "cash_flow", "all"),
        default="income",
        help=(
            "Restrict to one statement type. M1 default is `income` (income "
            "statement only). Use `all` to include every item in items.yaml."
        ),
    )
    return p


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_present(Path.cwd())
    parser = _build_parser()
    args = parser.parse_args(argv)

    items_path = args.items or _default_items_path()
    try:
        items_config = load_items(items_path)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if args.statement == "all":
        items = items_config.items
    else:
        items = items_config.by_statement(args.statement)

    if not items:
        print(
            f"error: no items found for statement={args.statement!r} in {items_path}",
            file=sys.stderr,
        )
        return 2

    try:
        with SECClient() as client:
            resolver = TickerResolver(client)
            try:
                company = resolver.resolve(args.ticker)
            except SECClientError as e:
                print(f"error: {e}", file=sys.stderr)
                return 1

            print(
                f"Resolved {company.ticker} → CIK {company.cik} ({company.title})",
                file=sys.stderr,
            )

            facts = fetch_company_facts(client, company.cik_padded)
            print(
                f"Fetched companyfacts for {facts.entity_name or company.title}",
                file=sys.stderr,
            )

            rows = extract_quarterly(facts, items, ticker=company.ticker)
            if not rows:
                print(
                    "error: no fiscal years discovered — the issuer may not have "
                    "filed an XBRL 10-K, or the SEC mapping is empty",
                    file=sys.stderr,
                )
                return 1

            zip_path = write_zip(rows, items, ticker=company.ticker, out_dir=args.out)
    except SECClientError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    print(zip_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
