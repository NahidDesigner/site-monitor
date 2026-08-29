"""Sitemap discovery: walk indexes and collect every page URL."""

from __future__ import annotations

import gzip
import logging
from xml.etree import ElementTree

import httpx

from .http import Fetcher

log = logging.getLogger(__name__)

MAX_DEPTH = 4
GZIP_MAGIC = b"\x1f\x8b"


def _localname(tag: str) -> str:
    """Strip the XML namespace: '{...}loc' -> 'loc'."""
    return tag.rsplit("}", 1)[-1].lower()


def _decode(response: httpx.Response) -> bytes:
    body = response.content
    if body[:2] == GZIP_MAGIC:
        try:
            return gzip.decompress(body)
        except OSError:
            log.warning("%s looked gzipped but would not decompress", response.url)
    return body


def parse_sitemap(body: bytes) -> tuple[list[str], list[str]]:
    """Split a sitemap document into (page urls, nested sitemap urls).

    A <sitemapindex> yields nested sitemaps; a <urlset> yields pages. Malformed
    XML yields nothing rather than blowing up the run.
    """
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError as exc:
        log.warning("could not parse sitemap XML: %s", exc)
        return [], []

    pages: list[str] = []
    nested: list[str] = []
    is_index = _localname(root.tag) == "sitemapindex"

    for entry in root:
        name = _localname(entry.tag)
        if name not in {"url", "sitemap"}:
            continue
        loc = next(
            (
                (child.text or "").strip()
                for child in entry
                if _localname(child.tag) == "loc" and child.text
            ),
            "",
        )
        if not loc:
            continue
        if name == "sitemap" or is_index:
            nested.append(loc)
        else:
            pages.append(loc)

    return pages, nested


async def collect_page_urls(
    fetcher: Fetcher,
    sitemap_url: str,
    *,
    limit: int = 0,
    max_depth: int = MAX_DEPTH,
) -> list[str]:
    """Fetch a sitemap (or sitemap index) and return every page URL it lists.

    Order is preserved and duplicates are dropped, so `limit` slices a stable
    prefix rather than an arbitrary sample.
    """
    pages: list[str] = []
    seen_pages: set[str] = set()
    visited: set[str] = set()
    queue: list[tuple[str, int]] = [(sitemap_url, 0)]

    while queue:
        url, depth = queue.pop(0)
        if url in visited or depth > max_depth:
            continue
        visited.add(url)

        response = await fetcher.get(url)
        if response.status_code != 200:
            log.warning("sitemap %s returned HTTP %s", url, response.status_code)
            continue

        found_pages, nested = parse_sitemap(_decode(response))
        for page in found_pages:
            if page not in seen_pages:
                seen_pages.add(page)
                pages.append(page)
        queue.extend((child, depth + 1) for child in nested)

        if limit and len(pages) >= limit:
            break

    return pages[:limit] if limit else pages
