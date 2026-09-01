"""The dashboard: auth, CRUD, and the routes the browser actually calls."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from site_monitor.config import Settings
from site_monitor.db import Database
from site_monitor.webapp import create_app

PASSWORD = "correct horse"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "app.db",
        dashboard_password=PASSWORD,
        session_secret="test-secret",
        retry_backoff=0.0,
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def signed_in(client):
    client.post("/login", data={"password": PASSWORD})
    return client


@pytest.fixture
def database(settings):
    with Database(settings.database_path) as db:
        yield db


# -- auth ---------------------------------------------------------------------


@pytest.mark.parametrize("path", ["/", "/sites", "/runs", "/pagespeed", "/schedules", "/settings"])
def test_pages_require_a_session(client, path):
    response = client.get(path, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/login"


def test_api_returns_401_rather_than_redirecting(client):
    """A polling fetch needs a status code, not an HTML login page."""
    assert client.get("/api/run/status").status_code == 401


def test_healthcheck_is_open(client):
    assert client.get("/healthz").json() == {"ok": True}


def test_wrong_password_is_refused(client):
    response = client.post("/login", data={"password": "nope"})

    assert "not correct" in response.text
    assert client.get("/", follow_redirects=False).status_code == 303


def test_correct_password_grants_a_session(client):
    response = client.post("/login", data={"password": PASSWORD}, follow_redirects=False)

    assert response.status_code == 303
    assert client.get("/").status_code == 200


def test_a_forged_cookie_is_rejected(client):
    client.cookies.set("sm_session", "99999999999.forged")

    assert client.get("/", follow_redirects=False).status_code == 303


def test_logout_ends_the_session(signed_in):
    signed_in.post("/logout")

    assert signed_in.get("/", follow_redirects=False).status_code == 303


# -- site CRUD ----------------------------------------------------------------


def test_add_a_site(signed_in, database):
    response = signed_in.post(
        "/sites/save",
        data={
            "domain": "https://dvlfirm.com/some/path",
            "enabled": "1",
            "pages": "https://dvlfirm.com/\nhttps://dvlfirm.com/about/\nhttps://dvlfirm.com/",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    site = database.list_sites()[0]
    assert site.domain == "dvlfirm.com"          # trimmed from the pasted URL
    assert site.pages == ("https://dvlfirm.com/", "https://dvlfirm.com/about/")


@pytest.mark.parametrize(
    "data, expected",
    [
        ({"domain": "", "pages": "https://a.com/"}, "Enter a domain"),
        ({"domain": "a.com"}, "either a sitemap URL"),
        ({"domain": "a.com", "pages": "a.com/about"}, "not full URLs"),
        ({"domain": "a.com", "sitemap": "ftp://a.com/s.xml"}, "full URL"),
        ({"domain": "a.com", "pages": "https://a.com/", "max_pages": "many"}, "whole number"),
    ],
)
def test_invalid_input_explains_itself(signed_in, data, expected):
    assert expected in signed_in.post("/sites/save", data=data).text


def test_malformed_pages_are_named_rather_than_reported_as_empty(signed_in):
    """Every line rejected must not read as 'you gave us nothing'."""
    response = signed_in.post("/sites/save", data={"domain": "a.com", "pages": "a.com/x"})

    assert "not full URLs" in response.text
    assert "a.com/x" in response.text


def test_renaming_a_site_does_not_leave_the_old_one_behind(signed_in, database):
    signed_in.post("/sites/save", data={"domain": "old.com", "pages": "https://old.com/"})

    signed_in.post(
        "/sites/save",
        data={"domain": "new.com", "pages": "https://new.com/", "original": "old.com"},
    )

    assert [s.domain for s in database.list_sites()] == ["new.com"]


def test_toggle_pauses_and_resumes(signed_in, database):
    signed_in.post("/sites/save", data={"domain": "a.com", "pages": "https://a.com/", "enabled": "1"})

    signed_in.post("/sites/a.com/toggle")
    assert not database.get_site("a.com")["enabled"]

    signed_in.post("/sites/a.com/toggle")
    assert database.get_site("a.com")["enabled"]


def test_delete_removes_the_site(signed_in, database):
    signed_in.post("/sites/save", data={"domain": "a.com", "pages": "https://a.com/"})

    signed_in.post("/sites/a.com/delete")

    assert database.site_count() == 0


def test_editing_an_unknown_site_redirects_instead_of_erroring(signed_in):
    response = signed_in.get("/sites/edit/nope.com", follow_redirects=False)

    assert response.status_code == 303


# -- import -------------------------------------------------------------------


def test_import_adds_sites(signed_in, database):
    body = "sites:\n  - domain: a.com\n    pages:\n      - https://a.com/\n"

    signed_in.post("/import", data={"content": body}, follow_redirects=False)

    assert database.site_count() == 1


def test_import_replace_clears_first(signed_in, database):
    signed_in.post("/sites/save", data={"domain": "old.com", "pages": "https://old.com/"})

    signed_in.post(
        "/import",
        data={
            "content": "sites:\n  - domain: new.com\n    pages:\n      - https://new.com/\n",
            "replace": "1",
        },
    )

    assert [s.domain for s in database.list_sites()] == ["new.com"]


@pytest.mark.parametrize(
    "content, expected",
    [
        ("", "Paste a site list"),
        ("sites:\n  - domain: a.com\n", "needs either"),
        ("sites:\n  - domain: a.com\n    pages:\n      - /relative/\n", "absolute URL"),
        ("{[not yaml", "could not"),
    ],
)
def test_bad_imports_are_rejected_with_a_reason(signed_in, content, expected):
    response = signed_in.post("/import", data={"content": content})

    assert expected.lower() in response.text.lower()


# -- schedules ----------------------------------------------------------------


def test_creating_a_schedule_arms_it(signed_in, database):
    signed_in.post(
        "/schedules/save",
        data={"name": "Every 6h", "kind": "css_check", "cron": "0 */6 * * *", "enabled": "1"},
    )

    row = database.list_schedules()[0]
    assert row["name"] == "Every 6h"
    assert row["next_run_at"] is not None


def test_a_bad_cron_is_rejected_before_it_is_stored(signed_in, database):
    response = signed_in.post(
        "/schedules/save",
        data={"name": "bad", "kind": "css_check", "cron": "99 * * * *"},
        follow_redirects=False,
    )

    assert "error=" in response.headers["location"]
    assert database.list_schedules() == []


def test_schedule_kind_is_constrained(signed_in, database):
    signed_in.post(
        "/schedules/save",
        data={"name": "x", "kind": "rm -rf", "cron": "0 * * * *"},
    )

    assert database.list_schedules()[0]["kind"] == "css_check"


def test_schedules_can_be_paused_and_deleted(signed_in, database):
    signed_in.post(
        "/schedules/save",
        data={"name": "s", "kind": "css_check", "cron": "0 * * * *", "enabled": "1"},
    )
    schedule_id = database.list_schedules()[0]["id"]

    signed_in.post(f"/schedules/{schedule_id}/toggle")
    assert not database.get_schedule(schedule_id)["enabled"]

    signed_in.post(f"/schedules/{schedule_id}/delete")
    assert database.list_schedules() == []


# -- settings -----------------------------------------------------------------


def test_settings_are_saved_and_override_the_environment(signed_in, database):
    signed_in.post(
        "/settings/save",
        data={"telegram_bot_token": "tok", "telegram_chat_id": "-100", "site_concurrency": "2"},
    )

    stored = database.get_settings()
    assert stored["telegram_bot_token"] == "tok"
    assert stored["site_concurrency"] == "2"


def test_settings_reject_a_non_numeric_limit(signed_in, database):
    response = signed_in.post(
        "/settings/save", data={"site_concurrency": "loads"}, follow_redirects=False
    )

    assert "error=" in response.headers["location"]
    assert "site_concurrency" not in database.get_settings()


def test_telegram_test_without_credentials_says_so(signed_in):
    response = signed_in.post("/settings/test-telegram", follow_redirects=False)

    assert "error=" in response.headers["location"]


# -- exports ------------------------------------------------------------------


@pytest.mark.parametrize("report", ["broken", "runs", "pagespeed"])
@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
def test_exports_download(signed_in, report, fmt):
    response = signed_in.get(f"/export/{report}.{fmt}")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert len(response.content) > 0


def test_unknown_export_is_a_404(signed_in):
    assert signed_in.get("/export/secrets.csv").status_code == 404
    assert signed_in.get("/export/runs.pdf").status_code == 404


# -- rendering ----------------------------------------------------------------


@pytest.mark.parametrize(
    "path", ["/", "/sites", "/sites/new", "/runs", "/pagespeed", "/schedules", "/settings", "/import"]
)
def test_every_page_renders(signed_in, path):
    assert signed_in.get(path).status_code == 200


def test_pagespeed_sorting_links_do_not_break_the_page(signed_in):
    for sort in ["performance", "domain", "lcp", "cls", "tbt", "url", "tested_at", "bogus"]:
        assert signed_in.get(f"/pagespeed?sort={sort}&dir=asc").status_code == 200


# -- timestamp display --------------------------------------------------------


def test_timestamps_are_shown_in_the_configured_timezone():
    """Someone who typed 3am must not be shown 07:00 and think it is wrong."""
    from site_monitor.webapp import _local_filter

    render = _local_filter("America/New_York")

    assert render("2026-08-29T07:00:00+00:00") == "29 Aug, 03:00 EDT"
    # The same wall-clock schedule in winter, when the offset differs.
    assert render("2026-01-15T08:00:00+00:00") == "15 Jan, 03:00 EST"


def test_timestamp_filter_degrades_rather_than_raising():
    from site_monitor.webapp import _local_filter

    render = _local_filter("America/New_York")

    assert render(None) == "—"
    assert render("not-a-date") == "not-a-date"
    # A stored value without an offset is UTC, as everything this app writes is.
    assert render("2026-08-29T07:00:00") == "29 Aug, 03:00 EDT"


def test_unknown_timezone_falls_back_to_utc():
    from site_monitor.webapp import _local_filter

    assert _local_filter("Mars/Olympus")("2026-08-29T07:00:00+00:00").endswith("UTC")


# -- login throttling ---------------------------------------------------------


def test_throttle_locks_a_client_after_repeated_failures():
    from site_monitor.webapp.auth import LoginThrottle

    throttle = LoginThrottle(max_attempts=3, window_seconds=900)

    for _ in range(3):
        assert throttle.locked_for("1.2.3.4") == 0
        throttle.record_failure("1.2.3.4")

    assert throttle.locked_for("1.2.3.4") > 0


def test_throttle_is_per_client(_=None):
    from site_monitor.webapp.auth import LoginThrottle

    throttle = LoginThrottle(max_attempts=2)
    throttle.record_failure("1.2.3.4")
    throttle.record_failure("1.2.3.4")

    assert throttle.locked_for("1.2.3.4") > 0
    assert throttle.locked_for("5.6.7.8") == 0


def test_throttle_forgets_attempts_once_the_window_passes():
    from site_monitor.webapp.auth import LoginThrottle

    throttle = LoginThrottle(max_attempts=2, window_seconds=60)
    throttle.record_failure("1.2.3.4", now=1000.0)
    throttle.record_failure("1.2.3.4", now=1000.0)

    assert throttle.locked_for("1.2.3.4", now=1030.0) > 0
    assert throttle.locked_for("1.2.3.4", now=1100.0) == 0


def test_a_successful_sign_in_clears_the_count():
    from site_monitor.webapp.auth import LoginThrottle

    throttle = LoginThrottle(max_attempts=3)
    throttle.record_failure("1.2.3.4")
    throttle.record_failure("1.2.3.4")

    throttle.reset("1.2.3.4")

    assert throttle.locked_for("1.2.3.4") == 0


def test_client_key_prefers_the_forwarded_address():
    from site_monitor.webapp.auth import client_key

    class Req:
        headers = {"x-forwarded-for": "9.9.9.9, 10.0.0.1"}
        client = type("C", (), {"host": "172.17.0.1"})()

    assert client_key(Req()) == "9.9.9.9"


def test_client_key_falls_back_to_the_socket_address():
    from site_monitor.webapp.auth import client_key

    class Req:
        headers = {}
        client = type("C", (), {"host": "172.17.0.1"})()

    assert client_key(Req()) == "172.17.0.1"


def test_repeated_bad_passwords_eventually_lock_the_login(client, monkeypatch):
    """The end-to-end behaviour, not just the throttle in isolation."""
    import site_monitor.webapp as webapp

    monkeypatch.setattr(webapp, "LOGIN_FAILURE_DELAY", 0)

    last = ""
    for _ in range(11):
        last = client.post("/login", data={"password": "guess"}).text

    assert "Too many attempts" in last
    # And the real password is refused too, while the lock holds.
    assert "Too many attempts" in client.post("/login", data={"password": PASSWORD}).text


def test_a_single_site_can_be_checked_from_the_sites_list(signed_in, database, monkeypatch):
    signed_in.post("/sites/save", data={"domain": "a.com", "pages": "https://a.com/"})
    signed_in.post("/sites/save", data={"domain": "b.com", "pages": "https://b.com/"})

    scoped: list = []

    async def fake_trigger(self, *, trigger="manual", only=None):
        scoped.append([s.domain for s in (only or [])])
        return True, "started"

    from site_monitor.runner import RunManager

    monkeypatch.setattr(RunManager, "trigger", fake_trigger)

    response = signed_in.post("/sites/b.com/check", follow_redirects=False)

    assert response.status_code == 303
    assert scoped == [["b.com"]]


def test_checking_an_unknown_site_reports_it(signed_in):
    response = signed_in.post("/sites/nope.com/check", follow_redirects=False)

    assert "error=" in response.headers["location"]


def test_the_sites_list_offers_a_per_site_check_and_speed_test(signed_in):
    signed_in.post("/sites/save", data={"domain": "a.com", "pages": "https://a.com/"})

    listing = signed_in.get("/sites").text
    assert "/sites/a.com/check" in listing
    assert "/sites/a.com/pagespeed" in listing

    editor = signed_in.get("/sites/edit/a.com").text
    assert "Check CSS now" in editor
    assert "Speed test" in editor


# -- returning to where a button was pressed ----------------------------------


def test_run_returns_to_the_page_it_was_pressed_on(signed_in, monkeypatch):
    """From the Sites page you want to watch rows tick off, not be bounced away."""
    from site_monitor.runner import RunManager

    async def fake(self, *, trigger="manual", only=None):
        return True, "started"

    monkeypatch.setattr(RunManager, "trigger", fake)

    response = signed_in.post(
        "/run", headers={"referer": "http://testserver/sites"}, follow_redirects=False
    )

    assert response.headers["location"].startswith("/sites")


def test_a_foreign_referer_is_not_followed(signed_in, monkeypatch):
    """Taking Referer at face value would be an open redirect."""
    from site_monitor.runner import RunManager

    async def fake(self, *, trigger="manual", only=None):
        return True, "started"

    monkeypatch.setattr(RunManager, "trigger", fake)

    response = signed_in.post(
        "/run", headers={"referer": "https://evil.test/phish"}, follow_redirects=False
    )

    assert response.headers["location"].startswith("/?")


def test_run_without_a_referer_falls_back_to_the_overview(signed_in, monkeypatch):
    from site_monitor.runner import RunManager

    async def fake(self, *, trigger="manual", only=None):
        return True, "started"

    monkeypatch.setattr(RunManager, "trigger", fake)

    response = signed_in.post("/run", follow_redirects=False)

    assert response.headers["location"].startswith("/?")


def test_progress_reports_which_sites_are_done(signed_in):
    """The Sites page needs per-site outcomes to tick rows off live."""
    status = signed_in.get("/api/run/status").json()

    assert "done_domains" in status
    assert "broken_domains" in status


# -- the schedule picker ------------------------------------------------------


def test_a_preset_from_the_dropdown_is_saved(signed_in, database):
    signed_in.post(
        "/schedules/save",
        data={"name": "Six hourly", "kind": "css_check", "cron": "0 */6 * * *", "enabled": "1"},
    )

    assert database.list_schedules()[0]["cron"] == "0 */6 * * *"


def test_a_custom_expression_beats_the_dropdown(signed_in, database):
    """Choosing "Custom schedule…" posts a sentinel plus the typed value."""
    signed_in.post(
        "/schedules/save",
        data={
            "name": "Odd one",
            "kind": "css_check",
            "cron": "custom",
            "cron_custom": "15 2 * * 3",
            "enabled": "1",
        },
    )

    assert database.list_schedules()[0]["cron"] == "15 2 * * 3"


def test_choosing_custom_without_typing_one_is_refused(signed_in, database):
    response = signed_in.post(
        "/schedules/save",
        data={"name": "x", "kind": "css_check", "cron": "custom", "cron_custom": ""},
        follow_redirects=False,
    )

    assert "error=" in response.headers["location"]
    assert database.list_schedules() == []


def test_a_bad_custom_expression_is_still_validated(signed_in, database):
    response = signed_in.post(
        "/schedules/save",
        data={"name": "x", "kind": "css_check", "cron": "custom", "cron_custom": "99 * * * *"},
        follow_redirects=False,
    )

    assert "out+of+range" in response.headers["location"].replace("%20", "+")
    assert database.list_schedules() == []


def test_the_form_offers_plain_language_options(signed_in):
    body = signed_in.get("/schedules").text

    assert "Every 6 hours" in body
    assert "Every day at 3:00am" in body
    assert "Custom schedule" in body


# -- report tabs --------------------------------------------------------------


@pytest.fixture
def mixed_runs(database):
    database.start_run(trigger="schedule:Nightly", scope="53 sites")
    database.start_run(trigger="dashboard", scope="53 sites")
    database.start_run(trigger="site:dvlfirm.com", scope="dvlfirm.com")
    database.start_run(trigger="", scope="")  # predates trigger recording
    return database


@pytest.mark.parametrize(
    "source, expected",
    [("all", 4), ("scheduled", 1), ("manual", 2), ("spot", 1)],
)
def test_each_tab_filters_to_its_own_runs(mixed_runs, source, expected):
    assert len(mixed_runs.runs_by_source(source)) == expected


def test_runs_from_before_triggers_were_recorded_land_under_manual(mixed_runs):
    """They have to appear somewhere; manual is the honest place."""
    triggers = [row["trigger"] for row in mixed_runs.runs_by_source("manual")]

    assert "" in triggers


def test_tab_counts_are_reported(mixed_runs):
    counts = mixed_runs.run_source_counts()

    assert counts == {"all": 4, "scheduled": 1, "manual": 2, "spot": 1}


def test_the_tabs_render_with_counts(signed_in, mixed_runs):
    body = signed_in.get("/runs").text

    assert "Spot checks" in body
    assert "/runs?source=scheduled" in body


def test_an_unknown_tab_falls_back_to_all(signed_in, mixed_runs):
    response = signed_in.get("/runs?source=../../etc/passwd")

    assert response.status_code == 200
    assert "All checks" in response.text


def test_a_filtered_tab_shows_only_its_runs(signed_in, mixed_runs):
    body = signed_in.get("/runs?source=spot").text

    assert "dvlfirm.com" in body
    assert "Nightly" not in body


def test_an_empty_tab_says_what_would_fill_it(signed_in, database):
    body = signed_in.get("/runs?source=scheduled").text

    assert "No scheduled checks" in body
    assert "/schedules" in body


def test_the_download_follows_the_selected_tab(signed_in, mixed_runs):
    everything = signed_in.get("/export/runs.csv?source=all").text
    spot_only = signed_in.get("/export/runs.csv?source=spot").text

    assert len(everything.splitlines()) == 5   # header + 4
    assert len(spot_only.splitlines()) == 2    # header + 1


# -- the PageSpeed site filter ------------------------------------------------


@pytest.fixture
def sites_with_partial_pagespeed(database):
    """Four configured sites; only one has ever been tested."""
    from site_monitor.pagespeed import PageSpeedResult

    for name in ("tested.com", "never1.com", "never2.com", "never3.com"):
        database.upsert_site(domain=name, pages=[f"https://{name}/"])
    run_id = database.start_pagespeed_run()
    database.record_pagespeed_result(
        run_id,
        PageSpeedResult(domain="tested.com", url="https://tested.com/",
                        strategy="mobile", performance=61.0),
    )
    return database


def test_the_filter_lists_every_configured_site(signed_in, sites_with_partial_pagespeed):
    """Not just the ones that happen to have results already."""
    body = signed_in.get("/pagespeed").text

    for name in ("tested.com", "never1.com", "never2.com", "never3.com"):
        assert f'value="{name}"' in body


def test_untested_sites_are_labelled_as_such(signed_in, sites_with_partial_pagespeed):
    body = signed_in.get("/pagespeed").text

    assert "not tested yet" in body


def test_coverage_is_stated(signed_in, sites_with_partial_pagespeed):
    body = signed_in.get("/pagespeed").text

    assert "1 of 4 configured sites" in body


def test_a_site_with_results_but_no_longer_configured_stays_reachable(signed_in, database):
    """Its history should not vanish because the site was removed."""
    from site_monitor.pagespeed import PageSpeedResult

    run_id = database.start_pagespeed_run()
    database.record_pagespeed_result(
        run_id,
        PageSpeedResult(domain="removed.com", url="https://removed.com/",
                        strategy="mobile", performance=40.0),
    )

    assert 'value="removed.com"' in signed_in.get("/pagespeed").text


def test_selecting_an_untested_site_explains_the_empty_result(
    signed_in, sites_with_partial_pagespeed
):
    body = signed_in.get("/pagespeed?domain=never1.com").text

    assert "No PageSpeed results for" in body
    assert "never1.com" in body
    assert "Test every site now" in body


def test_a_speed_test_can_be_scoped_to_one_site(signed_in, database, monkeypatch):
    signed_in.post("/sites/save", data={"domain": "a.com", "pages": "https://a.com/"})
    signed_in.post("/sites/save", data={"domain": "b.com", "pages": "https://b.com/"})

    scoped: list = []

    async def fake(self, *, trigger="manual", strategies=None, only=None):
        scoped.append([s.domain for s in (only or [])])
        return True, "started"

    from site_monitor.runner import RunManager

    monkeypatch.setattr(RunManager, "trigger_pagespeed", fake)

    response = signed_in.post("/sites/b.com/pagespeed", follow_redirects=False)

    assert response.status_code == 303
    assert scoped == [["b.com"]]


def test_a_speed_test_on_an_unknown_site_is_reported(signed_in):
    response = signed_in.post("/sites/nope.com/pagespeed", follow_redirects=False)

    assert "error=" in response.headers["location"]


# -- caching in front of the app ----------------------------------------------


@pytest.mark.parametrize("path", ["/", "/sites", "/pagespeed", "/api/run/status"])
def test_nothing_is_cacheable_by_a_cdn(signed_in, path):
    """A cached signed-in page served to the next visitor would be a leak, and
    a cached progress poll makes a running check look stuck."""
    response = signed_in.get(path)

    assert response.headers["cache-control"] == "no-store, private"
    assert "Cookie" in response.headers["vary"]


def test_the_login_page_is_not_cacheable_either(client):
    assert client.get("/login").headers["cache-control"] == "no-store, private"


def test_the_health_check_stays_cacheable(client):
    """An uptime probe should reach the app, not be answered from a cache --
    but it carries nothing private, so it needs no directive of its own."""
    assert "cache-control" not in client.get("/healthz").headers


# -- an unverified site must not read as "all clear" --------------------------


def _record_unverified_run(database) -> int:
    from site_monitor.crawler import SiteResult
    from site_monitor.elementor import PageResult

    database.upsert_site(domain="law.example", pages=["https://law.example/a"])
    run_id = database.start_run()
    database.record_site_result(
        run_id,
        SiteResult(
            domain="law.example",
            pages=[PageResult(url="https://law.example/a", status_code=200,
                              assets_checked=0, broken=())],
            pages_found=1,
            warning="no Elementor stylesheets found on any of 145 pages, "
                    "so nothing was verified",
        ),
    )
    database.finish_run(run_id, sites_checked=1, pages_checked=1,
                        assets_checked=0, broken_assets=0, status="ok")
    return run_id


def test_the_dashboard_will_not_call_an_unverified_site_all_clear(signed_in, database):
    _record_unverified_run(database)

    body = signed_in.get("/").text

    # Nothing IS broken on this site -- the point is that it is called out
    # separately rather than being allowed to pass as a healthy result.
    assert "could not be verified" in body
    assert "law.example" in body
    assert "Unverified" in body


def test_the_run_report_flags_the_site_that_verified_nothing(signed_in, database):
    run_id = _record_unverified_run(database)

    body = signed_in.get(f"/runs/{run_id}").text

    assert "verified nothing" in body
    assert "nothing was verified" in body


def test_a_genuinely_clean_run_still_reads_as_all_clear(signed_in, database):
    from site_monitor.crawler import SiteResult
    from site_monitor.elementor import PageResult

    database.upsert_site(domain="good.example", pages=["https://good.example/a"])
    run_id = database.start_run()
    database.record_site_result(
        run_id,
        SiteResult(
            domain="good.example",
            pages=[PageResult(url="https://good.example/a", status_code=200,
                              assets_checked=12, broken=())],
            pages_found=1,
        ),
    )
    database.finish_run(run_id, sites_checked=1, pages_checked=1,
                        assets_checked=12, broken_assets=0, status="ok")

    body = signed_in.get("/").text

    assert "nothing broken" in body
    assert "could not be verified" not in body


# -- per-site state, not the latest run ---------------------------------------


def _check(database, domain, *, broken=(), assets=10, warning="", trigger="manual"):
    """Record one complete check of one site, as a run would."""
    from site_monitor.crawler import SiteResult
    from site_monitor.elementor import AssetResult, PageResult

    pages = []
    for page_url, asset_url in broken:
        pages.append(
            PageResult(
                url=page_url,
                status_code=200,
                assets_checked=assets,
                broken=(
                    AssetResult(
                        url=asset_url, status_code=404, content_type="text/html",
                        ok=False, reason="HTTP 404", elapsed_ms=5,
                    ),
                ),
            )
        )
    if not pages:
        pages.append(
            PageResult(url=f"https://{domain}/", status_code=200,
                       assets_checked=assets, broken=())
        )

    run_id = database.start_run(trigger=trigger, scope=domain)
    database.record_site_result(
        run_id,
        SiteResult(domain=domain, pages=pages, pages_found=len(pages),
                   warning=warning or None),
    )
    database.finish_run(
        run_id, sites_checked=1, pages_checked=len(pages),
        assets_checked=assets, broken_assets=len(broken), status="ok",
    )
    return run_id


BAD_PAGE = "https://one.example/broken/"
BAD_CSS = "https://one.example/elementor/css/post-1.css?ver=111"


def test_a_spot_check_does_not_erase_every_other_site_from_the_overview(
    signed_in, database
):
    """The bug this covers: the dashboard read state off the latest run.

    A single-site spot check IS the latest run, so every other site silently
    dropped out of "broken right now" and the overview reported them fine
    when they had simply not been looked at.
    """
    database.upsert_site(domain="one.example", pages=[BAD_PAGE])
    database.upsert_site(domain="two.example", pages=["https://two.example/"])

    _check(database, "one.example", broken=[(BAD_PAGE, BAD_CSS)])
    _check(database, "two.example", trigger="site:two.example")  # the spot check

    body = signed_in.get("/").text

    # two.example was the latest run and is clean; one.example must survive it.
    assert "one.example" in body
    assert BAD_CSS in body


def test_the_overview_counts_sites_never_checked(signed_in, database):
    database.upsert_site(domain="seen.example", pages=["https://seen.example/"])
    database.upsert_site(domain="unseen.example", pages=["https://unseen.example/"])
    _check(database, "seen.example")

    body = signed_in.get("/").text

    assert "never been checked" in body
    assert "unseen.example" in body


# -- the site page ------------------------------------------------------------


def test_the_site_page_shows_what_a_recheck_fixed(signed_in, database):
    database.upsert_site(domain="one.example", pages=[BAD_PAGE])
    _check(database, "one.example", broken=[(BAD_PAGE, BAD_CSS)])
    _check(database, "one.example", broken=[(BAD_PAGE, BAD_CSS)])
    _check(database, "one.example")  # fixed

    body = signed_in.get("/sites/one.example").text

    assert "1 fixed" in body
    assert "still" in body  # the middle check was a repeat
    assert "Every Elementor stylesheet resolved correctly" in body


def test_the_site_page_labels_a_newly_broken_stylesheet(signed_in, database):
    database.upsert_site(domain="one.example", pages=[BAD_PAGE])
    _check(database, "one.example")
    _check(database, "one.example", broken=[(BAD_PAGE, BAD_CSS)])

    body = signed_in.get("/sites/one.example").text

    assert "Broken right now" in body
    assert BAD_CSS in body
    assert ">new<" in body


def test_the_site_page_handles_a_site_never_checked(signed_in, database):
    database.upsert_site(domain="fresh.example", pages=["https://fresh.example/"])

    body = signed_in.get("/sites/fresh.example").text

    assert "never been checked" in body
    assert "never checked" in body  # the health badge


def test_an_unknown_site_redirects_rather_than_500ing(signed_in):
    response = signed_in.get("/sites/nope.example", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert "/sites" in response.headers["location"]


def test_sites_new_is_not_swallowed_by_the_domain_route(signed_in):
    """/sites/new must keep matching its own page, not read as a domain."""
    body = signed_in.get("/sites/new").text

    assert "<form" in body
    assert "never been checked" not in body


def test_the_site_page_requires_a_login(client):
    response = client.get("/sites/one.example", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/login"


def test_the_oldest_visible_check_is_still_compared_against_older_history(
    signed_in, database
):
    """A window boundary must not turn an old check into a "first check".

    The page fetches one check beyond the window purely as a comparison base;
    without it the oldest visible row would claim there was nothing before it.
    """
    database.upsert_site(domain="one.example", pages=[BAD_PAGE])
    _check(database, "one.example", broken=[(BAD_PAGE, BAD_CSS)])  # oldest
    _check(database, "one.example", broken=[(BAD_PAGE, BAD_CSS)])
    _check(database, "one.example", broken=[(BAD_PAGE, BAD_CSS)])

    body = signed_in.get("/sites/one.example?limit=2").text

    # Two rows shown, and neither is the site's genuine first check.
    assert "first check" not in body
    assert body.count("still") >= 2


def test_a_genuine_first_check_still_says_so(signed_in, database):
    database.upsert_site(domain="one.example", pages=[BAD_PAGE])
    _check(database, "one.example", broken=[(BAD_PAGE, BAD_CSS)])

    body = signed_in.get("/sites/one.example").text

    assert "first check" in body


def test_the_schedules_page_links_each_schedule_to_the_runs_it_produced(
    signed_in, database
):
    database.upsert_site(domain="one.example", pages=[BAD_PAGE])
    database.create_schedule(
        name="nightly", kind="check", cron="0 3 * * *", enabled=True
    )
    run_id = _check(database, "one.example", trigger="schedule:nightly")

    body = signed_in.get("/schedules").text

    assert f'/runs/{run_id}' in body
    assert "has not fired yet" not in body


def test_a_schedule_that_never_fired_says_so(signed_in, database):
    database.create_schedule(
        name="weekly", kind="check", cron="0 3 * * 1", enabled=True
    )

    body = signed_in.get("/schedules").text

    assert "has not fired yet" in body


def test_the_overview_does_not_list_every_unreachable_page_in_the_fleet(
    signed_in, database
):
    """With 50 sites this table would bury everything below it."""
    from site_monitor.crawler import SiteResult
    from site_monitor.elementor import PageResult

    database.upsert_site(domain="stale.example", pages=["https://stale.example/"])
    run_id = database.start_run(trigger="manual", scope="stale.example")
    database.record_site_result(
        run_id,
        SiteResult(
            domain="stale.example",
            pages_found=30,
            pages=[
                PageResult(url=f"https://stale.example/{n}/", status_code=404,
                           assets_checked=0, broken=(),
                           error="page returned HTTP 404")
                for n in range(30)
            ],
        ),
    )
    database.finish_run(run_id, sites_checked=1, pages_checked=30,
                        assets_checked=0, broken_assets=0, status="ok")

    body = signed_in.get("/").text

    assert "and 18 more" in body
    assert "https://stale.example/29/" not in body
    # The full list is still reachable, on the site's own page.
    assert "https://stale.example/29/" in signed_in.get("/sites/stale.example").text


# -- copying one site's URLs, not the whole page ------------------------------


def _two_broken_sites(database) -> int:
    from site_monitor.crawler import SiteResult
    from site_monitor.elementor import AssetResult, PageResult

    def bad(page, asset):
        return PageResult(
            url=page, status_code=200, assets_checked=15,
            broken=(AssetResult(url=asset, status_code=404,
                                content_type="text/html", ok=False,
                                reason="HTTP 404", elapsed_ms=9),),
        )

    database.upsert_site(domain="alpha.example", pages=["https://alpha.example/one/"])
    database.upsert_site(domain="beta.example", pages=["https://beta.example/only/"])
    run_id = database.start_run(trigger="manual", scope="2 sites")
    database.record_site_result(run_id, SiteResult(
        domain="alpha.example", pages_found=2, pages=[
            bad("https://alpha.example/one/", "https://alpha.example/css/a1.css?ver=1"),
            bad("https://alpha.example/two/", "https://alpha.example/css/a2.css?ver=2")]))
    database.record_site_result(run_id, SiteResult(
        domain="beta.example", pages_found=1, pages=[
            bad("https://beta.example/only/", "https://beta.example/css/b1.css?ver=3")]))
    database.finish_run(run_id, sites_checked=2, pages_checked=3,
                        assets_checked=45, broken_assets=3, status="ok")
    return run_id


def test_each_site_on_a_report_gets_its_own_copy_buttons(signed_in, database):
    run_id = _two_broken_sites(database)

    body = signed_in.get(f"/runs/{run_id}").text

    # One scoping container per site, and a pair of buttons inside each.
    assert body.count("data-copy-group>") == 2
    assert body.count('data-copy="page"') == 3   # two per-site, one page-wide
    assert body.count('data-copy="asset"') == 3


def test_the_page_wide_copy_buttons_stay_outside_every_group(signed_in, database):
    """Scope comes from where a button sits, so position is the contract.

    A page-wide button that drifted inside a .finding would silently start
    copying one site instead of all of them.
    """
    run_id = _two_broken_sites(database)
    body = signed_in.get(f"/runs/{run_id}").text

    header = body[: body.index("data-copy-group>")]

    assert 'data-copy="page"' in header
    assert 'data-copy="asset"' in header
    assert "Copy all page URLs" in header


def test_the_overview_also_groups_copy_buttons_per_site(signed_in, database):
    _two_broken_sites(database)

    body = signed_in.get("/").text

    assert body.count("data-copy-group>") == 2
    assert "Copy all page URLs" in body


def test_a_group_holds_only_its_own_sites_urls(signed_in, database):
    """What the scoping actually has to guarantee, checked in the markup."""
    run_id = _two_broken_sites(database)
    body = signed_in.get(f"/runs/{run_id}").text

    groups = body.split("data-copy-group>")[1:]
    alpha, beta = groups[0], groups[1]

    assert "alpha.example/css/a1.css" in alpha
    assert "beta.example" not in alpha
    assert "beta.example/css/b1.css" in beta
    assert "alpha.example/css" not in beta


# -- the day report -----------------------------------------------------------


def _day_of_checks(database):
    """A morning breakage, partly fixed by the afternoon."""
    from site_monitor.crawler import SiteResult
    from site_monitor.elementor import AssetResult, PageResult

    def bad(page, asset):
        return PageResult(
            url=page, status_code=200, assets_checked=15,
            broken=(AssetResult(url=asset, status_code=404,
                                content_type="text/html", ok=False,
                                reason="HTTP 404", elapsed_ms=9),),
        )

    database.upsert_site(domain="alpha.example", pages=["https://alpha.example/one/"])
    now = datetime.now(timezone.utc)

    for hour_offset, broken in ((2, [("one", "a1"), ("two", "a2")]), (1, [("one", "a1")])):
        at = (now - timedelta(hours=hour_offset)).isoformat(timespec="seconds")
        pages = [bad(f"https://alpha.example/{p}/",
                     f"https://alpha.example/css/{a}.css?ver=1") for p, a in broken]
        run_id = database.start_run(trigger="schedule:nightly", scope="alpha.example")
        database._conn.execute(
            "UPDATE runs SET started_at = ? WHERE id = ?", (at, run_id)
        )
        database._conn.commit()
        database.record_site_result(run_id, SiteResult(
            domain="alpha.example", pages_found=len(pages), pages=pages))
        database.finish_run(run_id, sites_checked=1, pages_checked=len(pages),
                            assets_checked=30, broken_assets=len(broken), status="ok")


def test_the_day_report_shows_each_check_with_its_time(signed_in, database):
    _day_of_checks(database)

    body = signed_in.get("/reports/day").text

    assert "Site check report" in body
    assert "What happened, in order" in body
    assert "2 found" in body
    assert "1 fixed" in body


def test_the_day_report_groups_urls_under_their_site(signed_in, database):
    """Not one flat list -- pages grouped under the site they belong to."""
    _day_of_checks(database)

    body = signed_in.get("/reports/day").text

    assert "alpha.example" in body
    assert "https://alpha.example/two/" in body


# -- how much detail the report carries ---------------------------------------


def test_the_summary_level_carries_no_urls_at_all(signed_in, database):
    """The shortest form: when it happened, which sites, how many pages."""
    _day_of_checks(database)

    body = signed_in.get("/reports/day?detail=summary").text

    assert "alpha.example" in body
    assert "2 found" in body
    assert "https://alpha.example/two/" not in body
    assert "css/a2.css" not in body


def test_the_default_level_lists_pages_but_not_stylesheets(signed_in, database):
    """Stylesheet URLs are noise in a report meant to be read by a person."""
    _day_of_checks(database)

    body = signed_in.get("/reports/day").text

    assert "https://alpha.example/two/" in body
    assert "css/a2.css" not in body


def test_the_full_level_adds_the_stylesheet_behind_each_page(signed_in, database):
    """Kept for whoever has to fix the server, not for the summary reader."""
    _day_of_checks(database)

    body = signed_in.get("/reports/day?detail=full").text

    assert "https://alpha.example/two/" in body
    assert "https://alpha.example/css/a2.css?ver=1" in body
    assert body.index("https://alpha.example/two/") < body.index("css/a2.css")


def test_counts_are_pages_not_stylesheets(signed_in, database):
    """One page can reference several broken stylesheets.

    "How many pages are broken" is the number people ask for; counting rows
    would overstate it.
    """
    from site_monitor.crawler import SiteResult
    from site_monitor.elementor import AssetResult, PageResult

    database.upsert_site(domain="multi.example", pages=["https://multi.example/x/"])
    run_id = database.start_run(trigger="manual", scope="multi.example")
    database.record_site_result(run_id, SiteResult(
        domain="multi.example", pages_found=1,
        pages=[PageResult(
            url="https://multi.example/x/", status_code=200, assets_checked=15,
            broken=tuple(
                AssetResult(url=f"https://multi.example/css/{n}.css", status_code=404,
                            content_type="text/html", ok=False, reason="HTTP 404",
                            elapsed_ms=9)
                for n in range(3)
            ),
        )]))
    database.finish_run(run_id, sites_checked=1, pages_checked=1,
                        assets_checked=15, broken_assets=3, status="ok")

    body = signed_in.get("/reports/day?detail=summary").text

    # Three stylesheets, but only one page.
    assert "1 found" in body
    assert "3 found" not in body


def test_an_unknown_detail_level_falls_back_rather_than_failing(signed_in, database):
    _day_of_checks(database)

    body = signed_in.get("/reports/day?detail=nonsense").text

    assert "https://alpha.example/two/" in body


def test_the_download_name_says_which_version_it_is(signed_in, database):
    _day_of_checks(database)

    for level, marker in (("summary", "-summary.html"), ("urls", ".html"),
                          ("full", "-full.html")):
        response = signed_in.get(f"/reports/day?detail={level}&download=1")
        assert marker in response.headers["content-disposition"], level


def test_the_day_report_downloads_as_a_file(signed_in, database):
    _day_of_checks(database)

    response = signed_in.get("/reports/day?download=1")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "site-check-" in response.headers["content-disposition"]
    # A saved copy must not carry dashboard chrome.
    assert "Sign out" not in response.text
    assert "Download</a>" not in response.text


def test_a_day_with_no_checks_says_so(signed_in, database):
    body = signed_in.get("/reports/day?date=2020-01-01").text

    assert "No checks ran on this day" in body


def test_a_malformed_date_falls_back_to_today(signed_in, database):
    _day_of_checks(database)

    body = signed_in.get("/reports/day?date=not-a-date").text

    assert "What happened, in order" in body


def test_the_day_report_needs_a_login(client):
    response = client.get("/reports/day", follow_redirects=False)

    assert response.status_code in (302, 303, 307)
    assert response.headers["location"] == "/login"


# -- theme --------------------------------------------------------------------


def test_the_dashboard_is_light_unless_dark_is_chosen(signed_in):
    """A dark desktop should not silently impose a dark dashboard."""
    body = signed_in.get("/").text

    assert "@media (prefers-color-scheme" not in body
    assert '[data-theme="dark"]' in body
    assert 'id="theme-toggle"' in body


def test_the_login_page_follows_the_same_stored_choice(client):
    body = client.get("/login").text

    assert "@media (prefers-color-scheme" not in body
    assert "localStorage.getItem('theme')" in body


def test_documents_are_light_whatever_the_reader_uses(signed_in, database):
    """Reports get printed and sent on; they should not follow a machine."""
    _day_of_checks(database)

    body = signed_in.get("/reports/day").text

    assert "@media (prefers-color-scheme" not in body
    assert "data-theme" not in body


def test_the_overview_counts_the_broken_stylesheets_it_lists(signed_in, database):
    """The tile read "1 site broken · 0 broken stylesheets".

    The template used a variable the route never passed, and Jinja renders an
    undefined as empty rather than failing, so the count silently read zero
    while the list below it showed the breakages.
    """
    _two_broken_sites(database)

    body = signed_in.get("/").text

    assert "3 broken stylesheets" in body
    assert "0 broken stylesheets" not in body
