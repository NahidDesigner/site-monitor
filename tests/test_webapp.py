"""The dashboard: auth, CRUD, and the routes the browser actually calls."""

from __future__ import annotations

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
