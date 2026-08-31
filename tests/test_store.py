"""The site list, settings and schedules as stored in SQLite."""

from __future__ import annotations

import pytest

from site_monitor.config import Settings, Site, apply_overrides
from site_monitor.db import Database
from site_monitor.runner import resolve_sites


@pytest.fixture
def database(tmp_path):
    with Database(tmp_path / "test.db") as db:
        yield db


# -- sites --------------------------------------------------------------------


def test_upsert_and_read_back(database):
    database.upsert_site(
        domain="dvlfirm.com",
        pages=["https://dvlfirm.com/", "https://dvlfirm.com/about/"],
    )

    site = database.list_sites()[0]

    assert site.domain == "dvlfirm.com"
    assert site.pages == ("https://dvlfirm.com/", "https://dvlfirm.com/about/")
    assert site.enabled


def test_pages_keep_their_order_and_lose_duplicates(database):
    database.upsert_site(
        domain="a.com",
        pages=["https://a.com/z/", "https://a.com/a/", "https://a.com/z/"],
    )

    assert database.list_sites()[0].pages == ("https://a.com/z/", "https://a.com/a/")


def test_upsert_replaces_the_page_list_rather_than_merging(database):
    database.upsert_site(domain="a.com", pages=["https://a.com/old/"])

    database.upsert_site(domain="a.com", pages=["https://a.com/new/"])

    assert database.list_sites()[0].pages == ("https://a.com/new/",)


def test_enabled_only_filter(database):
    database.upsert_site(domain="on.com", pages=["https://on.com/"])
    database.upsert_site(domain="off.com", pages=["https://off.com/"], enabled=False)

    assert [s.domain for s in database.list_sites(enabled_only=True)] == ["on.com"]


def test_delete_removes_pages_too(database):
    database.upsert_site(domain="a.com", pages=["https://a.com/"])
    site_id = database.get_site("a.com")["id"]

    assert database.delete_site("a.com")
    assert database.site_pages(site_id) == []
    assert not database.delete_site("a.com")  # second delete is a no-op


def test_import_sites_merges_by_default_and_replaces_on_request(database):
    database.import_sites([Site(domain="a.com", pages=("https://a.com/",))])

    database.import_sites([Site(domain="b.com", pages=("https://b.com/",))])
    assert database.site_count() == 2

    database.import_sites(
        [Site(domain="c.com", pages=("https://c.com/",))], replace=True
    )
    assert [s.domain for s in database.list_sites()] == ["c.com"]


# -- resolve_sites ------------------------------------------------------------


def test_resolve_prefers_the_database(database, tmp_path):
    database.upsert_site(domain="db.com", pages=["https://db.com/"])
    settings = Settings(sites=(Site(domain="yaml.com", pages=("https://yaml.com/",)),))

    assert [s.domain for s in resolve_sites(settings, database)] == ["db.com"]


def test_resolve_imports_a_yaml_list_once_when_the_database_is_empty(database):
    settings = Settings(sites=(Site(domain="yaml.com", pages=("https://yaml.com/",)),))

    resolved = resolve_sites(settings, database)

    assert [s.domain for s in resolved] == ["yaml.com"]
    # Imported, not just read through -- a later call needs no file.
    assert database.site_count() == 1
    assert [s.domain for s in resolve_sites(Settings(), database)] == ["yaml.com"]


def test_resolve_does_not_reimport_after_every_site_is_disabled(database):
    """A deliberately paused fleet must not be silently repopulated."""
    database.upsert_site(domain="paused.com", pages=["https://paused.com/"], enabled=False)
    settings = Settings(sites=(Site(domain="yaml.com", pages=("https://yaml.com/",)),))

    assert resolve_sites(settings, database) == []
    assert database.site_count() == 1


def test_resolve_with_nothing_anywhere(database):
    assert resolve_sites(Settings(), database) == []


# -- settings -----------------------------------------------------------------


def test_only_whitelisted_settings_are_writable(database):
    database.set_settings({"telegram_bot_token": "abc", "database_path": "/etc/evil"})

    stored = database.get_settings()
    assert stored == {"telegram_bot_token": "abc"}


def test_empty_value_clears_an_override(database):
    database.set_settings({"telegram_bot_token": "abc"})

    database.set_settings({"telegram_bot_token": ""})

    assert "telegram_bot_token" not in database.get_settings()


def test_overrides_layer_over_the_environment():
    base = Settings(telegram_bot_token="from-env", site_concurrency=3)

    merged = apply_overrides(base, {"telegram_bot_token": "from-db", "site_concurrency": "9"})

    assert merged.telegram_bot_token == "from-db"
    assert merged.site_concurrency == 9


def test_a_malformed_override_is_ignored_rather_than_crashing_the_run():
    base = Settings(page_concurrency=8)

    merged = apply_overrides(base, {"page_concurrency": "lots"})

    assert merged.page_concurrency == 8


def test_pagespeed_strategies_are_validated():
    base = Settings()

    assert apply_overrides(base, {"pagespeed_strategies": "mobile"}).pagespeed_strategies == ("mobile",)
    # An unrecognised device leaves the default alone rather than emptying it.
    assert apply_overrides(base, {"pagespeed_strategies": "watch"}).pagespeed_strategies == (
        "mobile",
        "desktop",
    )


# -- schedules ----------------------------------------------------------------


def test_schedule_lifecycle(database):
    schedule_id = database.create_schedule(
        name="Every 6h", kind="css_check", cron="0 */6 * * *"
    )

    row = database.get_schedule(schedule_id)
    assert row["name"] == "Every 6h" and row["enabled"] == 1

    database.update_schedule(
        schedule_id, name="Nightly", kind="pagespeed", cron="0 3 * * *", enabled=False
    )
    row = database.get_schedule(schedule_id)
    assert (row["name"], row["kind"], row["enabled"]) == ("Nightly", "pagespeed", 0)

    assert database.list_schedules(enabled_only=True) == []
    assert database.delete_schedule(schedule_id)


def test_marking_a_run_records_status_and_next_time(database):
    schedule_id = database.create_schedule(name="s", kind="css_check", cron="* * * * *")

    database.mark_schedule_run(schedule_id, status="started", next_run_at="2026-09-01T00:00:00+00:00")

    row = database.get_schedule(schedule_id)
    assert row["last_status"] == "started"
    assert row["next_run_at"] == "2026-09-01T00:00:00+00:00"
    assert row["last_run_at"] is not None


# -- runs orphaned by a restart -----------------------------------------------


def test_runs_left_in_flight_by_a_restart_are_closed_out(database):
    """Otherwise they sit in Reports forever claiming to be in progress."""
    run_id = database.start_run()
    ps_id = database.start_pagespeed_run()

    closed = database.close_orphaned_runs()

    assert closed == 2
    assert database.run(run_id)["status"] == "interrupted"
    assert database.run(run_id)["finished_at"] is not None
    assert "restarted" in database.run(run_id)["error"]
    assert database.pagespeed_runs(1)[0]["status"] == "interrupted"


def test_completed_runs_are_left_alone(database):
    run_id = database.start_run()
    database.finish_run(
        run_id, status="completed", sites_checked=1, pages_checked=2,
        assets_checked=3, broken_assets=0,
    )

    assert database.close_orphaned_runs() == 0
    assert database.run(run_id)["status"] == "completed"


def test_closing_orphans_is_safe_when_there_are_none(database):
    assert database.close_orphaned_runs() == 0


# -- what started a run -------------------------------------------------------


def test_a_run_records_what_triggered_it_and_what_it_covered(database):
    run_id = database.start_run(trigger="schedule:Every 6h", scope="53 sites")

    row = database.run(run_id)
    assert row["trigger"] == "schedule:Every 6h"
    assert row["scope"] == "53 sites"


def test_a_pagespeed_run_records_its_trigger_and_expected_count(database):
    run_id = database.start_pagespeed_run(trigger="dashboard", expected=106)

    row = database.pagespeed_runs(1)[0]
    assert row["trigger"] == "dashboard"
    assert row["expected"] == 106


def test_columns_are_added_to_a_database_created_before_they_existed(tmp_path):
    """Anyone already running this must not lose their history to an upgrade."""
    import sqlite3

    path = tmp_path / "old.db"
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
            finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
            sites_checked INTEGER NOT NULL DEFAULT 0,
            pages_checked INTEGER NOT NULL DEFAULT 0,
            assets_checked INTEGER NOT NULL DEFAULT 0,
            broken_assets INTEGER NOT NULL DEFAULT 0, error TEXT);
        INSERT INTO runs (started_at, status, pages_checked)
        VALUES ('2026-08-30T23:00:00+00:00', 'completed', 45);
        """
    )
    con.commit()
    con.close()

    with Database(path) as db:
        rows = db.recent_runs(5)
        assert len(rows) == 1
        assert rows[0]["pages_checked"] == 45      # history intact
        assert rows[0]["trigger"] == ""            # column added, empty default
        db.start_run(trigger="dashboard", scope="1 site")

    # Reopening must not try to add the columns twice.
    with Database(path) as db:
        assert len(db.recent_runs(5)) == 2
