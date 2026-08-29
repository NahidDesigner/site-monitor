"""Setup helpers: find a site's sitemap, and pre-flight a configured one.

Hand-writing sitemap URLs for 40+ installs is where a site list goes wrong --
Yoast and Rank Math use /sitemap_index.xml, WP core uses /wp-sitemap.xml, and
plenty of sites redirect /sitemap.xml somewhere else entirely. These helpers
ask the site instead of guessing.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from .elementor import extract_elementor_css_urls
from .http import FetchError, Fetcher
from .sitemap import collect_page_urls

log = logging.getLogger(__name__)

# Probed in order, only when robots.txt does not name a sitemap.
CANDIDATE_PATHS = (
    "/sitemap_index.xml",  # Yoast, Rank Math
    "/wp-sitemap.xml",     # WordPress core 5.5+
    "/sitemap.xml",        # generic; often redirects to one of the above
    "/sitemap-index.xml",  # All in One SEO
)

ROBOTS_SITEMAP_RE = re.compile(r"^\s*sitemap\s*:\s*(\S+)", re.IGNORECASE | re.MULTILINE)


@dataclass
class SiteProbe:
    """What we learned about one site."""

    domain: str
    sitemap: str | None = None
    source: str = ""          # "robots.txt" or "probe" or "configured"
    pages_found: int = 0
    sample_url: str | None = None
    sample_css_count: int | None = None
    error: str | None = None

    @property
    def ok(self) -> bool:
        """Usable means "we know which pages to check" -- a sitemap is one way
        to learn that, an explicit page list is another."""
        return self.error is None and self.pages_found > 0

    @property
    def uses_elementor(self) -> bool | None:
        """None when no page was sampled."""
        if self.sample_css_count is None:
            return None
        return self.sample_css_count > 0


def normalize_domain(raw: str) -> str:
    """'https://www.Example.com/path/' -> 'www.example.com'."""
    value = raw.strip().lower()
    if not value:
        return ""
    if "//" not in value:
        value = f"https://{value}"
    host = urlparse(value).netloc or urlparse(value).path
    return host.strip("/").split("/")[0]


async def sitemaps_from_robots(fetcher: Fetcher, domain: str) -> list[str]:
    """Sitemap URLs a site declares in robots.txt -- the authoritative source."""
    try:
        response = await fetcher.get(f"https://{domain}/robots.txt")
    except FetchError as exc:
        log.debug("robots.txt for %s unreachable: %s", domain, exc.reason)
        return []
    if response.status_code != 200:
        return []

    found: list[str] = []
    for match in ROBOTS_SITEMAP_RE.finditer(response.text):
        url = match.group(1).strip()
        if url.startswith(("http://", "https://")) and url not in found:
            found.append(url)
    return found


async def sample_page(
    fetcher: Fetcher, pages: "list[str] | tuple[str, ...]"
) -> tuple[str | None, int | None]:
    """Fetch one representative page and count its Elementor stylesheets.

    Samples from the middle: a WordPress homepage is often the least
    representative page on the site, and trailing sitemap entries are often
    thin archives. Returns (sampled URL, stylesheet count); the count is None
    when the page could not be read.
    """
    if not pages:
        return None, None

    sample = pages[len(pages) // 2]
    try:
        response = await fetcher.get(sample)
    except FetchError:
        return sample, None
    if response.status_code != 200:
        return sample, None

    return sample, len(extract_elementor_css_urls(response.text, str(response.url)))


async def probe_sitemap(
    fetcher: Fetcher,
    sitemap_url: str,
    *,
    sample_pages: int = 1,
) -> tuple[int, str | None, int | None]:
    """Walk a sitemap and optionally sample a page for Elementor stylesheets.

    Returns (page count, sampled page URL, stylesheets found on it).
    """
    pages = await collect_page_urls(fetcher, sitemap_url)
    if not pages or sample_pages <= 0:
        return len(pages), None, None

    sample, css_count = await sample_page(fetcher, pages)
    return len(pages), sample, css_count


async def discover_site(
    fetcher: Fetcher,
    raw_domain: str,
    *,
    sample_pages: int = 1,
) -> SiteProbe:
    """Find a usable sitemap for one domain and confirm it yields pages."""
    domain = normalize_domain(raw_domain)
    if not domain:
        return SiteProbe(domain=raw_domain, error="could not parse domain")

    probe = SiteProbe(domain=domain)
    candidates = [(url, "robots.txt") for url in await sitemaps_from_robots(fetcher, domain)]
    candidates += [(f"https://{domain}{path}", "probe") for path in CANDIDATE_PATHS]

    seen: set[str] = set()
    last_error = "no sitemap found"
    for url, source in candidates:
        if url in seen:
            continue
        seen.add(url)
        try:
            count, sample, css_count = await probe_sitemap(
                fetcher, url, sample_pages=sample_pages
            )
        except FetchError as exc:
            last_error = f"{url}: {exc.reason}"
            continue
        if count > 0:
            probe.sitemap = url
            probe.source = source
            probe.pages_found = count
            probe.sample_url = sample
            probe.sample_css_count = css_count
            return probe
        last_error = f"{url}: reachable but listed no pages"

    probe.error = last_error
    return probe


async def discover_many(
    fetcher: Fetcher,
    domains: list[str],
    *,
    concurrency: int = 5,
    sample_pages: int = 1,
) -> list[SiteProbe]:
    """Discover many domains at once, preserving input order."""
    semaphore = asyncio.Semaphore(max(1, concurrency))

    async def guarded(domain: str) -> SiteProbe:
        async with semaphore:
            try:
                return await discover_site(fetcher, domain, sample_pages=sample_pages)
            except Exception as exc:  # never let one domain abort the batch
                log.exception("discovery failed for %s", domain)
                return SiteProbe(
                    domain=normalize_domain(domain) or domain,
                    error=f"{type(exc).__name__}: {exc}",
                )

    return list(await asyncio.gather(*(guarded(domain) for domain in domains)))


def render_sites_yaml(probes: list[SiteProbe]) -> str:
    """Emit a sites.yaml body from successful probes.

    Sites that could not be resolved are emitted as comments rather than
    dropped, so nothing goes missing silently on a 40-site setup.
    """
    lines = ["sites:"]
    for probe in probes:
        if probe.ok:
            note = f"  # {probe.pages_found} pages"
            if probe.uses_elementor is False:
                note += ", no Elementor CSS on the sampled page"
            lines.append(f"  - domain: {probe.domain}")
            lines.append(f"    sitemap: {probe.sitemap}{note}")
        else:
            lines.append(f"  # {probe.domain}: {probe.error} -- add by hand")
    return "\n".join(lines) + "\n"


def read_domains(text: str) -> list[str]:
    """Parse a domain list: one per line or comma separated, # comments ignored."""
    domains: list[str] = []
    for line in text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        for part in line.replace(",", " ").split():
            normalized = normalize_domain(part)
            if normalized and normalized not in domains:
                domains.append(normalized)
    return domains
