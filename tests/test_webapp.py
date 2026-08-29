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
