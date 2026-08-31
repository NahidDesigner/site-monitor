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


# -- explicit page lists ------------------------------------------------------


async def test_explicit_pages_are_checked_without_touching_a_sitemap(tmp_path):
    """A curated list is exact: nothing is fetched to resolve it."""
    requested: list[str] = []

    def tracking(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return handler(request)

    settings = settings_for(
        tmp_path,
        sites=(
            Site(
                domain="dvlfirm.com",
                pages=(PAGE_URL, "https://dvlfirm.com/healthy/"),
            ),
        ),
    )

    run = await run_checks(settings, transport=httpx.MockTransport(tracking))

    assert run.sites[0].pages_found == 2
    assert run.sites[0].broken_asset_count == 1
    assert not any("sitemap" in url for url in requested)


async def test_explicit_pages_win_over_a_sitemap_when_both_are_given(tmp_path):
    settings = settings_for(
        tmp_path,
        sites=(
            Site(
                domain="dvlfirm.com",
                sitemap="https://dvlfirm.com/sitemap.xml",
                pages=(PAGE_URL,),
            ),
        ),
    )

    run = await run_checks(settings, transport=httpx.MockTransport(handler))

    # The sitemap lists two pages; the explicit list names one.
    assert run.sites[0].pages_found == 1


async def test_max_pages_also_caps_an_explicit_list(tmp_path):
    settings = settings_for(
        tmp_path,
        max_pages_per_site=1,
        sites=(
            Site(
                domain="dvlfirm.com",
                pages=("https://dvlfirm.com/healthy/", PAGE_URL),
            ),
        ),
    )

    run = await run_checks(settings, transport=httpx.MockTransport(handler))

    assert run.sites[0].pages_checked == 1
    assert run.sites[0].broken_asset_count == 0  # the healthy page came first


async def test_site_with_an_empty_page_list_reports_an_error(tmp_path):
    settings = settings_for(
        tmp_path, sites=(Site(domain="dvlfirm.com", pages=()),)
    )
    # An empty list with no sitemap cannot happen via load_sites, but the
    # crawler must still degrade rather than crash.

    run = await run_checks(settings, transport=httpx.MockTransport(handler))

    assert run.sites[0].error


# --- a pass that verified nothing must not read as healthy -------------------

# Real cause seen in the wild: an optimisation plugin combines every stylesheet
# into its own cache directory, so nothing matches `elementor/css/` any more.
PAGE_WITHOUT_ELEMENTOR = (
    "<html><head>"
    '<link rel="stylesheet" href="/wp-content/cache/min/1/combined.css?ver=9">'
    "</head><body>hello</body></html>"
)


def plain_pages(request: httpx.Request) -> httpx.Response:
    """Every page loads fine and references no Elementor stylesheet."""
    return httpx.Response(
        200, text=PAGE_WITHOUT_ELEMENTOR, headers={"content-type": "text/html"}
    )


def site_with_plain_pages(count: int = 5) -> Settings:
    return Settings(
        sites=[
            Site(
                domain="law.example",
                pages=[f"https://law.example/{n}/" for n in range(count)],
            )
        ]
    )


async def test_a_site_with_no_elementor_stylesheets_is_flagged_not_passed():
    run = await run_checks(
        site_with_plain_pages(), transport=httpx.MockTransport(plain_pages)
    )
    result = run.sites[0]

    # It looks clean by every count the report carries...
    assert result.pages_checked == 5
    assert result.assets_checked == 0
    assert result.broken_asset_count == 0
    assert result.error is None

    # ...which is exactly why it has to be called out instead of passed.
    assert result.warning is not None
    assert "no Elementor stylesheets" in result.warning
    assert result.pages_without_assets == 5
    assert result.has_findings
    assert run.has_findings


async def test_unreachable_pages_are_not_reported_as_a_blind_spot():
    """A site that failed to load is already reported; don't double-report it."""

    def down(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom", headers={"content-type": "text/html"})

    settings = Settings(sites=[Site(domain="down.example", pages=["https://down.example/a"])])
    run = await run_checks(settings, transport=httpx.MockTransport(down))
    result = run.sites[0]

    assert result.warning is None
    assert result.broken_pages  # the page error is the finding
    assert result.has_findings


async def test_a_healthy_site_carries_no_warning(tmp_path):
    run = await run_checks(
        settings_for(tmp_path), transport=httpx.MockTransport(handler)
    )
    result = run.sites[0]

    assert result.assets_checked
    assert result.warning is None


async def test_the_warning_survives_a_round_trip_through_the_database(tmp_path):
    run = await run_checks(
        site_with_plain_pages(1), transport=httpx.MockTransport(plain_pages)
    )

    with Database(tmp_path / "monitor.db") as database:
        run_id = database.start_run()
        database.record_site_result(run_id, run.sites[0])
        rows = database.site_runs_for_run(run_id)

    assert len(rows) == 1
    assert "no Elementor stylesheets" in rows[0]["warning"]
