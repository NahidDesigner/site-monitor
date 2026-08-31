"""PageSpeed Insights client and sweep."""

from __future__ import annotations

import httpx

from site_monitor.config import Settings, Site
from site_monitor.db import Database
from site_monitor.pagespeed import parse_response, run_pagespeed, targets_for

REPORT = {
    "lighthouseResult": {
        "categories": {"performance": {"score": 0.87}},
        "audits": {
            "first-contentful-paint": {"numericValue": 1200.0},
            "largest-contentful-paint": {"numericValue": 2450.7},
            "cumulative-layout-shift": {"numericValue": 0.021},
            "total-blocking-time": {"numericValue": 310.0},
            "speed-index": {"numericValue": 3100.0},
            "interactive": {"numericValue": 4200.0},
        },
    }
}


def test_score_is_converted_to_a_percentage():
    result = parse_response("a.com", "https://a.com/", "mobile", REPORT)

    assert result.performance == 87.0
    assert result.lcp_ms == 2450.7
    assert result.cls == 0.021
    assert result.ok


def test_a_report_without_a_score_is_an_error_not_a_zero():
    """A failed Lighthouse run must not look like a site scoring 0."""
    result = parse_response(
        "a.com", "https://a.com/", "mobile",
        {"error": {"message": "Lighthouse returned error: NO_FCP"}},
    )

    assert result.performance is None
    assert not result.ok
    assert "NO_FCP" in result.error


def test_targets_use_the_first_curated_page_then_fall_back_to_the_root():
    targets = targets_for(
        [
            Site(domain="a.com", pages=("https://a.com/landing/", "https://a.com/x/")),
            Site(domain="b.com", sitemap="https://b.com/sitemap.xml"),
        ]
    )

    assert targets == [
        ("a.com", "https://a.com/landing/"),
        ("b.com", "https://b.com/"),
    ]


async def test_sweep_records_a_result_per_site_and_strategy(tmp_path):
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.params.get("strategy"))
        return httpx.Response(200, json=REPORT)

    settings = Settings(pagespeed_api_key="key", max_retries=1, request_timeout=5)
    with Database(tmp_path / "ps.db") as database:
        run_id, tested, failures = await run_pagespeed(
            settings,
            database,
            [Site(domain="a.com", pages=("https://a.com/",))],
            strategies=("mobile", "desktop"),
            transport=httpx.MockTransport(handler),
        )
        rows = database.pagespeed_results(run_id=run_id)
        run = database.latest_pagespeed_run()

    assert tested == 2 and failures == 0
    assert sorted(seen) == ["desktop", "mobile"]
    assert {row["strategy"] for row in rows} == {"mobile", "desktop"}
    assert all(row["performance"] == 87.0 for row in rows)
    assert run["status"] == "completed"


async def test_api_errors_are_recorded_against_the_url_not_raised(tmp_path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "Quota exceeded"}})

    settings = Settings(max_retries=1, request_timeout=5)
    with Database(tmp_path / "ps.db") as database:
        run_id, tested, failures = await run_pagespeed(
            settings,
            database,
            [Site(domain="a.com", pages=("https://a.com/",))],
            strategies=("mobile",),
            transport=httpx.MockTransport(handler),
        )
        rows = database.pagespeed_results(run_id=run_id)

    assert tested == 1 and failures == 1
    assert "Quota exceeded" in rows[0]["error"]
    assert rows[0]["performance"] is None


async def test_api_key_is_sent_when_configured(tmp_path):
    keys: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        keys.append(request.url.params.get("key"))
        return httpx.Response(200, json=REPORT)

    with Database(tmp_path / "ps.db") as database:
        await run_pagespeed(
            Settings(pagespeed_api_key="secret-key", max_retries=1),
            database,
            [Site(domain="a.com", pages=("https://a.com/",))],
            strategies=("mobile",),
            transport=httpx.MockTransport(handler),
        )

    assert keys == ["secret-key"]


async def test_sweep_with_no_sites_completes_cleanly(tmp_path):
    with Database(tmp_path / "ps.db") as database:
        run_id, tested, failures = await run_pagespeed(
            Settings(), database, [], transport=httpx.MockTransport(lambda r: httpx.Response(200))
        )
        run = database.latest_pagespeed_run()

    assert (tested, failures) == (0, 0)
    assert run["status"] == "completed"


def test_results_sort_puts_failures_last_not_first(tmp_path):
    """Sorting by worst performance must not surface untested URLs first."""
    from site_monitor.pagespeed import PageSpeedResult

    with Database(tmp_path / "ps.db") as database:
        run_id = database.start_pagespeed_run()
        database.record_pagespeed_result(
            run_id, PageSpeedResult("a.com", "https://a.com/", "mobile", performance=42.0)
        )
        database.record_pagespeed_result(
            run_id, PageSpeedResult("b.com", "https://b.com/", "mobile", error="boom")
        )
        rows = database.pagespeed_results(sort="performance", direction="asc")

    assert [row["domain"] for row in rows] == ["a.com", "b.com"]


def test_sort_key_is_whitelisted_against_injection(tmp_path):
    with Database(tmp_path / "ps.db") as database:
        # A bogus sort key falls back to the default instead of reaching SQL.
        assert database.pagespeed_results(sort="url; DROP TABLE runs--") == []
        assert database.recent_runs() == []


# -- shareable report links ---------------------------------------------------


def test_a_result_carries_a_shareable_rerun_link():
    from site_monitor.pagespeed import rerun_link

    result = parse_response("a.com", "https://a.com/page/?x=1", "mobile", REPORT)

    assert result.report_url == rerun_link("https://a.com/page/?x=1", "mobile")
    assert "pagespeed.web.dev" in result.report_url
    # The URL must survive being embedded as a query parameter.
    assert "https%3A%2F%2Fa.com%2Fpage%2F%3Fx%3D1" in result.report_url
    assert "form_factor=mobile" in result.report_url


def test_the_link_reflects_the_device_tested():
    from site_monitor.pagespeed import rerun_link

    assert rerun_link("https://a.com/", "desktop").endswith("form_factor=desktop")
    # Anything unrecognised falls back to mobile rather than an invalid link.
    assert rerun_link("https://a.com/", "watch").endswith("form_factor=mobile")


def test_no_link_without_a_url():
    from site_monitor.pagespeed import rerun_link

    assert rerun_link("", "mobile") == ""


# -- pairing mobile with desktop ----------------------------------------------


def _record(database, run_id, domain, url, strategy, score):
    from site_monitor.pagespeed import PageSpeedResult

    database.record_pagespeed_result(
        run_id,
        PageSpeedResult(domain=domain, url=url, strategy=strategy,
                        performance=score, lcp_ms=2000.0, cls=0.05, tbt_ms=100.0),
    )


def test_both_devices_pair_onto_one_row(tmp_path):
    with Database(tmp_path / "p.db") as database:
        run_id = database.start_pagespeed_run()
        _record(database, run_id, "a.com", "https://a.com/", "mobile", 56.0)
        _record(database, run_id, "a.com", "https://a.com/", "desktop", 88.0)

        pairs = database.pagespeed_pairs()

    assert len(pairs) == 1
    assert pairs[0]["mobile"]["performance"] == 56.0
    assert pairs[0]["desktop"]["performance"] == 88.0


def test_a_page_tested_on_one_device_still_appears_with_the_gap_visible(tmp_path):
    """A half-finished sweep must not hide the pages it never got to."""
    with Database(tmp_path / "p.db") as database:
        run_id = database.start_pagespeed_run()
        _record(database, run_id, "a.com", "https://a.com/", "mobile", 56.0)

        pairs = database.pagespeed_pairs()

    assert pairs[0]["mobile"] is not None
    assert pairs[0]["desktop"] is None


def test_pairs_keep_the_newest_result_for_each_device(tmp_path):
    with Database(tmp_path / "p.db") as database:
        old = database.start_pagespeed_run()
        _record(database, old, "a.com", "https://a.com/", "mobile", 40.0)
        new = database.start_pagespeed_run()
        _record(database, new, "a.com", "https://a.com/", "mobile", 72.0)

        pairs = database.pagespeed_pairs()

    assert len(pairs) == 1
    assert pairs[0]["mobile"]["performance"] == 72.0


def test_pairs_can_be_filtered_to_one_run(tmp_path):
    with Database(tmp_path / "p.db") as database:
        first = database.start_pagespeed_run()
        _record(database, first, "a.com", "https://a.com/", "mobile", 40.0)
        second = database.start_pagespeed_run()
        _record(database, second, "b.com", "https://b.com/", "mobile", 70.0)

        assert [p["domain"] for p in database.pagespeed_pairs(run_id=first)] == ["a.com"]


def test_pairs_sort_by_mobile_score_worst_first(tmp_path):
    with Database(tmp_path / "p.db") as database:
        run_id = database.start_pagespeed_run()
        _record(database, run_id, "fast.com", "https://fast.com/", "mobile", 95.0)
        _record(database, run_id, "slow.com", "https://slow.com/", "mobile", 22.0)

        pairs = database.pagespeed_pairs(sort="performance", direction="asc")

    assert [p["domain"] for p in pairs] == ["slow.com", "fast.com"]
