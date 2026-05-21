"""CLI entry point.

Usage:
    sec-financials TICKER [--out DIR] [--items PATH] [--statement KIND]
    sec-financials extract TICKER ...    (same as above; explicit form)
    sec-financials serve [--host HOST] [--port PORT] [--reload]

The bare `sec-financials AAPL` is preserved as a shortcut for
`sec-financials extract AAPL` — when the first positional doesn't match a
known subcommand, it's treated as a ticker for the `extract` subcommand.

Both subcommands auto-load `.env` from the working directory (or any
ancestor) so `SEC_USER_AGENT` doesn't need to be exported manually.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sec_financials.config import load_items
from sec_financials.pipeline import generate_report
from sec_financials.sec_client import SECClientError

_SUBCOMMANDS = ("extract", "serve")


def _load_dotenv_if_present(start: Path) -> None:
    """Tiny dotenv-style loader. Searches `start` and its ancestors for `.env`."""
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
    return cwd / "config" / "items.yaml"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sec-financials",
        description=(
            "Pull quarterly financial statement data from SEC EDGAR for a "
            "US stock ticker (extract) or run the local web UI (serve)."
        ),
    )
    sub = parser.add_subparsers(dest="cmd", required=True)

    # extract --------------------------------------------------------------
    ex = sub.add_parser(
        "extract", help="One-shot CSV generation for a ticker."
    )
    ex.add_argument("ticker", help="US stock ticker (e.g. AAPL, MSFT)")
    ex.add_argument(
        "--out",
        type=Path,
        default=Path.cwd(),
        help="Output directory for the .zip (default: current directory)",
    )
    ex.add_argument(
        "--items",
        type=Path,
        default=None,
        help="Path to items.yaml (default: ./config/items.yaml or nearest ancestor)",
    )
    ex.add_argument(
        "--statement",
        choices=("income", "balance_sheet", "cash_flow", "all"),
        default="all",
        help=(
            "Restrict to one statement type. Default is `all` (all 27 items "
            "across income, balance sheet, cash flow)."
        ),
    )

    # serve ----------------------------------------------------------------
    sv = sub.add_parser(
        "serve", help="Run the local web UI on http://localhost:PORT/"
    )
    sv.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    sv.add_argument("--port", type=int, default=8000, help="Bind port (default: 8000)")
    sv.add_argument(
        "--reload",
        action="store_true",
        help="Reload on code changes (dev only).",
    )

    return parser


def _cmd_extract(args: argparse.Namespace) -> int:
    items_path = args.items or _default_items_path()
    try:
        items_config = load_items(items_path)
    except FileNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    try:
        report = generate_report(
            args.ticker,
            items_config,
            statement=args.statement,
        )
    except SECClientError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    if report.row_count == 0:
        print(
            "error: no fiscal years discovered — the issuer may not have "
            "filed an XBRL 10-K, or the SEC mapping is empty",
            file=sys.stderr,
        )
        return 1

    print(
        f"Resolved {report.ticker} → CIK {report.cik} ({report.entity_name})",
        file=sys.stderr,
    )

    args.out.mkdir(parents=True, exist_ok=True)
    zip_path = args.out / report.zip_filename
    zip_path.write_bytes(report.zip_bytes)
    print(zip_path)
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    # Import here so `extract` callers don't pay for FastAPI/uvicorn import.
    import uvicorn

    uvicorn.run(
        "sec_financials.web:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    _load_dotenv_if_present(Path.cwd())

    if argv is None:
        argv = sys.argv[1:]

    # Backward-compat: `sec-financials AAPL` -> `sec-financials extract AAPL`.
    # If the first positional isn't a known subcommand and isn't a flag,
    # inject `extract` as the implicit subcommand.
    if argv and not argv[0].startswith("-") and argv[0] not in _SUBCOMMANDS:
        argv = ["extract", *argv]

    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.cmd == "extract":
        return _cmd_extract(args)
    if args.cmd == "serve":
        return _cmd_serve(args)
    parser.error(f"unknown command: {args.cmd!r}")
    return 2  # unreachable but keeps mypy quiet


if __name__ == "__main__":
    raise SystemExit(main())
