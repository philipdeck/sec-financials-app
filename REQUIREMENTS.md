# SEC Financials Extractor — Requirements

**Status:** v1.0 — Shipped
**Owner:** pdeck
**Last updated:** 2026-05-19
**Repository:** _TBD (to be created on GitHub)_

This document is a living spec. Sections marked **[OPEN]** are unresolved and
should be answered before or during the relevant implementation phase. The
changelog at the bottom tracks revisions.

---

## 1. Purpose

Build a small web application that, given a US stock ticker, retrieves
financial statement data directly from the SEC and produces a CSV file in a
user-defined format. The app must run on a public website with no manual
intervention from Claude (or any other assistant) at runtime.

## 2. Goals

- Single input: a US stock ticker symbol (e.g. `AAPL`).
- Output: a downloadable CSV containing the income statement, balance sheet,
  and cash flow statement data for that company.
- Hosted on a public URL so the user can reach it from any browser.
- Source code stored on GitHub with a clean commit history.
- Fully autonomous operation in production — no Claude / no LLM call path at
  runtime.

## 3. Non-Goals (initial release)

- No login, accounts, or user data persistence.
- No paid data sources, no aggregators (Yahoo, IEX, Polygon, etc.).
- No analytics, charting, or valuation modeling — data extraction only.
- No support for non-US issuers that do not file with the SEC.
- No real-time / intraday data — SEC filings are quarterly/annual.

## 4. Users & Use Cases

- **Primary user:** project owner, researching companies and needing tidy CSVs
  for downstream analysis (Excel, BI tools, scripts).
- **Primary flow:** open site → enter ticker → click "Generate CSV" → download
  file → open in spreadsheet.

## 5. Functional Requirements

### 5.1 Input
- Free-text ticker field (case-insensitive, trimmed).
- Validation: ticker must resolve to a CIK via the SEC ticker→CIK mapping.
- Friendly error if ticker not found.
- **Period coverage:** the **5 most recent completed fiscal years** (each
  with Q1–Q4) plus **the in-progress fiscal year's actually-filed
  quarters** (Q1, Q2, and/or Q3). The in-progress year contributes
  partial rows because there is no 10-K yet from which to derive Q4. As
  the issuer files each new 10-Q, those quarters appear automatically;
  when the 10-K eventually lands, the year is rolled into the completed
  set on the next extraction (with Q4 derived).

### 5.2 Data scope

**Primary reporting frequency: quarterly.** Output rows represent quarterly
periods. Annual (10-K) filings are pulled in parallel and used for
cross-validation (§5.5), not as the primary output.

**Maintained items list.** The set of line items to extract is a maintained
list — _not_ "everything in the three statements." Items can be added or
removed over time without code changes (or with a single-file change). Each
item produces one column in the output CSV, in list order.

**Schema.** Every item has these fields:

| Field | Required | Purpose |
|-------|----------|---------|
| `key` | yes | Stable short name used as the CSV column header (snake_case, e.g. `revenue`) |
| `display_name` | yes | Human-readable label, used in error / log messages |
| `statement` | yes | `income` / `balance_sheet` / `cash_flow` |
| `flow_or_stock` | yes | `flow` (sums across periods, subject to §5.6 cross-validation) or `stock` (point-in-time balance, exact-match validation) |
| `unit` | yes | Expected unit, e.g. `USD`, `shares` |
| `xbrl_tags` | one of these two | Ordered fallback list of US-GAAP concept tags — first tag with a reported value wins |
| `derivation` | one of these two | Used when no single tag matches the concept. Has `add:` and (optional) `subtract:` lists of tags. The item's value is `sum(add) − sum(subtract)`. Missing components are treated as zero. |

Exactly one of `xbrl_tags` or `derivation` must be present. Validated against
a JSON Schema at startup; a malformed file fails startup loudly.

**Locked field list (v1).** 27 items, ordered by statement. The authoritative
copy lives in [`config/items.yaml`](config/items.yaml). Summary:

- **Income statement (11):** `revenue`, `other_revenue` (non-operating, incl.
  interest income), `cost_of_sales`, `other_cost_of_sales`,
  `operating_expenses`, `other_costs` (non-operating), `interest_expense`
  (gross), `income_tax_expense`, `minority_interest` (NCI portion of net
  income), `net_income`, `shares_diluted`.
- **Balance sheet (8):** `cash`, `investments` (short-term only),
  `current_assets`, `fixed_assets` (PP&E net), `total_assets`, `debt`
  (LT incl. current portion — **derived**: `LongTermDebtNoncurrent +
  LongTermDebtCurrent`), `total_liabilities`, `retained_earnings`.
- **Cash flow (8):** `depreciation` (split from amortization where possible),
  `amortization`, `stock_compensation`, `capex`, `cash_from_operations`,
  `dividends_paid`, `debt_change` (**derived**: LT + ST issuances minus
  repayments), `equity_change` (**derived**: common stock issuance minus
  repurchases; dividends excluded).

**Storage: a single YAML file in the repository** at `config/items.yaml`,
edited as plain text in any editor (Notepad, VS Code, etc.) and version-
controlled like the rest of the code. No database, no admin UI in v1.

- YAML is chosen over JSON specifically because it allows `# comments` —
  useful for annotating why a particular XBRL tag is in the fallback list
  ("`SalesRevenueNet` retained for filings before 2018").
- Loaded once at application startup.
- **Validated against a JSON Schema on load.** If `items.yaml` is malformed
  or violates the schema, the app fails to start with a clear error
  message — no silent fallback that would cause missing columns.
- Changes ship via normal git/PR flow, not at runtime. This keeps the
  configuration auditable and prevents drift between deployed instances.

Example shape:

```yaml
items:
  - key: revenue
    display_name: Revenue
    statement: income
    flow_or_stock: flow
    unit: USD
    xbrl_tags:
      - Revenues
      - RevenueFromContractWithCustomerExcludingAssessedTax
      - SalesRevenueNet    # older filings

  - key: cost_of_goods_sold
    display_name: Cost of Goods Sold
    statement: income
    flow_or_stock: flow
    unit: USD
    xbrl_tags:
      - CostOfGoodsAndServicesSold
      - CostOfRevenue
      - CostOfGoodsSold

  - key: cash
    display_name: Cash & Equivalents
    statement: balance_sheet
    flow_or_stock: stock
    unit: USD
    xbrl_tags:
      - CashAndCashEquivalentsAtCarryingValue
      - Cash
```

Every CSV always includes the full maintained list — no per-request subset
selection in v1. If items are added or removed in the future, the change
ships through git and applies to all subsequent requests.

### 5.5 Per-quarter sourcing rules

Different quarters come from different filings. The app must apply the
correct source per quarter, then derive single-quarter figures where filings
report only year-to-date.

**Source by quarter:**

| Quarter | Primary source | Notes |
|---------|---------------|-------|
| Q1 | 10-Q | Filed ~45 days after fiscal Q1 end |
| Q2 | 10-Q | Filed ~45 days after fiscal Q2 end |
| Q3 | 10-Q | Filed ~45 days after fiscal Q3 end |
| Q4 | "Other documents" — typically the 8-K earnings press release attached as Exhibit 99 — then verified against the 10-K | There is no 10-Q for Q4. The 10-K is the authoritative annual document but does not always present Q4 as a standalone column. |

**Q4 strategy: parse the 8-K earnings press release for a directly reported
Q4 value, then verify it against the 10-K.**

- Primary Q4 source is the 8-K filed at quarter-end, typically with the
  full earnings release attached as Exhibit 99.1 or 99.2.
- 8-K exhibits are usually HTML (sometimes PDF) and may or may not be
  XBRL-tagged. The extractor must handle both:
  - If XBRL tags for the Q4 standalone values are present, use them
    directly (fastest, most reliable).
  - Otherwise, parse the HTML exhibit to locate the Q4 column of each
    statement table. This is the messier path and is the main complexity
    cost of choosing this strategy.
- Verification: compute the implied annual total as
  `Q1 + Q2 + Q3 + Q4(from 8-K)` and compare to the 10-K annual value. If
  the difference exceeds the §5.6 tolerance, flag in the row's `notes`
  column rather than failing the request.
- If the 8-K cannot be parsed for a given quarter (missing exhibit, format
  the parser cannot handle), **fall back to the derivation**
  `Q4 = 10-K annual − 9-month YTD` and record the fallback in `notes`
  (e.g. `"Q4 revenue derived from 10-K minus 9mo YTD; 8-K parse failed"`).

**Single-quarter derivation for YTD-reported items.** Many filings (always
cash flow, sometimes income statement items) report _cumulative_ year-to-date
values rather than standalone quarter values. The standard derivation is:

| Quarter | Single-quarter value |
|---------|---------------------|
| Q1 | `value_3mo` (direct from Q1 10-Q) |
| Q2 | `value_6mo (Q2 10-Q)` − `value_3mo (Q1 10-Q)` |
| Q3 | `value_9mo (Q3 10-Q)` − `value_6mo (Q2 10-Q)` |
| Q4 | `value_annual (10-K)` − `value_9mo (Q3 10-Q)` |

**Statement-specific behavior:**
- **Cash flow statement:** _always_ YTD in 10-Q filings — apply subtraction
  for every quarter, every line item.
- **Income statement:** most filers report a 3-month column directly in the
  10-Q (no subtraction needed), but some only report YTD. The extractor
  must detect which form the filing uses and apply subtraction only when
  needed. Q4 always requires subtraction since it's derived from the 10-K.
- **Balance sheet:** point-in-time, no derivation — read the balance as of
  the period-end date.

### 5.6 Cross-validation against annual filings

For every ticker request, after collecting all quarterly figures, validate
them against the 10-K annual values:

**Flow items** (revenue, COGS, cash flow line items, expenses):
- `|Σ quarters (Q1+Q2+Q3+Q4) − annual 10-K| / |annual 10-K| ≤ 0.5%`.
- Because Q4 is sourced from the 8-K rather than derived from the 10-K
  (§5.5), this is a true independent cross-check.

**Stock items** (cash, total assets, equity):
- Fiscal-year-end quarterly balance must match the 10-K balance-sheet value
  on the same date exactly (no tolerance — same point-in-time observation
  from the same issuer).

**Tolerance: 0.5% relative difference.** No absolute floor — a 0.5% gap is
treated as a real discrepancy regardless of line-item size. If small
line items prove noisy in practice, revisit and add a floor in a later
revision.

**On mismatch:** the row is emitted normally. A human-readable description
of the discrepancy is appended to the `notes` column on the affected
quarter's row(s), e.g.:

```
revenue: ΣQ1-Q4 (391,200,000,000) vs 10-K annual (391,035,000,000), diff 0.04% — within tolerance
revenue: ΣQ1-Q4 (393,500,000,000) vs 10-K annual (391,035,000,000), diff 0.63% — flagged
```

Only flagged discrepancies (those exceeding tolerance) are written to
`notes`; within-tolerance differences are not logged in the output.

**On restated filings:** when a 10-K restates prior quarters, prefer the
most recently filed value. The sidecar sources file (§5.3) records which
`accession_number` each value came from so restatements are traceable.

### 5.3 Output (CSV)

- Filename: `{TICKER}_financials_{YYYYMMDD}.csv` (UTC date).
- Encoding: UTF-8 with BOM (Excel-friendly).
- Values are emitted in **millions** of the reported unit (i.e. raw value
  ÷ 1,000,000). No formatting, no thousands separators, no currency
  symbols. Negative values use a leading minus. Integer millions render
  without a fractional part (e.g. `111439` rather than `111439.000000`).
- **Delivery:** standard browser download via `Content-Disposition: attachment`.
  The file lands in the user's browser-configured download folder
  (typically `~/Downloads`); the user moves it from there as needed. There is
  **no server-side persistence** of generated CSVs — each request generates
  the file in-memory and streams it to the client. (Browser security
  prevents a remote app from writing to a chosen folder on the user's
  machine in most browsers; if direct-to-folder workflow becomes important
  later, a local CLI entry point would be added as a v2 feature.)

**Layout: quarters on the vertical axis, metrics on the horizontal axis.**
Each row is one fiscal quarter; each metric from the maintained items list
(§5.2) gets its own column. 5 years × 4 quarters = up to 20 rows per ticker.
Rows are sorted oldest → newest.

**Column order:**

1. Identifier / period columns (fixed):
   - `ticker`
   - `fiscal_year` (e.g. `2024`)
   - `fiscal_quarter` (`Q1` / `Q2` / `Q3` / `Q4`)
   - `period_end` (ISO date, e.g. `2024-03-30`)
2. Metric columns (one per item in the maintained list, in the order defined
   by that list — see §5.2): e.g. `revenue`, `cost_of_goods_sold`, `cash`, …
3. Metadata column (fixed, far right edge):
   - `notes` — free text. Empty when the row has no issues. Populated when
     the cross-validation in §5.6 detects a discrepancy or a value is
     missing. Multiple issues on the same row are semicolon-separated.

**Worked example:**

```
ticker,fiscal_year,fiscal_quarter,period_end,revenue,cost_of_goods_sold,cash,notes
AAPL,2020,Q1,2019-12-28,91819000000,56602000000,39771000000,
AAPL,2020,Q2,2020-03-28,58313000000,35943000000,40174000000,
AAPL,2020,Q3,2020-06-27,59685000000,37029000000,33383000000,
AAPL,2020,Q4,2020-09-26,64698000000,40009000000,38016000000,"revenue: 8-K vs derived 10-K differ by 0.7%"
AAPL,2021,Q1,2020-12-26,111439000000,67111000000,36010000000,
...
```

Notes on the layout:

- **Source traceability** (which 10-Q / 10-K / 8-K accession each value came
  from) does **not** appear in the main CSV — it would clutter the
  spreadsheet. Instead, **always generate** a sidecar file
  `{TICKER}_sources_{YYYYMMDD}.csv` in long format
  (`fiscal_year, fiscal_quarter, metric, value, accession_number, form_type, filed_date`)
  alongside the main CSV. Both files are delivered to the user — the most
  practical packaging is a single zip
  (`{TICKER}_financials_{YYYYMMDD}.zip`) containing both, so one click =
  one download.
- **Missing values** (metric not reported in that filing) appear as empty
  cells, not `0`, not `N/A`. The `notes` column records which metrics were
  missing.

### 5.4 Error handling
- Unknown ticker → 400 with clear message.
- SEC rate limit / 5xx → retry with exponential backoff (max 3 attempts);
  surface a friendly error if still failing.
- Missing concept tag for a given filing → leave value blank, do not fail the
  whole row.

## 6. External Dependencies

**SEC EDGAR is the only permitted external API.** Specifically:

- `https://www.sec.gov/files/company_tickers.json` — ticker → CIK lookup
- `https://data.sec.gov/submissions/CIK{cik}.json` — filing index
- `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json` — all XBRL
  tagged financial facts (primary data source)
- `https://data.sec.gov/api/xbrl/companyconcept/CIK{cik}/us-gaap/{tag}.json`
  — per-concept time series (fallback)

Compliance requirements per SEC fair-access policy:
- Send a descriptive `User-Agent` header with a real contact email.
- Limit to **≤ 10 requests/second** per their published guidance.
- Cache responses where reasonable to minimize traffic.

## 7. Architecture

**Stack: Python + FastAPI.**

- **Language:** Python 3.12+.
- **Web framework:** FastAPI — small, modern, auto-generates an OpenAPI
  spec and an interactive `/docs` page useful for debugging.
- **Data shaping:** `pandas` for CSV assembly and the YTD-subtraction math.
- **HTTP client:** `httpx` (FastAPI's async-friendly default) for SEC
  EDGAR calls. Sends the required `User-Agent` header from env var on
  every request.
- **HTML parsing (for 8-K exhibits):** `beautifulsoup4` + `lxml`. The 8-K
  earnings releases vary in structure across issuers, so the parser will
  be table-detection heuristic-based.
- **Frontend:** a single server-rendered HTML page with a tiny amount of
  vanilla JS (no React/Vue/Svelte). The UI is one input + one button +
  status messages — no need for a frontend framework.
- **Packaging:** `uv` or `pip-tools` for dependency pinning; `pyproject.toml`.
- **Linting / formatting:** `ruff` (combined linter + formatter).
- **Tests:** `pytest`.

**Considered and rejected:**

- **Next.js (TypeScript):** rejected because CSV/numeric data wrangling
  and especially the 8-K HTML parsing have far more prior art and better
  libraries in Python than in JavaScript.
- **Flask:** rejected in favor of FastAPI, which is a strict superset of
  Flask's ergonomics with better built-in validation and docs. No
  existing Flask codebase to inherit.

## 8. Hosting & Infrastructure

- **Provider: Render** (free tier).
  - Git-push deploys: pushing to `main` on GitHub triggers an automatic
    deploy. Matches §9's workflow.
  - Automatic HTTPS via Let's Encrypt, including on the custom domain.
  - Free tier supports custom domains at no charge.
  - **Accepted tradeoff: cold starts.** After ~15 minutes of inactivity the
    free-tier service spins down; the next request takes ~30 seconds while
    it spins back up. Acceptable for a personal-use tool.
- **Custom domain: `projects.extuple.com`.** Chosen as a personal-tools
  umbrella so future projects can live under the same namespace.
  - The apex `extuple.com` is used for email and stays untouched. DNS
    setup is one added `CNAME` record on `projects` pointing at the Render
    service URL. MX records on the apex are unaffected.
  - For v1 this app sits at the root of the subdomain
    (`https://projects.extuple.com/`). If a second project is added later,
    options are path-prefix routing (`/sec/`, `/foo/`) or sub-subdomains
    (`sec.projects.extuple.com`). Decision deferred until needed.
- No database in v1 (stateless). Optional: in-memory LRU cache for SEC
  responses within a single process lifetime.
- **Region:** Render's default (US-East / Oregon — auto-selected). Not
  worth optimizing for a low-traffic personal app.

## 9. Source Control & Workflow

- GitHub repository, **public**.
- Branching: `main` is always deployable; feature work on short-lived branches.
- Conventional-style commit messages.
- CI: GitHub Actions running lint + tests on PRs. Auto-deploy on merge to
  `main` (provider-dependent).
- `.gitignore` excludes secrets, virtualenvs, build artifacts.

## 10. Autonomy Requirement

The deployed app must operate with **no Claude / LLM involvement at runtime**.
- No API calls to Anthropic or any LLM provider from the server or client.
- All logic is deterministic Python/TypeScript code.
- Claude is used only as a development-time assistant (writing code, docs,
  tests). Nothing Claude-related ships in the production bundle.

## 11. Security & Compliance

- No secrets needed for SEC (public API), but provider hosting keys must be
  stored as platform secrets, never committed.
- Set the SEC-required `User-Agent` from an env var so it can be changed
  without redeploy of source.
- Basic rate limiting on the public endpoint to discourage abuse (e.g. 30
  requests/minute per IP).
- No PII collected.

## 12. Testing

- Unit tests for: ticker→CIK resolution, XBRL concept extraction, CSV
  serialization.
- Integration test that hits SEC for one known ticker (e.g. `AAPL`) and
  verifies a known revenue value within tolerance.
- Snapshot test for the CSV format so layout changes are explicit.

## 13. Milestones

1. **M0 — Spec lock:** _Complete (v0.12)._ All initial open questions resolved.
2. **M1 — Backend MVP:** _Complete._ ticker→CIK, companyfacts fetch, income
   statement extraction (5y × 4q), CSV+sources zip output via CLI.
   Verified against AAPL FY2021–FY2025: quarterly sums match annual 10-K
   totals to the dollar.
3. **M2 — Full statements:** _Complete._ Balance sheet (stock items via
   point-in-time XBRL contexts) and cash flow (YTD-subtraction for Q2/Q3)
   extraction wired in. Derived items (`debt`, `debt_change`,
   `equity_change`) work across both flow and stock. Verified against AAPL
   FY2024: total assets, debt, retained earnings, CFO, capex, dividends,
   and the ~$95B buyback all match Apple's actual filings.
4. **M3 — Web UI:** _Complete (local)._ FastAPI app at
   `src/sec_financials/web.py` serves a single server-rendered HTML page
   with a ticker form. POST returns the zip directly as a
   `Content-Disposition: attachment` download. Run locally with
   `sec-financials serve`. Deploy to Render is M5.
5. **M4 — Hardening:** error handling polish, rate limiting per §11,
   request-level caching of SEC responses, GitHub Actions CI.
   _Deferred — not blocking daily use._
6. **M5 — v1 release:** _Complete._ Repo on GitHub at
   `github.com/philipdeck/sec-financials-app`. Service on Render free
   tier, auto-deploying on push to `main`. Custom domain
   **`https://projects.extuple.com/`** live with Let's Encrypt SSL.
   GoDaddy CNAME `projects` → `sec-financials.onrender.com`. End-to-end
   verified: AAPL submission returns a 9.9KB zip with all 27 columns,
   split-adjusted share counts, and source traceability sidecar.

## 14. Open Questions (consolidated)

_All initial spec questions resolved as of v0.12._ Future open items will
be added here as implementation surfaces them.

## 15. Change Log

| Date       | Version | Change |
|------------|---------|--------|
| 2026-05-19 | 0.1     | Initial draft. |
| 2026-05-19 | 0.2     | Reframed §5.2 around a **maintained items list** (seeded with revenue, COGS, cash). Set **quarterly as primary** reporting frequency. Added §5.5 cross-validation against 10-K annuals, including the 10-Q cumulative-reporting subtlety and Q4 derivation. Resolved the annual-vs-quarterly open question; added new opens on item-list storage, subset selection, validation tolerance, and mismatch behavior. |
| 2026-05-19 | 0.3     | Locked **5-year** period coverage. Split former §5.5 into §5.5 (per-quarter sourcing rules) and §5.6 (cross-validation). Spelled out source-by-quarter (Q1–Q3 from 10-Q, Q4 from 10-K-derivation _or_ 8-K with 10-K verification — new open question). Added YTD-subtraction derivation table with statement-specific behavior (cash flow always YTD; income statement sometimes; balance sheet never). |
| 2026-05-19 | 0.4     | Resolved CSV layout: **quarters on rows, metrics on columns**, one row per fiscal quarter, identifier + period columns on the left, metadata (`validation_status`, `validation_note`) on the right. Added worked example. Moved source traceability to an optional sidecar long-format CSV. Defined missing-value and validation-flag conventions. |
| 2026-05-19 | 0.5     | Locked maintained items list storage: **YAML file at `config/items.yaml`**, plain-text editable, schema-validated at startup, edited via git/PR. Chose YAML over JSON for inline `#` comments on XBRL tag fallbacks. Added example YAML shape. Removed the corresponding open question. |
| 2026-05-19 | 0.6     | Resolved four opens: (1) **Q4 source = 8-K** earnings-release parsing with 10-K verification, with deterministic `10-K − 9mo` fallback when the 8-K cannot be parsed; (2) **always emit the full maintained item list** per request (no subset selection in v1); (3) **validation tolerance = 0.5%** flat (dropped the $1M absolute floor previously proposed — revisit if noisy); (4) **single `notes` column** at the far right replaces the prior `validation_status` + `validation_note` pair. Discrepancies are described in plain English; missing values and 8-K-parse fallbacks also recorded there. |
| 2026-05-19 | 0.7     | Stack locked: **Python 3.12 + FastAPI**, with pandas for CSV shaping, httpx for SEC calls, BeautifulSoup + lxml for 8-K HTML parsing, ruff for lint/format, pytest for tests. Frontend is a single server-rendered HTML page — no JS framework. Rewrote §7 from "options to choose" to a locked architecture with the rejected alternatives recorded. |
| 2026-05-19 | 0.8     | Locked the **27-item field list** in `config/items.yaml`. Extended the items schema with a `derivation` block (sum/subtract of XBRL tags) for items with no single matching tag — used for `debt`, `debt_change`, `equity_change`. Rewrote §5.2 with the full schema spec and an item-by-item summary of what's included. Resolved interpretation choices: other_revenue/other_costs = non-operating; interest_expense = gross (interest income lives in other_revenue); minority_interest = IS line for NCI portion of net income; shares = weighted-avg diluted; investments = short-term only; debt = LT incl. current portion; cash_from_operations = bottom-line of operating section. Removed the corresponding open question on the XBRL concept list. |
| 2026-05-19 | 0.9     | Specified CSV delivery: **standard browser download** (`Content-Disposition: attachment`), file lands in the user's download folder, no server-side persistence. Noted that direct-to-folder workflow would require a local CLI entry point (deferred to v2). |
| 2026-05-19 | 0.10    | Source-traceability sidecar CSV is **always emitted**, packaged with the main CSV in a single zip download. **GitHub repo = public** locked in §9. Open-questions list pruned; hosting provider explicitly added as the remaining decision (custom domain deferred until hosting is picked). |
| 2026-05-19 | 0.11    | Hosting locked: **Render free tier** with git-push deploys and automatic HTTPS. Briefly considered PythonAnywhere for its no-cold-starts behavior but rejected because (a) it would have forced switching the framework from FastAPI to Flask (PythonAnywhere is WSGI-first), and (b) custom domains there require a paid plan ($60/yr) whereas Render supports them on free tier. **Custom domain: subdomain of `extuple.com`** (already owned, used for email — apex stays untouched; only an added CNAME). Accepted tradeoff: ~30s cold start after ~15 min of idle on Render free tier. Specific subdomain name still TBD. |
| 2026-05-19 | 0.12    | Subdomain locked: **`projects.extuple.com`** as a personal-tools umbrella. Documented the multi-project-future implication (path-prefix vs sub-subdomain) for if another project ships under the same umbrella later — decision deferred until needed. **All initial spec open questions resolved.** |
| 2026-05-20 | 0.13    | **M2 complete.** Extractor now handles balance sheet (stock items via point-in-time XBRL contexts: no duration filter, Q4 balance pulled from 10-K with fp=FY) and cash flow (YTD-subtraction: Q2 = 6mo − 3mo, Q3 = 9mo − 6mo). Verified against AAPL FY2024: total assets / debt / retained earnings / CFO / capex / dividends all match actual filings. Updated §13 milestone status. Known minor data gap (not blocking): a few Apple-specific tags missing from items.yaml (D&A combined, post-2023 interest expense) — items.yaml-only fix when needed. |
| 2026-05-20 | 0.14    | **In-progress fiscal year support.** §5.1 period coverage now reads as "5 completed years + in-progress year's filed quarters." `discover_quarters_to_extract` finds the most recent fy with 10-Qs but no 10-K and adds its filed Q1/Q2/Q3 rows. Q4 of the in-progress year is correctly omitted (no 10-K to derive from). Verified against AAPL: output is now 22 rows (FY2021–FY2025 + FY2026 Q1 & Q2 = $143.8B / $111.2B, matching Apple's actual 10-Q filings). |
| 2026-05-21 | 0.15    | **M3 complete (local).** New `src/sec_financials/web.py` exposes a FastAPI app with two routes: `GET /` renders a single server-rendered HTML form (no JS framework, vanilla CSS, ~150 lines total), `POST /` runs the same extraction pipeline the CLI uses and returns the zip as a download. New `sec-financials serve` subcommand runs uvicorn locally. CLI restructured to use subcommands (`extract` / `serve`) with backward-compat: `sec-financials AAPL` still works (auto-injects `extract`). Shared pipeline extracted to `pipeline.py` so CLI and web stay in sync. 7 new tests via FastAPI TestClient (form rendering, success zip download, error re-rendering, ticker normalization, healthz). Suite is now 49 passing. Deploy to Render still pending (M5). |
| 2026-05-21 | 0.16    | **M5 prep.** Added `render.yaml` blueprint (free plan, oregon region, Python 3.12.7, `/healthz` health check, `SEC_USER_AGENT` declared as a dashboard-set secret) and `runtime.txt`. Build command is `pip install .` (no dev extras in production); start command is `uvicorn sec_financials.web:app --host 0.0.0.0 --port $PORT --workers 1`. README updated with deploy steps. Verified the prod-equivalent start command locally: NVDA form submission returns an 8.8KB zip with all 27 columns. Remaining M5 steps are user-driven: push to GitHub, create Render service, configure DNS CNAME for `projects.extuple.com`. |
| 2026-05-21 | 0.17    | **Values now emitted in millions.** Per user request, all numeric values in the main CSV and sources sidecar are divided by 1,000,000 before output. `111,439,000,000` → `111439`; `5,157,787,000` → `5157.787`. Trailing zeros and the trailing decimal point are stripped. Applies uniformly to USD and shares-unit items. Web form subtitle updated to note this. |
| 2026-05-22 | 0.18    | **NVDA gaps filled (D&A, capex, interest).** items.yaml: added `DepreciationDepletionAndAmortization` / `DepreciationAndAmortization` to depreciation fallback; `PaymentsToAcquireProductiveAssets` / `PaymentsForCapitalImprovements` to capex; `InterestExpenseNonoperating` to interest_expense. Extractor: same-tag preference for Q4 derivation (prevents mixing pure-D annual with combined-D&A quarterly when issuers tag inconsistently across years). |
| 2026-05-22 | 0.19    | **Stock-split back-adjustment.** New module `src/sec_financials/splits.py` detects clean split ratios (2,3,4,5,6,7,8,10 and reciprocals for reverse) in any item with `unit==shares` and back-adjusts all earlier periods by the cumulative multiplier. Verified against NVDA's two splits (4:1 in 2021 and 10:1 in 2024): pre-fix `shares_diluted` jumped from 632M → 2,500M → 24,500M across the history; post-fix it reads flat at ~25,000M. Adjusted rows get a `split-adjusted` marker in the notes column; source sidecar audit trail is unchanged. |
| 2026-05-22 | v1.0    | **Shipped.** Public service live at https://projects.extuple.com/. Render free tier, Let's Encrypt SSL, GoDaddy CNAME on `projects` subdomain pointing at `sec-financials.onrender.com`. Apex `extuple.com` and email MX records untouched. Auto-deploy from `main` on GitHub. M4 hardening (rate limit, request caching, CI) deferred — not blocking daily use; can land any time. |
