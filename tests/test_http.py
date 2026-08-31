"""Retry behaviour: transient failures retry, real answers do not."""

from __future__ import annotations

import httpx
import pytest

from site_monitor.http import FetchError, Fetcher, browser_headers, build_client


def _fetcher(handler, *, max_retries=3):
    client = build_client(
        user_agent="test-agent",
        timeout=5,
        max_connections=5,
        transport=httpx.MockTransport(handler),
    )
    return client, Fetcher(client, max_retries=max_retries, backoff=0.0)


async def test_transport_errors_are_retried_then_succeed():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise httpx.ConnectTimeout("timed out", request=request)
        return httpx.Response(200, text="ok")

    client, fetcher = _fetcher(handler)
    async with client:
        response = await fetcher.get("https://example.com/")

    assert response.status_code == 200
    assert attempts["n"] == 3


async def test_exhausted_retries_raise_fetch_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client, fetcher = _fetcher(handler, max_retries=2)
    async with client:
        with pytest.raises(FetchError) as info:
            await fetcher.get("https://example.com/")

    assert "ConnectError" in info.value.reason


@pytest.mark.parametrize("status", [429, 500, 502, 503, 504])
async def test_transient_statuses_are_retried(status):
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(200 if attempts["n"] > 1 else status)

    client, fetcher = _fetcher(handler)
    async with client:
        response = await fetcher.get("https://example.com/")

    assert response.status_code == 200
    assert attempts["n"] == 2


async def test_404_is_returned_immediately_never_retried():
    """A 404 is the signal this tool exists to find -- retrying only wastes time."""
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(404)

    client, fetcher = _fetcher(handler)
    async with client:
        response = await fetcher.head("https://example.com/x.css")

    assert response.status_code == 404
    assert attempts["n"] == 1


async def test_persistently_transient_status_is_returned_after_last_attempt():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client, fetcher = _fetcher(handler, max_retries=2)
    async with client:
        response = await fetcher.get("https://example.com/")

    assert response.status_code == 503


def test_browser_headers_look_like_a_browser():
    headers = browser_headers("Mozilla/5.0 test")

    assert headers["User-Agent"] == "Mozilla/5.0 test"
    assert "text/html" in headers["Accept"]


def test_no_cache_busting_headers_are_sent():
    """Two reasons, and the second is the important one.

    A browser sends Cache-Control/Pragma only on a hard refresh, so sending them
    marks the request as unusual. And they ask intermediaries to bypass the
    cache -- the stale cache being measured. A layer that honoured them would
    hand back freshly generated HTML, and the stale ?ver= reference this tool
    exists to find would never appear.
    """
    headers = browser_headers("Mozilla/5.0 test")

    assert "Cache-Control" not in headers
    assert "Pragma" not in headers


async def test_a_cached_page_is_what_gets_checked():
    """End to end: a server that varies on cache-busting must serve us the
    cached copy, because that is the copy with the broken reference in it."""
    STALE = "<html><head><link href='/elementor/css/post-1.css?ver=OLD'></head></html>"
    FRESH = "<html><head><link href='/elementor/css/post-1.css?ver=NEW'></head></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        busting = request.headers.get("cache-control", "") or request.headers.get("pragma", "")
        return httpx.Response(200, text=FRESH if busting else STALE,
                              headers={"content-type": "text/html"})

    client, fetcher = _fetcher(handler)
    async with client:
        response = await fetcher.get("https://example.com/")

    assert "ver=OLD" in response.text
