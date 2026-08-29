"""Sitemap discovery and the sites.yaml it emits."""

from __future__ import annotations

import httpx

from site_monitor.discovery import (
    SiteProbe,
    discover_many,
    discover_site,
    normalize_domain,
    read_domains,
    render_sites_yaml,
    sitemaps_from_robots,
)
from site_monitor.http import Fetcher, build_client

NS = 'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"'
URLSET = (
    f'<?xml version="1.0"?><urlset {NS}>'
    "<url><loc>https://a.com/one/</loc></url>"
    "<url><loc>https://a.com/two/</loc></url>"
    "<url><loc>https://a.com/three/</loc></url></urlset>"
)
ELEMENTOR_PAGE = (
    "<html><head><link rel='stylesheet' "
    "href='https://a.com/wp-content/uploads/elementor/css/post-7.css?ver=1'>"
    "</head><body></body></html>"
)


def _fetcher(handler):
    client = build_client(
        user_agent="test",
        timeout=5,
        max_connections=5,
        transport=httpx.MockTransport(handler),
    )
    return client, Fetcher(client, max_retries=1, backoff=0.0)


def test_normalize_domain_strips_scheme_path_and_case():
    assert normalize_domain("https://www.Example.com/some/path/") == "www.example.com"
    assert normalize_domain("example.com") == "example.com"
    assert normalize_domain("  ") == ""


def test_read_domains_accepts_lines_commas_and_comments():
    text = "a.com, b.com  # two here\n\n# skip me\nhttps://c.com/x\na.com\n"

    assert read_domains(text) == ["a.com", "b.com", "c.com"]


async def test_robots_txt_is_preferred_over_probing():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url.endswith("/robots.txt"):
            return httpx.Response(
                200,
                text="User-agent: *\nDisallow:\nSitemap: https://a.com/custom-map.xml\n",
            )
        if url == "https://a.com/custom-map.xml":
            return httpx.Response(200, text=URLSET)
        if url.startswith("https://a.com/") and url.endswith("/"):
            return httpx.Response(200, text=ELEMENTOR_PAGE)
        return httpx.Response(404)

    client, fetcher = _fetcher(handler)
    async with client:
        probe = await discover_site(fetcher, "a.com")

    assert probe.ok
    assert probe.sitemap == "https://a.com/custom-map.xml"
    assert probe.source == "robots.txt"
    assert probe.pages_found == 3
    # The usual candidate paths are never tried once robots.txt answers.
    assert "https://a.com/sitemap_index.xml" not in requested


async def test_falls_back_to_probing_known_paths():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        # Yoast path missing; WP core path present.
        if url == "https://a.com/wp-sitemap.xml":
            return httpx.Response(200, text=URLSET)
        if url.startswith("https://a.com/") and url.endswith("/"):
            return httpx.Response(200, text=ELEMENTOR_PAGE)
        return httpx.Response(404)

    client, fetcher = _fetcher(handler)
    async with client:
        probe = await discover_site(fetcher, "a.com")

    assert probe.sitemap == "https://a.com/wp-sitemap.xml"
    assert probe.source == "probe"


async def test_sample_page_reports_elementor_stylesheet_count():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        if url == "https://a.com/sitemap_index.xml":
            return httpx.Response(200, text=URLSET)
        return httpx.Response(200, text=ELEMENTOR_PAGE)

    client, fetcher = _fetcher(handler)
    async with client:
        probe = await discover_site(fetcher, "a.com")

    assert probe.uses_elementor is True
    assert probe.sample_css_count == 1
    assert probe.sample_url == "https://a.com/two/"  # sampled from the middle


async def test_site_without_elementor_is_resolved_but_flagged():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        if url == "https://a.com/sitemap_index.xml":
            return httpx.Response(200, text=URLSET)
        return httpx.Response(200, text="<html><head></head></html>")

    client, fetcher = _fetcher(handler)
    async with client:
        probe = await discover_site(fetcher, "a.com")

    assert probe.ok
    assert probe.uses_elementor is False


async def test_unresolvable_domain_reports_an_error_not_a_crash():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no such host", request=request)

    client, fetcher = _fetcher(handler)
    async with client:
        probe = await discover_site(fetcher, "nope.invalid")

    assert not probe.ok
    assert probe.error


async def test_discover_many_preserves_order_and_isolates_failures():
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if "bad.com" in url:
            raise httpx.ConnectError("refused", request=request)
        if url.endswith("/robots.txt"):
            return httpx.Response(404)
        if url.endswith("/sitemap_index.xml"):
            return httpx.Response(200, text=URLSET)
        return httpx.Response(200, text=ELEMENTOR_PAGE)

    client, fetcher = _fetcher(handler)
    async with client:
        probes = await discover_many(fetcher, ["a.com", "bad.com", "c.com"])

    assert [probe.domain for probe in probes] == ["a.com", "bad.com", "c.com"]
    assert probes[0].ok and probes[2].ok
    assert not probes[1].ok


def test_rendered_yaml_is_parseable_and_keeps_failures_as_comments():
    import yaml

    from site_monitor.config import load_sites

    probes = [
        SiteProbe(
            domain="a.com",
            sitemap="https://a.com/sitemap_index.xml",
            source="robots.txt",
            pages_found=143,
            sample_css_count=15,
        ),
        SiteProbe(domain="bad.com", error="no sitemap found"),
    ]

    body = render_sites_yaml(probes)
    parsed = yaml.safe_load(body)

    assert parsed["sites"] == [
        {"domain": "a.com", "sitemap": "https://a.com/sitemap_index.xml"}
    ]
    # The failure survives as a comment so nothing goes missing silently.
    assert "# bad.com: no sitemap found" in body


def test_rendered_yaml_round_trips_through_the_real_loader(tmp_path):
    from site_monitor.config import load_sites

    probes = [
        SiteProbe(
            domain="dvlfirm.com",
            sitemap="https://dvlfirm.com/sitemap_index.xml",
            pages_found=143,
            sample_css_count=15,
        )
    ]
    path = tmp_path / "sites.yaml"
    path.write_text(render_sites_yaml(probes), encoding="utf-8")

    sites = load_sites(path)

    assert sites[0].domain == "dvlfirm.com"
    assert sites[0].sitemap == "https://dvlfirm.com/sitemap_index.xml"


async def test_robots_parsing_ignores_relative_and_malformed_entries():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=(
                "Sitemap: /relative.xml\n"
                "sitemap:https://a.com/ok.xml\n"
                "SITEMAP: https://a.com/ok.xml\n"
                "not a sitemap line\n"
            ),
        )

    client, fetcher = _fetcher(handler)
    async with client:
        found = await sitemaps_from_robots(fetcher, "a.com")

    assert found == ["https://a.com/ok.xml"]


def test_probe_is_ok_without_a_sitemap_when_pages_are_known():
    """A site configured with an explicit page list has no sitemap to report."""
    probe = SiteProbe(domain="a.com", source="pages", pages_found=24)

    assert probe.ok


def test_probe_is_not_ok_when_no_pages_were_found():
    assert not SiteProbe(domain="a.com", sitemap="https://a.com/s.xml").ok
    assert not SiteProbe(domain="a.com", error="boom", pages_found=3).ok
