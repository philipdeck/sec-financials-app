# SEC Financials Extractor

A small web app (and CLI) that fetches quarterly financial statement data
directly from SEC EDGAR for a given US stock ticker and emits a tidy CSV.

Source of truth for behavior: [`REQUIREMENTS.md`](REQUIREMENTS.md).
Field list (editable): [`config/items.yaml`](config/items.yaml).

## Quick start (CLI, M1)

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

# 4. Run
sec-financials AAPL
```

This writes `AAPL_financials_YYYYMMDD.zip` to the current directory.
The zip contains the main wide-format CSV plus a long-format sources
sidecar.

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
