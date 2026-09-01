"""A day's checks, assembled as a report.

The two things that can go quietly wrong here: which runs count as "today",
and what counts as newly found versus already known.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from site_monitor.crawler import SiteResult
from site_monitor.daily import build_day_report, day_bounds, today_in
from site_monitor.db import Database
from site_monitor.elementor import AssetResult, PageResult

PAGE_A = "https://alpha.example/one/"
PAGE_B = "https://alpha.example/two/"
CSS_A = "https://alpha.example/elementor/css/post-1.css?ver=1"
CSS_B = "https://alpha.example/elementor/css/post-2.css?ver=2"


@pytest.fixture
def database(tmp_path):
    with Database(tmp_path / "day.db") as db:
        yield db


def bad(page: str, asset: str) -> PageResult:
    return PageResult(
        url=page, status_code=200, assets_checked=15,
        broken=(AssetResult(url=asset, status_code=404, content_type="text/html",
                            ok=False, reason="HTTP 404", elapsed_ms=9),),
    )


def record(database, *, at: str, broken=(), domain="alpha.example", trigger="schedule:nightly"):
    """Record one check at a specific stored timestamp."""
    pages = [bad(page, asset) for page, asset in broken] or [
        PageResult(url=PAGE_A, status_code=200, assets_checked=15, broken=())
    ]
    run_id = database.start_run(trigger=trigger, scope=domain)
    # start_run stamps "now"; this report is about *when* things happened, so
    # the fixture has to be able to place a check at a chosen time.
    database._conn.execute("UPDATE runs SET started_at = ? WHERE id = ?", (at, run_id))
    database._conn.commit()
    database.record_site_result(
        run_id, SiteResult(domain=domain, pages_found=len(pages), pages=pages)
    )
    database.finish_run(run_id, sites_checked=1, pages_checked=len(pages),
                        assets_checked=15, broken_assets=len(broken), status="ok")
    return run_id


# -- which runs belong to the day ---------------------------------------------


def test_a_day_is_a_calendar_day_in_the_configured_timezone():
    start, end = day_bounds(date(2026, 9, 1), "Asia/Dhaka")

    assert start == "2026-08-31T18:00:00+00:00"
    assert end == "2026-09-01T18:00:00+00:00"


def test_the_window_matches_how_timestamps_are_stored():
    """Compared as text, so the two formats must agree exactly."""
    from site_monitor.db import utcnow

    start, end = day_bounds(datetime.now(timezone.utc).date(), "UTC")

    assert start <= utcnow() < end
    assert len(start) == len(utcnow())


def test_an_unknown_timezone_falls_back_to_utc_rather_than_failing():
    assert day_bounds(date(2026, 9, 1), "Not/AZone") == day_bounds(date(2026, 9, 1), "UTC")


def test_only_that_days_checks_are_included(database):
    record(database, at="2026-08-31T23:59:59+00:00")   # yesterday
    record(database, at="2026-09-01T01:00:00+00:00")   # today
    record(database, at="2026-09-01T23:59:59+00:00")   # today
    record(database, at="2026-09-02T00:00:00+00:00")   # tomorrow

    report = build_day_report(database, date(2026, 9, 1), "UTC")

    assert report.check_count == 2


def test_checks_are_ordered_earliest_first(database):
    record(database, at="2026-09-01T17:00:00+00:00")
    record(database, at="2026-09-01T01:00:00+00:00")
    record(database, at="2026-09-01T09:00:00+00:00")

    report = build_day_report(database, date(2026, 9, 1), "UTC")
    times = [c.run["started_at"][11:16] for c in report.checks]

    assert times == ["01:00", "09:00", "17:00"]


def test_a_day_with_no_checks_is_empty_not_an_error(database):
    report = build_day_report(database, date(2026, 9, 1), "UTC")

    assert report.is_empty
    assert report.check_count == 0
    assert report.outstanding == []


# -- found and fixed ----------------------------------------------------------


def test_the_day_reads_as_a_sequence_of_found_and_fixed(database):
    """The 1am / 5am story: found, then partly fixed, then clean."""
    record(database, at="2026-09-01T01:00:00+00:00",
           broken=[(PAGE_A, CSS_A), (PAGE_B, CSS_B)])
    record(database, at="2026-09-01T05:00:00+00:00", broken=[(PAGE_A, CSS_A)])
    record(database, at="2026-09-01T09:00:00+00:00")

    report = build_day_report(database, date(2026, 9, 1), "UTC")
    first, second, third = report.checks

    assert first.headline == "2 found"
    assert second.headline == "1 fixed, 1 still broken"
    assert third.headline == "1 fixed"

    assert report.found_count == 2
    assert report.fixed_count == 2
    assert report.outstanding == []  # clean by the last check of the day


def test_an_outstanding_breakage_is_not_re_reported_as_found_each_time(database):
    record(database, at="2026-09-01T01:00:00+00:00", broken=[(PAGE_A, CSS_A)])
    record(database, at="2026-09-01T05:00:00+00:00", broken=[(PAGE_A, CSS_A)])
    record(database, at="2026-09-01T09:00:00+00:00", broken=[(PAGE_A, CSS_A)])

    report = build_day_report(database, date(2026, 9, 1), "UTC")

    assert report.found_count == 1  # found once, at 01:00
    assert [c.still_count for c in report.checks] == [0, 1, 1]


def test_a_breakage_carried_over_from_yesterday_is_not_found_today(database):
    """Comparison crosses midnight deliberately.

    Resetting at midnight would report every outstanding breakage as
    freshly found each morning, which is both wrong and alarming.
    """
    record(database, at="2026-08-31T22:00:00+00:00", broken=[(PAGE_A, CSS_A)])
    record(database, at="2026-09-01T01:00:00+00:00", broken=[(PAGE_A, CSS_A)])

    report = build_day_report(database, date(2026, 9, 1), "UTC")

    assert report.found_count == 0
    assert report.checks[0].still_count == 1


def test_a_regenerated_stylesheet_counts_as_a_new_breakage(database):
    record(database, at="2026-09-01T01:00:00+00:00", broken=[(PAGE_A, CSS_A)])
    record(database, at="2026-09-01T05:00:00+00:00",
           broken=[(PAGE_A, "https://alpha.example/elementor/css/post-1.css?ver=99")])

    report = build_day_report(database, date(2026, 9, 1), "UTC")

    assert report.checks[1].found_count == 1
    assert report.checks[1].fixed_count == 1


# -- what makes the cut -------------------------------------------------------


def test_healthy_sites_are_left_out_of_a_check(database):
    """Fifty clean rows would bury the one that matters."""
    record(database, at="2026-09-01T01:00:00+00:00")

    report = build_day_report(database, date(2026, 9, 1), "UTC")

    assert report.checks[0].sites  # the site was checked
    assert report.checks[0].noteworthy == []  # but has nothing to report
    assert report.checks[0].headline == "nothing broken"


def test_an_unverified_site_is_noteworthy_even_with_nothing_broken(database):
    run_id = database.start_run(trigger="manual", scope="law.example")
    database._conn.execute(
        "UPDATE runs SET started_at = ? WHERE id = ?",
        ("2026-09-01T01:00:00+00:00", run_id),
    )
    database._conn.commit()
    database.record_site_result(
        run_id,
        SiteResult(
            domain="law.example", pages_found=1,
            pages=[PageResult(url="https://law.example/", status_code=200,
                              assets_checked=0, broken=())],
            warning="no Elementor stylesheets found on any of 1 pages",
        ),
    )
    database.finish_run(run_id, sites_checked=1, pages_checked=1,
                        assets_checked=0, broken_assets=0, status="ok")

    report = build_day_report(database, date(2026, 9, 1), "UTC")

    assert len(report.checks[0].noteworthy) == 1
    assert report.outstanding[0].domain == "law.example"


def test_outstanding_reflects_each_sites_last_check_of_the_day(database):
    record(database, at="2026-09-01T01:00:00+00:00", broken=[(PAGE_A, CSS_A)])
    record(database, at="2026-09-01T05:00:00+00:00", broken=[(PAGE_B, CSS_B)],
           domain="beta.example")

    report = build_day_report(database, date(2026, 9, 1), "UTC")
    domains = sorted(site.domain for site in report.outstanding)

    assert domains == ["alpha.example", "beta.example"]
    assert report.sites_affected == 2


def test_multiple_sites_in_one_check_are_reported_separately(database):
    run_id = database.start_run(trigger="schedule:nightly", scope="2 sites")
    database._conn.execute(
        "UPDATE runs SET started_at = ? WHERE id = ?",
        ("2026-09-01T01:00:00+00:00", run_id),
    )
    database._conn.commit()
    database.record_site_result(run_id, SiteResult(
        domain="alpha.example", pages_found=1, pages=[bad(PAGE_A, CSS_A)]))
    database.record_site_result(run_id, SiteResult(
        domain="beta.example", pages_found=1, pages=[bad(PAGE_B, CSS_B)]))
    database.finish_run(run_id, sites_checked=2, pages_checked=2,
                        assets_checked=30, broken_assets=2, status="ok")

    report = build_day_report(database, date(2026, 9, 1), "UTC")

    assert len(report.checks[0].noteworthy) == 2
    assert report.sites_touched == 2
    assert report.found_count == 2


def test_today_follows_the_configured_timezone():
    utc_today = today_in("UTC")
    dhaka_today = today_in("Asia/Dhaka")

    # Dhaka is ahead of UTC, so it is either the same day or one ahead.
    assert dhaka_today - utc_today in (timedelta(0), timedelta(days=1))
