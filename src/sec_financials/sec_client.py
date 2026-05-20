"""HTTP client for SEC EDGAR.

Enforces the SEC fair-access policy:
  - Sends a descriptive User-Agent header (from `SEC_USER_AGENT` env var).
  - Caps request rate at 10 req/sec (one-second sliding window).
  - Retries on 429 / 5xx with exponential backoff, up to 3 attempts.

See REQUIREMENTS.md §6 and §11 for the policy text.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Any

import httpx

SEC_BASE_DATA = "https://data.sec.gov"
SEC_BASE_WWW = "https://www.sec.gov"

_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=10.0)
_MAX_REQUESTS_PER_SECOND = 10
_MAX_ATTEMPTS = 3
_RETRY_BACKOFF_SECONDS = (1.0, 2.0, 4.0)


class SECClientError(RuntimeError):
    """Raised when SEC requests fail after retries, or when configuration is bad."""


class _RateLimiter:
    """Sliding-window rate limiter: at most `max_per_second` calls per second."""

    def __init__(self, max_per_second: int) -> None:
        self._max = max_per_second
        self._timestamps: deque[float] = deque()
        self._lock = threading.Lock()

    def acquire(self) -> None:
        """Block until a request slot is available."""
        with self._lock:
            now = time.monotonic()
            # Drop timestamps older than 1 second.
            while self._timestamps and now - self._timestamps[0] >= 1.0:
                self._timestamps.popleft()
            if len(self._timestamps) >= self._max:
                sleep_for = 1.0 - (now - self._timestamps[0])
                if sleep_for > 0:
                    time.sleep(sleep_for)
                # Re-drain after sleeping.
                now = time.monotonic()
                while self._timestamps and now - self._timestamps[0] >= 1.0:
                    self._timestamps.popleft()
            self._timestamps.append(time.monotonic())


def _resolve_user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT", "").strip()
    if not ua:
        raise SECClientError(
            "SEC_USER_AGENT environment variable is not set. "
            "SEC fair-access policy requires a descriptive User-Agent with a "
            "real contact email. Copy .env.example to .env and edit it, or "
            'set the variable directly: $env:SEC_USER_AGENT = "App Name '
            'you@example.com"'
        )
    if "@" not in ua:
        raise SECClientError(
            f"SEC_USER_AGENT does not contain an email address (got {ua!r}). "
            "The SEC requires a real contact email in the User-Agent."
        )
    return ua


class SECClient:
    """Thin, polite client for SEC EDGAR JSON endpoints."""

    def __init__(
        self,
        *,
        user_agent: str | None = None,
        timeout: httpx.Timeout = _DEFAULT_TIMEOUT,
        rate_limiter: _RateLimiter | None = None,
    ) -> None:
        self._user_agent = user_agent or _resolve_user_agent()
        self._rate_limiter = rate_limiter or _RateLimiter(_MAX_REQUESTS_PER_SECOND)
        self._client = httpx.Client(
            timeout=timeout,
            headers={
                "User-Agent": self._user_agent,
                "Accept": "application/json",
                "Accept-Encoding": "gzip, deflate",
            },
            follow_redirects=True,
        )

    # Context-manager support so callers can use `with SECClient() as c: ...`
    def __enter__(self) -> SECClient:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        self._client.close()

    def get_json(self, url: str) -> Any:
        """GET `url` and return parsed JSON, with rate limiting and retries.

        Raises SECClientError if all retries fail.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_ATTEMPTS):
            self._rate_limiter.acquire()
            try:
                response = self._client.get(url)
            except httpx.HTTPError as e:
                last_exc = e
                self._sleep_for_attempt(attempt)
                continue

            # 404 is a real (non-retryable) answer for unknown tickers / CIKs.
            if response.status_code == 404:
                raise SECClientError(f"SEC returned 404 for {url}")

            # 429 (rate limited) and 5xx are retryable.
            if response.status_code == 429 or 500 <= response.status_code < 600:
                last_exc = SECClientError(
                    f"SEC returned {response.status_code} for {url} (attempt "
                    f"{attempt + 1}/{_MAX_ATTEMPTS})"
                )
                self._sleep_for_attempt(attempt)
                continue

            # Any other non-2xx is non-retryable.
            response.raise_for_status()

            try:
                return response.json()
            except ValueError as e:
                raise SECClientError(
                    f"SEC response at {url} was not valid JSON: {e}"
                ) from e

        assert last_exc is not None
        raise SECClientError(
            f"SEC request to {url} failed after {_MAX_ATTEMPTS} attempts: "
            f"{last_exc}"
        ) from last_exc

    @staticmethod
    def _sleep_for_attempt(attempt: int) -> None:
        """Backoff sleep — only on retryable failures, never after the last try."""
        if attempt + 1 < _MAX_ATTEMPTS:
            time.sleep(_RETRY_BACKOFF_SECONDS[attempt])
