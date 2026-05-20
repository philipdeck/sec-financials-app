"""Tests for the SEC HTTP client.

These tests use httpx's MockTransport so they don't hit the real SEC.
"""

from __future__ import annotations

import time

import httpx
import pytest

from sec_financials.sec_client import (
    SECClient,
    SECClientError,
    _RateLimiter,
    _resolve_user_agent,
)

# ──────────────────────────────────────────────────────────────────────────
# User-Agent resolution
# ──────────────────────────────────────────────────────────────────────────


def test_resolve_user_agent_reads_env(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SEC_USER_AGENT", "My App me@example.com")
    assert _resolve_user_agent() == "My App me@example.com"


def test_resolve_user_agent_unset_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("SEC_USER_AGENT", raising=False)
    with pytest.raises(SECClientError, match="SEC_USER_AGENT"):
        _resolve_user_agent()


def test_resolve_user_agent_without_email_raises(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("SEC_USER_AGENT", "My App with no email")
    with pytest.raises(SECClientError, match="email"):
        _resolve_user_agent()


# ──────────────────────────────────────────────────────────────────────────
# Rate limiter
# ──────────────────────────────────────────────────────────────────────────


def test_rate_limiter_allows_burst_up_to_limit():
    rl = _RateLimiter(max_per_second=3)
    start = time.monotonic()
    for _ in range(3):
        rl.acquire()
    elapsed = time.monotonic() - start
    # Three acquires within the limit shouldn't block.
    assert elapsed < 0.1


def test_rate_limiter_throttles_beyond_limit():
    rl = _RateLimiter(max_per_second=2)
    start = time.monotonic()
    for _ in range(3):
        rl.acquire()
    elapsed = time.monotonic() - start
    # The third call must wait roughly a full second.
    assert elapsed >= 0.9


# ──────────────────────────────────────────────────────────────────────────
# get_json behaviour via MockTransport
# ──────────────────────────────────────────────────────────────────────────


def _make_client(handler, user_agent: str = "Test App test@example.com") -> SECClient:
    """Build an SECClient whose underlying httpx client uses a MockTransport."""
    client = SECClient.__new__(SECClient)
    client._user_agent = user_agent  # type: ignore[attr-defined]
    client._rate_limiter = _RateLimiter(max_per_second=1000)  # type: ignore[attr-defined]
    client._client = httpx.Client(  # type: ignore[attr-defined]
        transport=httpx.MockTransport(handler),
        headers={"User-Agent": user_agent},
    )
    return client


def test_get_json_returns_parsed_body():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"ok": True, "url": str(request.url)})

    with _make_client(handler) as c:
        body = c.get_json("https://data.sec.gov/anything.json")
        assert body == {"ok": True, "url": "https://data.sec.gov/anything.json"}


def test_get_json_sends_user_agent():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("User-Agent", ""))
        return httpx.Response(200, json={})

    with _make_client(handler, user_agent="UA me@x.com") as c:
        c.get_json("https://data.sec.gov/x.json")
    assert seen == ["UA me@x.com"]


def test_get_json_404_is_non_retryable():
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(404)

    with _make_client(handler) as c:
        with pytest.raises(SECClientError, match="404"):
            c.get_json("https://data.sec.gov/missing.json")
    assert calls["n"] == 1  # no retries on 404


def test_get_json_retries_on_500_then_succeeds(monkeypatch: pytest.MonkeyPatch):
    # Skip backoff sleeps for fast tests.
    monkeypatch.setattr("sec_financials.sec_client.time.sleep", lambda _: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(500)
        return httpx.Response(200, json={"ok": True})

    with _make_client(handler) as c:
        body = c.get_json("https://data.sec.gov/flaky.json")
    assert body == {"ok": True}
    assert calls["n"] == 3


def test_get_json_gives_up_after_three_attempts(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr("sec_financials.sec_client.time.sleep", lambda _: None)
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    with _make_client(handler) as c:
        with pytest.raises(SECClientError, match="failed after 3 attempts"):
            c.get_json("https://data.sec.gov/dead.json")
    assert calls["n"] == 3
