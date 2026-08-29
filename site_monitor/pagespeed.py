"""PageSpeed Insights: run Lighthouse against monitored pages and keep history.

One URL per site by default -- the homepage. Testing all 1,470 pages would be
~2,940 API calls per run, which is neither affordable nor useful; performance
regressions show up on the homepage first and the per-page detail belongs in a
targeted run, not a scheduled sweep.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from .config import Settings, Site
from .db import Database
from .http import FetchError, Fetcher, build_client

log = logging.getLogger(__name__)

API_URL = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"

# Lighthouse audit ids -> the column we keep them in.
AUDIT_FIELDS = {
    "first-contentful-paint": "fcp_ms",
    "largest-contentful-paint": "lcp_ms",
    "cumulative-layout-shift": "cls",
    "total-blocking-time": "tbt_ms",
    "speed-index": "speed_index",
    "interactive": "tti_ms",
}

STRATEGIES = ("mobile", "desktop")


@dataclass
class PageSpeedResult:
    domain: str
    url: str
    strategy: str
    performance: float | None = None
    fcp_ms: float | None = None
    lcp_ms: float | None = None
    cls: float | None = None
    tbt_ms: float | None = None
    speed_index: float | None = None
    tti_ms: float | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and self.performance is not None


def parse_response(domain: str, url: str, strategy: str, payload: dict) -> PageSpeedResult:
    """Pull the handful of numbers worth keeping out of a Lighthouse report."""
    result = PageSpeedResult(domain=domain, url=url, strategy=strategy)
    lighthouse = payload.get("lighthouseResult") or {}

    categories = lighthouse.get("categories") or {}
    score = (categories.get("performance") or {}).get("score")
    if isinstance(score, (int, float)):
        # Lighthouse reports 0-1; a percentage is what people talk about.
        result.performance = round(float(score) * 100, 1)

    audits = lighthouse.get("audits") or {}
    for audit_id, field in AUDIT_FIELDS.items():
        value = (audits.get(audit_id) or {}).get("numericValue")
        if isinstance(value, (int, float)):
            setattr(result, field, round(float(value), 3))

    if result.performance is None:
        message = ((payload.get("error") or {}).get("message")) or (
            "no performance score in the response"
        )
        result.error = str(message)[:400]
    return result


async def test_url(
    fetcher: Fetcher,
    domain: str,
    url: str,
    strategy: str,
    api_key: str | None,
) -> PageSpeedResult:
    """One PageSpeed test. Never raises; failures come back on the result."""
    params = {
        "url": url,
        "strategy": strategy,
        "category": "performance",
    }
    if api_key:
        params["key"] = api_key

    try:
        response = await fetcher.get(API_URL, params=params)
    except FetchError as exc:
        return PageSpeedResult(
            domain=domain, url=url, strategy=strategy,
            error=f"request failed: {exc.reason}",
        )

    if response.status_code != 200:
        detail = ""
        try:
            detail = (response.json().get("error") or {}).get("message", "")
        except (ValueError, AttributeError):
            detail = response.text[:200]
        return PageSpeedResult(
            domain=domain, url=url, strategy=strategy,
            error=f"HTTP {response.status_code}: {detail}"[:400],
        )

    try:
        payload = response.json()
    except ValueError:
        return PageSpeedResult(
            domain=domain, url=url, strategy=strategy,
            error="response was not JSON",
        )

    return parse_response(domain, url, strategy, payload)


def targets_for(sites: list[Site]) -> list[tuple[str, str]]:
    """(domain, url) pairs to test — one representative page per site."""
    targets: list[tuple[str, str]] = []
    for site in sites:
        if site.pages:
            targets.append((site.domain, site.pages[0]))
        elif site.sitemap:
            # No curated list: fall back to the site root.
            targets.append((site.domain, f"https://{site.domain}/"))
    return targets


async def run_pagespeed(
    settings: Settings,
    database: Database,
    sites: list[Site],
    *,
    strategies: "tuple[str, ...] | None" = None,
    concurrency: int = 2,
    on_result=None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[int, int, int]:
    """Test every site and store the results.

    Returns (run_id, tested, failures). Concurrency stays low by default:
    PageSpeed is a shared Google quota, and hammering it earns 429s that cost
    more time than the parallelism saves.
    """
    picked = tuple(strategies or settings.pagespeed_strategies or ("mobile",))
    targets = targets_for(sites)
    jobs = [
        (domain, url, strategy)
        for domain, url in targets
        for strategy in picked
    ]

    run_id = database.start_pagespeed_run()
    tested = 0
    failures = 0

    if not jobs:
        database.finish_pagespeed_run(
            run_id, status="completed", urls_tested=0, failures=0,
            error="no sites to test",
        )
        return run_id, 0, 0

    semaphore = asyncio.Semaphore(max(1, concurrency))

    # PageSpeed can take 30s+ per URL on a slow site; a normal request timeout
    # would abort perfectly good tests.
    async with build_client(
        user_agent=settings.user_agent,
        timeout=max(settings.request_timeout, 120.0),
        max_connections=max(2, concurrency),
        transport=transport,
    ) as client:
        fetcher = Fetcher(client, max_retries=settings.max_retries, backoff=2.0)

        async def guarded(job) -> PageSpeedResult:
            domain, url, strategy = job
            async with semaphore:
                return await test_url(
                    fetcher, domain, url, strategy, settings.pagespeed_api_key
                )

        try:
            for coro in asyncio.as_completed([guarded(job) for job in jobs]):
                result = await coro
                database.record_pagespeed_result(run_id, result)
                tested += 1
                if not result.ok:
                    failures += 1
                    log.warning("pagespeed %s (%s): %s", result.url, result.strategy, result.error)
                if on_result is not None:
                    on_result(result)
        except Exception as exc:
            database.finish_pagespeed_run(
                run_id, status="failed", urls_tested=tested, failures=failures,
                error=f"{type(exc).__name__}: {exc}",
            )
            raise

    database.finish_pagespeed_run(
        run_id, status="completed", urls_tested=tested, failures=failures
    )
    return run_id, tested, failures
