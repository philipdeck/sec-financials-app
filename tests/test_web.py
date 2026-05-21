"""Tests for the FastAPI web app.

These tests don't hit the real SEC. We monkey-patch the pipeline at the
boundary (`sec_financials.web.generate_report`) so the routes can be
exercised in isolation.
"""

from __future__ import annotations

import io
import zipfile

import pytest
from fastapi.testclient import TestClient

from sec_financials.config import ItemsConfig, load_items
from sec_financials.pipeline import GeneratedReport
from sec_financials.sec_client import SECClientError
from sec_financials.web import create_app

# We use the real items.yaml — the schema validation should pass on it
# and gives the test a realistic ItemsConfig to inject.
ITEMS_YAML = (
    __import__("pathlib")
    .Path(__file__)
    .parent.parent
    / "config"
    / "items.yaml"
)


@pytest.fixture
def items_config() -> ItemsConfig:
    return load_items(ITEMS_YAML)


@pytest.fixture
def client(items_config: ItemsConfig) -> TestClient:
    return TestClient(create_app(items_config=items_config))


def _stub_report(zip_bytes: bytes = b"ZIPBYTES") -> GeneratedReport:
    return GeneratedReport(
        ticker="AAPL",
        entity_name="Apple Inc.",
        cik=320193,
        row_count=22,
        zip_bytes=zip_bytes,
        zip_filename="AAPL_financials_20260520.zip",
    )


# ──────────────────────────────────────────────────────────────────────────
# Form rendering
# ──────────────────────────────────────────────────────────────────────────


def test_index_renders_form(client: TestClient):
    resp = client.get("/")
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert "SEC Financials Extractor" in body
    assert 'name="ticker"' in body
    assert "Generate CSV" in body
    # No error block on the empty form.
    assert 'class="alert"' not in body


def test_healthz_returns_ok(client: TestClient):
    resp = client.get("/healthz")
    assert resp.status_code == 200
    assert resp.text == "ok"


# ──────────────────────────────────────────────────────────────────────────
# POST / — success path
# ──────────────────────────────────────────────────────────────────────────


def test_post_returns_zip_download(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    # Build a real (tiny) zip so headers/length come out correctly.
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("AAPL_financials_20260520.csv", "ticker,fy\nAAPL,2024\n")
    real_zip = buf.getvalue()

    monkeypatch.setattr(
        "sec_financials.web.generate_report",
        lambda *a, **k: _stub_report(zip_bytes=real_zip),
    )

    resp = client.post("/", data={"ticker": "aapl"})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "application/zip"
    assert "attachment" in resp.headers["content-disposition"]
    assert "AAPL_financials_20260520.zip" in resp.headers["content-disposition"]
    assert resp.content == real_zip

    # The returned bytes should be a valid zip with the expected entry.
    z = zipfile.ZipFile(io.BytesIO(resp.content))
    assert "AAPL_financials_20260520.csv" in z.namelist()


def test_post_normalizes_ticker_to_uppercase(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    seen: dict[str, str] = {}

    def fake_generate(ticker: str, _cfg, **_kwargs):
        seen["ticker"] = ticker
        return _stub_report()

    monkeypatch.setattr("sec_financials.web.generate_report", fake_generate)
    client.post("/", data={"ticker": "  msft  "})
    assert seen["ticker"] == "MSFT"


# ──────────────────────────────────────────────────────────────────────────
# POST / — error paths
# ──────────────────────────────────────────────────────────────────────────


def test_post_unknown_ticker_re_renders_form_with_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    def fake_generate(*_a, **_k):
        raise SECClientError("Ticker 'FOOBAR' not found in SEC's mapping.")

    monkeypatch.setattr("sec_financials.web.generate_report", fake_generate)

    resp = client.post("/", data={"ticker": "FOOBAR"})
    # Form re-rendered, not 4xx — better UX than a raw error page.
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/html")
    body = resp.text
    assert 'class="alert"' in body
    assert "FOOBAR" in body  # ticker pre-filled
    assert "not found" in body.lower()


def test_post_empty_row_count_shows_friendly_error(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    empty = GeneratedReport(
        ticker="XYZ",
        entity_name="XYZ Co.",
        cik=1,
        row_count=0,
        zip_bytes=b"",
        zip_filename="XYZ_financials_20260520.zip",
    )
    monkeypatch.setattr(
        "sec_financials.web.generate_report", lambda *a, **k: empty
    )
    resp = client.post("/", data={"ticker": "XYZ"})
    assert resp.status_code == 200
    assert "Could not extract data" in resp.text


def test_post_missing_ticker_returns_form_error(client: TestClient):
    # FastAPI's Form() raises 422 when the required field is missing.
    resp = client.post("/", data={})
    assert resp.status_code == 422
