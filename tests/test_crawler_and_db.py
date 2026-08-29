"""Crawler orchestration and persistence."""

from __future__ import annotations

import httpx

from fixtures import BROKEN_CSS, HEALTHY_CSS, PAGE_URL, build_page_html
from site_monitor.config import Settings, Site
from site_monitor.crawler import run_checks
from site_monitor.db import Database

SITEMAP = """<?xml version="1.0" encoding="UTF-8"?>
<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
  <url><loc>https://dvlfirm.com/business-law/trust-restatement/</loc></url>
  <url><loc>https://dvlfirm.com/healthy/</loc></url>
</urlset>"""

HEALTHY_PAGE = (
    "<html><head><link rel='stylesheet' href='"
    f"{HEALTHY_CSS[0]}'></head><body></body></html>"
)


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith("sitemap.xml"):
        return httpx.Response(200, text=SITEMAP)
    if url == PAGE_URL:
        return httpx.Response(
            200, text=build_page_html(), headers={"content-type": "text/html"}
        )
    if url == "https://dvlfirm.com/healthy/":
        return httpx.Response(
            200, text=HEALTHY_PAGE, headers={"content-type": "text/html"}
        )
    if url == BROKEN_CSS:
        return httpx.Response(404, headers={"content-type": "text/html"})
    return httpx.Response(200, headers={"content-type": "text/css"})


def settings_for(tmp_path, **overrides) -> Settings:
    defaults = dict(
        database_path=tmp_path / "test.db",
        site_concurrency=2,
        page_concurrency=4,
        asset_concurrency=4,
        max_retries=2,
        retry_backoff=0.0,
        sites=(
            Site(domain="dvlfirm.com", sitemap="https://dvlfirm.com/sitemap.xml"),
        ),
    )
    defaults.update(overrides)
    return Settings(**defaults)


async def test_run_checks_finds_the_one_broken_stylesheet(tmp_path):
    run = await run_checks(
        settings_for(tmp_path), transport=httpx.MockTransport(handler)
    )

    assert len(run.sites) == 1
    site = run.sites[0]
    assert site.pages_found == 2
    assert site.pages_checked == 2
    assert site.assets_checked == 16  # 15 on the broken page, 1 on the healthy one
    assert site.broken_page_count == 1
    assert site.broken_asset_count == 1
    assert site.broken_pages[0].url == PAGE_URL
    assert run.has_findings


async def test_disabled_sites_are_skipped(tmp_path):
    settings = settings_for(
        tmp_path,
        sites=(
            Site(
                domain="dvlfirm.com",
                sitemap="https://dvlfirm.com/sitemap.xml",
                enabled=False,
            ),
        ),
    )

    run = await run_checks(settings, transport=httpx.MockTransport(handler))

    assert run.sites == []
    assert not run.has_findings


async def test_unreachable_sitemap_is_a_site_error_not_a_crash(tmp_path):
    def failing(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    run = await run_checks(
        settings_for(tmp_path), transport=httpx.MockTransport(failing)
    )

    assert run.sites[0].error.startswith("sitemap unreachable")
    assert run.has_findings


async def test_one_failing_site_does_not_stop_the_others(tmp_path):
    def mixed(request: httpx.Request) -> httpx.Response:
        if "broken-site" in str(request.url):
            raise httpx.ConnectError("refused", request=request)
        return handler(request)

    settings = settings_for(
        tmp_path,
        sites=(
            Site(domain="broken-site.com", sitemap="https://broken-site.com/sitemap.xml"),
            Site(domain="dvlfirm.com", sitemap="https://dvlfirm.com/sitemap.xml"),
        ),
    )

    run = await run_checks(settings, transport=httpx.MockTransport(mixed))

    # Order follows sites.yaml even though results arrive as they finish.
    assert [site.domain for site in run.sites] == ["broken-site.com", "dvlfirm.com"]
    assert run.sites[0].error is not None
    assert run.sites[1].broken_asset_count == 1


async def test_max_pages_per_site_caps_the_crawl(tmp_path):
    run = await run_checks(
        settings_for(tmp_path, max_pages_per_site=1),
        transport=httpx.MockTransport(handler),
    )

    assert run.sites[0].pages_checked == 1


async def test_concurrency_limits_are_respected(tmp_path):
    """Never more than page_concurrency page fetches in flight at once."""
    import asyncio

    inflight = {"now": 0, "peak": 0}
    pages = "".join(
        f"<url><loc>https://dvlfirm.com/p{i}/</loc></url>" for i in range(20)
    )
    sitemap = (
        '<?xml version="1.0"?><urlset '
        'xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">'
        f"{pages}</urlset>"
    )

    async def slow(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("sitemap.xml"):
            return httpx.Response(200, text=sitemap)
        if "/p" in url and url.endswith("/"):
            inflight["now"] += 1
            inflight["peak"] = max(inflight["peak"], inflight["now"])
            await asyncio.sleep(0.01)
            inflight["now"] -= 1
            return httpx.Response(
                200, text=HEALTHY_PAGE, headers={"content-type": "text/html"}
            )
        return httpx.Response(200, headers={"content-type": "text/css"})

    await run_checks(
        settings_for(tmp_path, page_concurrency=3),
        transport=httpx.MockTransport(slow),
    )

    assert inflight["peak"] <= 3


async def test_results_are_persisted(tmp_path):
    database = Database(tmp_path / "run.db")
    run_id = database.start_run()

    run = await run_checks(
        settings_for(tmp_path),
        transport=httpx.MockTransport(handler),
        on_site_complete=lambda result: database.record_site_result(run_id, result),
    )
    database.finish_run(
        run_id,
        status="completed",
        sites_checked=len(run.sites),
        pages_checked=run.pages_checked,
        assets_checked=run.assets_checked,
        broken_assets=run.broken_asset_count,
    )

    rows = database.broken_assets_for_run(run_id)
    assert len(rows) == 1
    assert rows[0]["domain"] == "dvlfirm.com"
    assert rows[0]["page_url"] == PAGE_URL
    assert rows[0]["asset_url"] == BROKEN_CSS
    assert rows[0]["status_code"] == 404
    assert rows[0]["content_type"] == "text/html"

    latest = database.recent_runs(1)[0]
    assert latest["status"] == "completed"
    assert latest["broken_assets"] == 1
    assert latest["pages_checked"] == 2
    database.close()


def test_page_errors_are_stored_separately(tmp_path):
    from site_monitor.crawler import SiteResult
    from site_monitor.elementor import PageResult

    database = Database(tmp_path / "errors.db")
    run_id = database.start_run()
    result = SiteResult(
        domain="a.com",
        sitemap="https://a.com/s.xml",
        pages_found=1,
        pages=[
            PageResult(
                url="https://a.com/gone/",
                status_code=500,
                assets_checked=0,
                broken=(),
                error="page returned HTTP 500",
            )
        ],
    )

    site_run_id = database.record_site_result(run_id, result)

    rows = list(
        database._conn.execute(
            "SELECT * FROM page_errors WHERE site_run_id = ?", (site_run_id,)
        )
    )
    assert len(rows) == 1
    assert rows[0]["error"] == "page returned HTTP 500"
    assert database.broken_assets_for_run(run_id) == []
    database.close()


def test_schema_is_created_and_reopening_is_safe(tmp_path):
    path = tmp_path / "nested" / "dir" / "monitor.db"

    Database(path).close()
    database = Database(path)  # second open must not fail on existing tables

    assert path.exists()
    assert database.recent_runs() == []
    database.close()
