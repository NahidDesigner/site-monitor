"""Orchestration: sitemap -> pages -> Elementor stylesheet checks."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

import httpx

from .config import Settings, Site
from .elementor import PageResult, check_page
from .http import FetchError, Fetcher, build_client

log = logging.getLogger(__name__)


@dataclass
class SiteResult:
    """Everything one site's pass produced."""

    domain: str
    sitemap: str = ""
    pages_found: int = 0
    pages: list[PageResult] = field(default_factory=list)
    duration_ms: int = 0
    error: str | None = None

    @property
    def pages_checked(self) -> int:
        return len(self.pages)

    @property
    def assets_checked(self) -> int:
        return sum(page.assets_checked for page in self.pages)

    @property
    def broken_pages(self) -> list[PageResult]:
        return [page for page in self.pages if page.is_broken]

    @property
    def broken_page_count(self) -> int:
        return len(self.broken_pages)

    @property
    def broken_asset_count(self) -> int:
        return sum(len(page.broken) for page in self.pages)

    @property
    def has_findings(self) -> bool:
        """True when this site is worth alerting about."""
        return bool(self.error) or bool(self.broken_pages)


@dataclass
class RunResult:
    """Everything one whole run produced."""

    sites: list[SiteResult] = field(default_factory=list)
    duration_ms: int = 0

    @property
    def pages_checked(self) -> int:
        return sum(site.pages_checked for site in self.sites)

    @property
    def assets_checked(self) -> int:
        return sum(site.assets_checked for site in self.sites)

    @property
    def broken_asset_count(self) -> int:
        return sum(site.broken_asset_count for site in self.sites)

    @property
    def sites_with_findings(self) -> list[SiteResult]:
        return [site for site in self.sites if site.has_findings]

    @property
    def has_findings(self) -> bool:
        return bool(self.sites_with_findings)


async def check_site(
    fetcher: Fetcher,
    site: Site,
    *,
    settings: Settings,
) -> SiteResult:
    """Walk one site's sitemap and check every page it lists."""
    from .sitemap import collect_page_urls  # local: keeps the import graph flat

    started = time.perf_counter()
    result = SiteResult(domain=site.domain, sitemap=site.sitemap)

    limit = site.max_pages or settings.max_pages_per_site or 0

    if site.has_explicit_pages:
        # A curated list is already exact; nothing to fetch or resolve.
        page_urls = list(site.pages[:limit] if limit else site.pages)
    else:
        try:
            page_urls = await collect_page_urls(fetcher, site.sitemap, limit=limit)
        except FetchError as exc:
            result.error = f"sitemap unreachable: {exc.reason}"
            result.duration_ms = int((time.perf_counter() - started) * 1000)
            log.error("%s: %s", site.domain, result.error)
            return result

    result.pages_found = len(page_urls)
    if not page_urls:
        result.error = (
            "site lists no pages"
            if site.has_explicit_pages
            else "sitemap listed no page URLs"
        )
        result.duration_ms = int((time.perf_counter() - started) * 1000)
        log.error("%s: %s", site.domain, result.error)
        return result

    log.info("%s: checking %s pages", site.domain, len(page_urls))

    page_semaphore = asyncio.Semaphore(settings.page_concurrency)
    asset_semaphore = asyncio.Semaphore(settings.asset_concurrency)

    async def guarded(url: str) -> PageResult:
        async with page_semaphore:
            return await check_page(fetcher, url, asset_semaphore=asset_semaphore)

    result.pages = list(await asyncio.gather(*(guarded(url) for url in page_urls)))
    result.duration_ms = int((time.perf_counter() - started) * 1000)

    log.info(
        "%s: %s broken stylesheets across %s pages (%s stylesheets checked, %sms)",
        site.domain,
        result.broken_asset_count,
        result.broken_page_count,
        result.assets_checked,
        result.duration_ms,
    )
    return result


async def run_checks(
    settings: Settings,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    on_site_complete=None,
) -> RunResult:
    """Check every enabled site, at most `site_concurrency` at a time.

    `on_site_complete` is called with each SiteResult as it lands, so callers
    can persist incrementally instead of holding everything to the end.
    """
    started = time.perf_counter()
    sites = [site for site in settings.sites if site.enabled]
    if not sites:
        log.warning("no enabled sites to check")
        return RunResult()

    # The connection pool has to cover every site running in parallel, each of
    # which fans out to pages and then to stylesheets.
    max_connections = max(
        settings.site_concurrency
        * max(settings.page_concurrency, settings.asset_concurrency),
        settings.asset_concurrency,
    )

    site_semaphore = asyncio.Semaphore(settings.site_concurrency)
    run = RunResult()

    async with build_client(
        user_agent=settings.user_agent,
        timeout=settings.request_timeout,
        max_connections=max_connections,
        transport=transport,
    ) as client:
        fetcher = Fetcher(
            client,
            max_retries=settings.max_retries,
            backoff=settings.retry_backoff,
        )

        async def guarded(site: Site) -> SiteResult:
            async with site_semaphore:
                try:
                    return await check_site(fetcher, site, settings=settings)
                except Exception as exc:  # one bad site must not kill the run
                    log.exception("%s: unexpected failure", site.domain)
                    return SiteResult(
                        domain=site.domain,
                        sitemap=site.sitemap,
                        error=f"unexpected failure: {type(exc).__name__}: {exc}",
                    )

        for coro in asyncio.as_completed([guarded(site) for site in sites]):
            site_result = await coro
            run.sites.append(site_result)
            if on_site_complete is not None:
                on_site_complete(site_result)

    # as_completed returns out of order; restore sites.yaml order for reporting.
    order = {site.domain: index for index, site in enumerate(sites)}
    run.sites.sort(key=lambda result: order.get(result.domain, len(order)))
    run.duration_ms = int((time.perf_counter() - started) * 1000)
    return run
