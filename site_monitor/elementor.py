"""Task 1: detect Elementor CSS files that stale HTML still references.

Nginx FastCGI cache and Cloudflare can serve HTML long after Elementor has
regenerated its per-post stylesheets. The cached HTML points at the old
`?ver=` timestamp, that URL 404s, and WordPress answers the 404 with its own
HTML page -- `content-type: text/html`, HTTP 404. The browser parses that as a
stylesheet with zero rules, so the page loads with no error in the console and
a silently broken layout.

So a stylesheet is only healthy when BOTH hold: HTTP 200, and a content type
that actually says CSS.
"""

from __future__ import annotations

import asyncio
import html
import logging
import re
import time
from dataclasses import dataclass
from urllib.parse import urljoin

from .http import FetchError, Fetcher

log = logging.getLogger(__name__)

# Elementor writes its per-post/global stylesheets under
# wp-content/uploads/elementor/css/. We match the directory rather than a full
# absolute URL so multisite, CDN rewrites and protocol-relative hrefs all hit.
#
# `?ver=` is not required by the pattern: a reference that lost its version
# string is just as capable of 404ing, and matching without it costs one extra
# HEAD on a healthy page.
ELEMENTOR_CSS_HREF_RE = re.compile(
    r"""href\s*=\s*(?P<quote>["'])(?P<url>[^"'>]*?elementor/css/[^"'>]*?\.css[^"'>]*)(?P=quote)""",
    re.IGNORECASE,
)

# Servers that refuse HEAD outright -- re-ask with GET rather than reporting a
# false breakage.
HEAD_UNSUPPORTED_STATUSES = frozenset({403, 405, 501})


@dataclass(frozen=True)
class AssetResult:
    """The verdict on one stylesheet URL."""

    url: str
    status_code: int | None
    content_type: str | None
    ok: bool
    reason: str | None
    elapsed_ms: int


@dataclass(frozen=True)
class PageResult:
    """The verdict on one page: which of its stylesheets are broken."""

    url: str
    status_code: int | None
    assets_checked: int
    broken: tuple[AssetResult, ...]
    error: str | None = None

    @property
    def is_broken(self) -> bool:
        return bool(self.broken) or self.error is not None


def extract_elementor_css_urls(html_text: str, base_url: str) -> list[str]:
    """Pull every Elementor stylesheet href out of a page, absolute and deduped.

    WordPress HTML-escapes ampersands in attributes (`&#038;`), so hrefs are
    unescaped before being resolved against the page's *final* URL -- the one
    after redirects, which is what the browser would resolve against too.
    """
    urls: list[str] = []
    seen: set[str] = set()
    for match in ELEMENTOR_CSS_HREF_RE.finditer(html_text):
        raw = html.unescape(match.group("url").strip())
        if not raw:
            continue
        absolute = urljoin(base_url, raw)
        if absolute not in seen:
            seen.add(absolute)
            urls.append(absolute)
    return urls


def judge_asset(
    url: str,
    status_code: int,
    content_type: str | None,
    elapsed_ms: int,
) -> AssetResult:
    """Apply the two-part health rule to one stylesheet response."""
    normalized = (content_type or "").split(";", 1)[0].strip().lower()

    if status_code != 200:
        reason = f"HTTP {status_code}"
        if normalized:
            reason += f" (content-type: {normalized})"
        ok = False
    elif "text/css" not in normalized:
        reason = f"content-type is {normalized or 'missing'}, not text/css"
        ok = False
    else:
        reason = None
        ok = True

    return AssetResult(
        url=url,
        status_code=status_code,
        content_type=normalized or None,
        ok=ok,
        reason=reason,
        elapsed_ms=elapsed_ms,
    )


async def check_asset(fetcher: Fetcher, url: str) -> AssetResult:
    """HEAD one stylesheet and judge it."""
    started = time.perf_counter()
    try:
        response = await fetcher.head(url)
        if response.status_code in HEAD_UNSUPPORTED_STATUSES:
            # The server may simply not answer HEAD for this path; confirm with
            # a GET before calling the stylesheet broken.
            response = await fetcher.get(url)
    except FetchError as exc:
        return AssetResult(
            url=url,
            status_code=None,
            content_type=None,
            ok=False,
            reason=f"request failed: {exc.reason}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )

    return judge_asset(
        url=url,
        status_code=response.status_code,
        content_type=response.headers.get("content-type"),
        elapsed_ms=int((time.perf_counter() - started) * 1000),
    )


async def check_page(
    fetcher: Fetcher,
    page_url: str,
    *,
    asset_semaphore: asyncio.Semaphore,
) -> PageResult:
    """Fetch a page and check every Elementor stylesheet it references."""
    try:
        response = await fetcher.get(page_url)
    except FetchError as exc:
        return PageResult(
            url=page_url,
            status_code=None,
            assets_checked=0,
            broken=(),
            error=f"page request failed: {exc.reason}",
        )

    if response.status_code != 200:
        return PageResult(
            url=page_url,
            status_code=response.status_code,
            assets_checked=0,
            broken=(),
            error=f"page returned HTTP {response.status_code}",
        )

    css_urls = extract_elementor_css_urls(response.text, str(response.url))
    if not css_urls:
        log.debug("no Elementor stylesheets referenced by %s", page_url)
        return PageResult(
            url=page_url,
            status_code=response.status_code,
            assets_checked=0,
            broken=(),
        )

    async def guarded(url: str) -> AssetResult:
        async with asset_semaphore:
            return await check_asset(fetcher, url)

    results = await asyncio.gather(*(guarded(url) for url in css_urls))
    broken = tuple(result for result in results if not result.ok)

    if broken:
        log.info(
            "%s: %s/%s Elementor stylesheets broken",
            page_url,
            len(broken),
            len(results),
        )

    return PageResult(
        url=page_url,
        status_code=response.status_code,
        assets_checked=len(results),
        broken=broken,
    )


__all__ = [
    "AssetResult",
    "PageResult",
    "check_asset",
    "check_page",
    "extract_elementor_css_urls",
    "judge_asset",
]