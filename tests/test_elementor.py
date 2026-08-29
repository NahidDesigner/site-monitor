"""Detection tests: the two-part health rule and the href extraction."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from fixtures import (
    BROKEN_CSS,
    HEALTHY_CSS,
    PAGE_URL,
    UNRELATED_CSS,
    WORDPRESS_404_HTML,
    build_page_html,
)
from site_monitor.elementor import (
    check_page,
    extract_elementor_css_urls,
    judge_asset,
)
from site_monitor.http import Fetcher, build_client


# -- extraction ---------------------------------------------------------------


def test_extracts_every_elementor_stylesheet_and_nothing_else():
    urls = extract_elementor_css_urls(build_page_html(), PAGE_URL)

    assert len(urls) == len(HEALTHY_CSS) + 1
    assert BROKEN_CSS in urls
    assert set(HEALTHY_CSS).issubset(urls)
    for unrelated in UNRELATED_CSS:
        assert unrelated not in urls


def test_extraction_handles_quoting_escaping_and_relative_urls():
    html = (
        "<link rel='stylesheet' href='/wp-content/uploads/elementor/css/post-1.css?ver=1' />"
        '<link rel="stylesheet" href="//cdn.example.com/wp-content/uploads/elementor/css/post-2.css?ver=2" />'
        '<link rel="stylesheet" HREF = "https://x.test/elementor/css/post-3.css?ver=3&#038;x=1" />'
    )
    urls = extract_elementor_css_urls(html, "https://example.com/page/")

    assert urls == [
        "https://example.com/wp-content/uploads/elementor/css/post-1.css?ver=1",
        "https://cdn.example.com/wp-content/uploads/elementor/css/post-2.css?ver=2",
        "https://x.test/elementor/css/post-3.css?ver=3&x=1",
    ]


def test_extraction_deduplicates_repeated_hrefs():
    href = "https://example.com/wp-content/uploads/elementor/css/post-9.css?ver=1"
    html = f'<link href="{href}"><link href="{href}">'

    assert extract_elementor_css_urls(html, "https://example.com/") == [href]


def test_extraction_resolves_against_the_final_url_after_redirects():
    html = "<link href='/wp-content/uploads/elementor/css/post-1.css?ver=1'>"

    urls = extract_elementor_css_urls(html, "https://www.example.com/final/")

    assert urls == [
        "https://www.example.com/wp-content/uploads/elementor/css/post-1.css?ver=1"
    ]


# -- the health rule ----------------------------------------------------------


def test_200_text_css_is_healthy():
    result = judge_asset(BROKEN_CSS, 200, "text/css; charset=UTF-8", 5)

    assert result.ok
    assert result.reason is None
    assert result.content_type == "text/css"


def test_the_real_failure_404_with_html_body_is_broken():
    result = judge_asset(BROKEN_CSS, 404, "text/html; charset=UTF-8", 5)

    assert not result.ok
    assert "404" in result.reason
    assert "text/html" in result.reason


def test_200_but_html_content_type_is_broken():
    """A soft-404: WordPress serves its error page with HTTP 200."""
    result = judge_asset(BROKEN_CSS, 200, "text/html; charset=UTF-8", 5)

    assert not result.ok
    assert "text/css" in result.reason


def test_missing_content_type_is_broken():
    result = judge_asset(BROKEN_CSS, 200, None, 5)

    assert not result.ok
    assert "missing" in result.reason


@pytest.mark.parametrize("status", [301, 403, 500, 503])
def test_any_non_200_status_is_broken(status):
    assert not judge_asset(BROKEN_CSS, status, "text/css", 5).ok


def test_content_type_matching_is_case_and_parameter_insensitive():
    assert judge_asset(BROKEN_CSS, 200, "TEXT/CSS;charset=utf-8", 5).ok


# -- end to end over a mock transport ----------------------------------------


def _handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url == PAGE_URL:
        return httpx.Response(
            200, text=build_page_html(), headers={"content-type": "text/html"}
        )
    if url == BROKEN_CSS:
        # Exactly what the live site does: 404 + WordPress's HTML 404 page.
        return httpx.Response(
            404,
            text=WORDPRESS_404_HTML,
            headers={"content-type": "text/html; charset=UTF-8"},
        )
    if url in HEALTHY_CSS:
        return httpx.Response(
            200, text=".e{color:red}", headers={"content-type": "text/css"}
        )
    return httpx.Response(200, text="", headers={"content-type": "text/css"})


async def _run_page(handler=_handler, url: str = PAGE_URL):
    async with build_client(
        user_agent="test-agent",
        timeout=5,
        max_connections=10,
        transport=httpx.MockTransport(handler),
    ) as client:
        fetcher = Fetcher(client, max_retries=2, backoff=0.0)
        return await check_page(
            fetcher, url, asset_semaphore=asyncio.Semaphore(5)
        )


async def test_end_to_end_flags_only_the_stale_stylesheet():
    """The verified dvlfirm.com case, reproduced against a mock transport."""
    result = await _run_page()

    assert result.status_code == 200
    assert result.assets_checked == 15
    assert len(result.broken) == 1
    assert result.broken[0].url == BROKEN_CSS
    assert result.broken[0].status_code == 404
    assert result.broken[0].content_type == "text/html"
    assert result.is_broken


async def test_healthy_page_reports_nothing():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PAGE_URL:
            return httpx.Response(
                200, text=build_page_html(), headers={"content-type": "text/html"}
            )
        return httpx.Response(200, headers={"content-type": "text/css"})

    result = await _run_page(handler)

    assert result.assets_checked == 15
    assert result.broken == ()
    assert not result.is_broken


async def test_page_sends_a_browser_user_agent_and_follows_redirects():
    seen: list[str] = []
    final = "https://dvlfirm.com/business-law/trust-restatement/"

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers.get("user-agent", ""))
        if str(request.url) == "http://dvlfirm.com/business-law/trust-restatement":
            return httpx.Response(301, headers={"location": final})
        return _handler(request)

    result = await _run_page(
        handler, url="http://dvlfirm.com/business-law/trust-restatement"
    )

    assert result.status_code == 200
    assert len(result.broken) == 1
    assert all("Mozilla" in ua or ua == "test-agent" for ua in seen)


async def test_head_rejecting_server_falls_back_to_get():
    """A 405 on HEAD is the server's limitation, not a broken stylesheet."""
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        if str(request.url) == PAGE_URL:
            return httpx.Response(
                200, text=build_page_html(), headers={"content-type": "text/html"}
            )
        if request.method == "HEAD":
            return httpx.Response(405)
        return httpx.Response(
            200, text=".e{}", headers={"content-type": "text/css"}
        )

    result = await _run_page(handler)

    assert result.broken == ()
    assert "GET" in methods and "HEAD" in methods


async def test_unreachable_stylesheet_is_reported_as_broken():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == PAGE_URL:
            return httpx.Response(
                200, text=build_page_html(), headers={"content-type": "text/html"}
            )
        raise httpx.ConnectError("connection refused", request=request)

    result = await _run_page(handler)

    assert len(result.broken) == 15
    assert all(asset.status_code is None for asset in result.broken)
    assert "request failed" in result.broken[0].reason


async def test_page_that_404s_is_an_error_not_a_css_finding():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text="nope", headers={"content-type": "text/html"})

    result = await _run_page(handler)

    assert result.error == "page returned HTTP 404"
    assert result.broken == ()
    assert result.is_broken


async def test_page_without_elementor_is_skipped_cleanly():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text="<html><head><link href='/style.css'></head></html>",
            headers={"content-type": "text/html"},
        )

    result = await _run_page(handler)

    assert result.assets_checked == 0
    assert not result.is_broken
