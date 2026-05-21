# SEC Financials Extractor

A small web app (and CLI) that fetches quarterly financial statement data
directly from SEC EDGAR for a given US stock ticker and emits a tidy CSV.

Source of truth for behavior: [`REQUIREMENTS.md`](REQUIREMENTS.md).
Field list (editable): [`config/items.yaml`](config/items.yaml).

## Quick start

```powershell
# 1. Clone the repo and enter it
git clone https://github.com/<owner>/sec-financials-app.git
cd sec-financials-app

# 2. Create a venv and install
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

# 3. Configure your SEC User-Agent (required by SEC fair-access policy)
copy .env.example .env
# Edit .env and replace the example email with your own
```

### Run as a web app

```powershell
sec-financials serve
# Open http://127.0.0.1:8000/ in your browser, type a ticker, click Generate
```

### Run as a one-shot CLI

```powershell
sec-financials AAPL                  # equivalent to: sec-financials extract AAPL
sec-financials extract MSFT --out .\out
```

Either entry point produces `{TICKER}_financials_YYYYMMDD.zip`, containing
the wide-format main CSV plus the long-format sources sidecar.

## Project layout

```
sec-financials-app/
├── REQUIREMENTS.md        Living spec
├── config/
│   └── items.yaml         The maintained list of fields to extract
├── src/sec_financials/    Python package
└── tests/                 pytest suite
```

## Development

```powershell
ruff check .
ruff format .
pytest
```

## Constraints

- **SEC EDGAR is the only external API.** No paid data aggregators.
- **No LLM calls at runtime.** Claude / other assistants are used only as
  development-time tools.
- **GitHub-hosted, deployed on Render** at `projects.extuple.com`.
