"""FastAPI web app: form -> ticker -> zip download.

Single server-rendered HTML page (no JS framework) per REQUIREMENTS.md §7.
The extraction pipeline is the same as the CLI's — both delegate to
`sec_financials.pipeline.generate_report`.

Deployment target: Render (M5). For local development:
    sec-financials serve --port 8000
or:
    uvicorn sec_financials.web:app --reload
"""

from __future__ import annotations

import html
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, Response

from sec_financials.cli import _default_items_path, _load_dotenv_if_present
from sec_financials.config import ItemsConfig, ItemsConfigError, load_items
from sec_financials.pipeline import GeneratedReport, generate_report
from sec_financials.sec_client import SECClientError

_PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <title>SEC Financials Extractor</title>
    <style>
        :root {{
            --fg: #1a1a1a;
            --muted: #666;
            --bg: #f7f7f5;
            --card: #ffffff;
            --border: #e0e0dc;
            --accent: #0d4f8b;
            --accent-hover: #0a3d6e;
            --error-bg: #fdecea;
            --error-fg: #8b1a13;
            --success-fg: #1f6b2e;
        }}
        * {{ box-sizing: border-box; }}
        body {{
            margin: 0;
            font: 16px/1.5 -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            color: var(--fg);
            background: var(--bg);
            min-height: 100vh;
            display: flex;
            align-items: flex-start;
            justify-content: center;
            padding: 4rem 1rem;
        }}
        main {{
            background: var(--card);
            border: 1px solid var(--border);
            border-radius: 8px;
            padding: 2rem;
            max-width: 540px;
            width: 100%;
            box-shadow: 0 1px 3px rgba(0,0,0,0.04);
        }}
        h1 {{
            margin: 0 0 0.25rem 0;
            font-size: 1.5rem;
            font-weight: 600;
        }}
        .subtitle {{
            color: var(--muted);
            margin: 0 0 1.5rem 0;
            font-size: 0.95rem;
        }}
        form {{
            display: flex;
            gap: 0.5rem;
            margin-bottom: 1rem;
        }}
        input[type=text] {{
            flex: 1;
            padding: 0.65rem 0.85rem;
            font: inherit;
            font-size: 1.1rem;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            border: 1px solid var(--border);
            border-radius: 6px;
            background: var(--bg);
            transition: border-color .15s, background .15s;
        }}
        input[type=text]:focus {{
            outline: none;
            border-color: var(--accent);
            background: var(--card);
        }}
        button {{
            padding: 0.65rem 1.25rem;
            font: inherit;
            font-weight: 500;
            border: 0;
            border-radius: 6px;
            background: var(--accent);
            color: white;
            cursor: pointer;
            transition: background .15s;
        }}
        button:hover {{ background: var(--accent-hover); }}
        button:disabled {{ background: var(--muted); cursor: wait; }}
        .alert {{
            padding: 0.75rem 1rem;
            border-radius: 6px;
            background: var(--error-bg);
            color: var(--error-fg);
            font-size: 0.93rem;
            margin-bottom: 1rem;
        }}
        details {{
            margin-top: 1.5rem;
            color: var(--muted);
            font-size: 0.9rem;
        }}
        details summary {{ cursor: pointer; }}
        details p {{ margin: 0.5rem 0 0 0; }}
        code {{
            background: var(--bg);
            padding: 0.1rem 0.35rem;
            border-radius: 3px;
            font-size: 0.85em;
        }}
    </style>
</head>
<body>
    <main>
        <h1>SEC Financials Extractor</h1>
        <p class="subtitle">Enter a US ticker. Get a CSV (plus sources sidecar) zipped for download. <em>Values are in millions of the reported unit.</em></p>

        {error_block}

        <form method="post" action="/" onsubmit="this.querySelector('button').disabled=true;this.querySelector('button').textContent='Generating…';">
            <input type="text" name="ticker" placeholder="AAPL" value="{ticker_value}" autofocus required maxlength="10">
            <button type="submit">Generate CSV</button>
        </form>

        <details>
            <summary>About</summary>
            <p>Pulls 5 fiscal years of quarterly data plus the in-progress
            year's filed quarters, directly from SEC EDGAR. Source code is
            on <a href="https://github.com/">GitHub</a>; the field list is
            maintained in <code>config/items.yaml</code>.</p>
        </details>
    </main>
</body>
</html>"""


def _render_page(*, ticker_value: str = "", error: str | None = None) -> str:
    error_block = ""
    if error:
        error_block = f'<div class="alert">{html.escape(error)}</div>'
    return _PAGE_TEMPLATE.format(
        ticker_value=html.escape(ticker_value),
        error_block=error_block,
    )


def create_app(*, items_config: ItemsConfig | None = None) -> FastAPI:
    """Build the FastAPI application.

    Passing `items_config` is mainly for tests; production loads it at
    startup from the standard location.
    """
    app = FastAPI(title="SEC Financials Extractor", docs_url=None, redoc_url=None)

    # If no config was injected, load it lazily on first request so that
    # importing the module doesn't require the file to be present.
    state: dict[str, ItemsConfig | None] = {"items": items_config}

    def get_items() -> ItemsConfig:
        if state["items"] is None:
            path = _default_items_path()
            state["items"] = load_items(path)
        return state["items"]  # type: ignore[return-value]

    @app.get("/", response_class=HTMLResponse)
    def index() -> HTMLResponse:
        return HTMLResponse(_render_page())

    @app.post("/")
    def generate(ticker: str = Form(...)) -> Response:
        ticker_clean = ticker.strip().upper()
        try:
            items_config = get_items()
        except (ItemsConfigError, FileNotFoundError) as e:
            return HTMLResponse(
                _render_page(
                    ticker_value=ticker_clean,
                    error=f"Server misconfiguration: {e}",
                ),
                status_code=500,
            )

        try:
            report: GeneratedReport = generate_report(
                ticker_clean,
                items_config,
                statement="all",
            )
        except SECClientError as e:
            return HTMLResponse(
                _render_page(ticker_value=ticker_clean, error=str(e)),
                status_code=200,
            )
        except ValueError as e:
            return HTMLResponse(
                _render_page(ticker_value=ticker_clean, error=str(e)),
                status_code=200,
            )

        if report.row_count == 0:
            return HTMLResponse(
                _render_page(
                    ticker_value=ticker_clean,
                    error=(
                        f"Could not extract data for {ticker_clean}. The "
                        "issuer may not have filed an XBRL 10-K."
                    ),
                ),
                status_code=200,
            )

        return Response(
            content=report.zip_bytes,
            media_type="application/zip",
            headers={
                "Content-Disposition": f'attachment; filename="{report.zip_filename}"',
                "Content-Length": str(len(report.zip_bytes)),
            },
        )

    @app.get("/healthz", response_class=HTMLResponse)
    def healthz() -> HTMLResponse:
        """Liveness probe for Render and similar platforms."""
        return HTMLResponse("ok", status_code=200)

    return app


# Module-level app for `uvicorn sec_financials.web:app`. We load .env from
# the working directory on import so that the configured SEC_USER_AGENT is
# available before any request comes in.
_load_dotenv_if_present(Path.cwd())
app = create_app()
