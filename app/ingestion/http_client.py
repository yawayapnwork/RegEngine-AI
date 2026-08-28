"""Polite, resilient HTTP client for polling sebi.gov.in.

Design goals, in order of priority:
  1. Be a good citizen of a public government site: honor robots.txt, keep a
     floor on the gap between requests, and identify a real contact email in
     a custom header so SEBI's admins can reach us if something's wrong.
  2. Survive the transient failures a public site under load throws at any
     poller (timeouts, 429s, 5xx, connection resets) via bounded exponential
     backoff with jitter.
  3. Spread load across a User-Agent pool and, optionally, an outbound proxy
     pool so a single IP/UA fingerprint issuing thousands of polls a day
     doesn't itself become the reason for a block.

This is a monitoring client for public regulatory filings, not a scraper for
gated or authenticated content — it never attempts to defeat CAPTCHAs, solve
challenge pages, or spoof credentials.
"""
from __future__ import annotations

import asyncio
import logging
import random
import time
from itertools import cycle
from urllib import robotparser
from urllib.parse import urlparse

import httpx

from app.config import Settings
from app.ingestion.exceptions import RobotsDisallowedError, SourceFetchError

logger = logging.getLogger(__name__)

_RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}


class RateLimiter:
    """Enforces a minimum wall-clock gap between successive requests.

    A single async lock serializes acquire() calls so concurrent callers
    can't both observe a stale "last request" timestamp and burst past the
    floor together.
    """

    def __init__(self, min_interval_seconds: float) -> None:
        self._min_interval = min_interval_seconds
        self._lock = asyncio.Lock()
        self._last_request_monotonic: float | None = None

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if self._last_request_monotonic is not None:
                elapsed = now - self._last_request_monotonic
                wait_for = self._min_interval - elapsed
                if wait_for > 0:
                    await asyncio.sleep(wait_for)
            self._last_request_monotonic = time.monotonic()


class _RobotsCache:
    """Per-host robots.txt cache so we don't refetch it on every request."""

    def __init__(self) -> None:
        self._parsers: dict[str, robotparser.RobotFileParser] = {}
        self._lock = asyncio.Lock()

    async def is_allowed(self, client: httpx.AsyncClient, url: str, user_agent: str) -> bool:
        parsed = urlparse(url)
        host = f"{parsed.scheme}://{parsed.netloc}"
        async with self._lock:
            parser = self._parsers.get(host)
            if parser is None:
                parser = robotparser.RobotFileParser()
                try:
                    resp = await client.get(f"{host}/robots.txt", timeout=10.0)
                    if resp.status_code == 200:
                        parser.parse(resp.text.splitlines())
                    else:
                        # No robots.txt (or unreachable) -> treat as unrestricted.
                        parser.parse([])
                except httpx.HTTPError:
                    parser.parse([])
                self._parsers[host] = parser
        return parser.can_fetch(user_agent, url)


class SebiHttpClient:
    """Async HTTP client wrapper with UA rotation, proxy rotation, robots.txt
    compliance, request-rate limiting, and retry-with-backoff."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._rate_limiter = RateLimiter(settings.ingestion_request_min_interval_seconds)
        self._robots = _RobotsCache()
        user_agents = settings.ingestion_user_agents or ["RegEngineAI-Ingestion/1.0"]
        self._user_agents = cycle(user_agents)
        proxies = settings.ingestion_proxy_urls or [None]
        self._proxies = cycle(proxies)
        self._client = httpx.AsyncClient(
            timeout=settings.ingestion_request_timeout_seconds,
            follow_redirects=True,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "SebiHttpClient":
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _next_headers(self) -> dict[str, str]:
        return {
            "User-Agent": next(self._user_agents),
            "Accept": "application/rss+xml, application/xml, text/html, application/pdf;q=0.9, */*;q=0.8",
            "From": self._settings.ingestion_contact_email,
            "X-Ingestion-Client": "RegEngineAI-Ingestion/1.0 (regulatory monitoring; contact via From header)",
        }

    async def get(self, url: str) -> httpx.Response:
        """GET with robots.txt compliance, rate limiting, UA/proxy rotation,
        and bounded exponential-backoff retry on transient failures."""
        headers = self._next_headers()
        proxy = next(self._proxies)

        if self._settings.ingestion_respect_robots_txt:
            allowed = await self._robots.is_allowed(self._client, url, headers["User-Agent"])
            if not allowed:
                raise RobotsDisallowedError(f"robots.txt disallows fetching {url}")

        max_retries = self._settings.ingestion_max_retries
        backoff_base = self._settings.ingestion_retry_backoff_base_seconds
        backoff_max = self._settings.ingestion_retry_backoff_max_seconds

        last_exc: Exception | None = None
        for attempt in range(1, max_retries + 1):
            await self._rate_limiter.acquire()
            try:
                # httpx.AsyncClient is created without a fixed proxy so we can
                # rotate per-request; route this single call through `proxy`.
                if proxy:
                    async with httpx.AsyncClient(
                        timeout=self._settings.ingestion_request_timeout_seconds,
                        follow_redirects=True,
                        proxy=proxy,
                    ) as proxied:
                        response = await proxied.get(url, headers=headers)
                else:
                    response = await self._client.get(url, headers=headers)

                if response.status_code in _RETRYABLE_STATUS:
                    raise httpx.HTTPStatusError(
                        f"Retryable status {response.status_code}",
                        request=response.request,
                        response=response,
                    )
                response.raise_for_status()
                return response

            except (httpx.TransportError, httpx.HTTPStatusError, httpx.TimeoutException) as exc:
                last_exc = exc
                if attempt == max_retries:
                    break
                sleep_for = min(backoff_max, backoff_base * (2 ** (attempt - 1)))
                sleep_for *= 0.75 + random.random() * 0.5  # +/- jitter to avoid thundering-herd retries
                logger.warning(
                    "GET %s failed (attempt %d/%d): %s — retrying in %.1fs",
                    url, attempt, max_retries, exc, sleep_for,
                )
                await asyncio.sleep(sleep_for)
                # Rotate identity on retry: a fresh UA/proxy pairing is more
                # likely to succeed than hammering the same one repeatedly.
                headers = self._next_headers()
                proxy = next(self._proxies)

        raise SourceFetchError(f"Failed to GET {url} after {max_retries} attempts: {last_exc!r}") from last_exc
