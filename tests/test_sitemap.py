"""Sitemap walking, including indexes and gzipped documents."""

from __future__ import annotations

import gzip

import httpx

from site_monitor.sitemap import collect_page_urls, parse_sitemap
from site_monitor.http import Fetcher, build_client

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'

URLSET = f"""<?xml version="1.0" encoding="UTF-8"?>
<urlset {NS}>
  <url><loc>https://dvlfirm.com/</loc><lastmod>2026-01-01</lastmod></url>
  <url><loc>https://dvlfirm.com/business-law/trust-restatement/</loc></url>
</urlset>"""

INDEX = f"""<?xml version="1.0" encoding="UTF-8"?>
<sitemapindex {NS}>
  <sitemap><loc>https://dvlfirm.com/page-sitemap.xml</loc></sitemap>
  <sitemap><loc>https://dvlfirm.com/post-sitemap.xml</loc></sitemap>
</sitemapindex>"""


def test_parse_urlset():
    pages, nested = parse_sitemap(URLSET.encode())

    assert pages == [
        "https://dvlfirm.com/",
        "https://dvlfirm.com/business-law/trust-restatement/",
    ]
    assert nested == []


def test_parse_index():
    pages, nested = parse_sitemap(INDEX.encode())

    assert pages == []
    assert len(nested) == 2


def test_malformed_xml_yields_nothing_instead_of_raising():
    assert parse_sitemap(b"<urlset><broken>") == ([], [])


def _fetcher(handler):
    client = build_client(
        user_agent="test",
        timeout=5,
        max_connections=5,
        transport=httpx.MockTransport(handler),
    )
    return client, Fetcher(client, max_retries=2, backoff=0.0)


async def test_index_is_walked_and_pages_deduplicated():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("sitemap_index.xml"):
            return httpx.Response(200, text=INDEX)
        # Both child sitemaps list the same pages; the result must not repeat.
        return httpx.Response(200, text=URLSET)

    client, fetcher = _fetcher(handler)
    async with client:
        pages = await collect_page_urls(
            fetcher, "https://dvlfirm.com/sitemap_index.xml"
        )

    assert pages == [
        "https://dvlfirm.com/",
        "https://dvlfirm.com/business-law/trust-restatement/",
    ]


async def test_gzipped_sitemap_is_decompressed():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            content=gzip.compress(URLSET.encode()),
            headers={"content-type": "application/octet-stream"},
        )

    client, fetcher = _fetcher(handler)
    async with client:
        pages = await collect_page_urls(fetcher, "https://dvlfirm.com/sitemap.xml.gz")

    assert len(pages) == 2


async def test_limit_truncates_and_stops_walking():
    fetched: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        fetched.append(str(request.url))
        if str(request.url).endswith("sitemap_index.xml"):
            return httpx.Response(200, text=INDEX)
        return httpx.Response(200, text=URLSET)

    client, fetcher = _fetcher(handler)
    async with client:
        pages = await collect_page_urls(
            fetcher, "https://dvlfirm.com/sitemap_index.xml", limit=1
        )

    assert pages == ["https://dvlfirm.com/"]
    # Index + first child only; the second child is never fetched.
    assert len(fetched) == 2


async def test_recursive_sitemap_reference_terminates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=f'<?xml version="1.0"?><sitemapindex {NS}>'
            "<sitemap><loc>https://dvlfirm.com/sitemap_index.xml</loc></sitemap>"
            "</sitemapindex>",
        )

    client, fetcher = _fetcher(handler)
    async with client:
        pages = await collect_page_urls(
            fetcher, "https://dvlfirm.com/sitemap_index.xml"
        )

    assert pages == []


async def test_non_200_sitemap_is_skipped_not_fatal():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("sitemap_index.xml"):
            return httpx.Response(200, text=INDEX)
        if url.endswith("page-sitemap.xml"):
            return httpx.Response(404)
        return httpx.Response(200, text=URLSET)

    client, fetcher = _fetcher(handler)
    async with client:
        pages = await collect_page_urls(
            fetcher, "https://dvlfirm.com/sitemap_index.xml"
        )

    assert len(pages) == 2
