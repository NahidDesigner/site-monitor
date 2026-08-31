"""Shared PageSpeed reports: what a client can open, and what they cannot.

Google's PageSpeed API is stateless and mints no permalink for a run made
through it, so proof of a specific measurement has to be served from here.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from site_monitor.config import Settings
from site_monitor.db import Database, unpack_report
from site_monitor.pagespeed import PageSpeedResult, parse_response
from site_monitor.webapp import create_app

PASSWORD = "correct horse"

LIGHTHOUSE = {
    "requestedUrl": "https://client.example/",
    "fetchTime": "2026-08-31T09:00:00.000Z",
    "categories": {"performance": {"score": 0.62}},
    "audits": {
        "largest-contentful-paint": {"numericValue": 7480.0},
        "cumulative-layout-shift": {"numericValue": 0.117},
        "total-blocking-time": {"numericValue": 22.0},
        "first-contentful-paint": {"numericValue": 5510.0},
        "speed-index": {"numericValue": 6100.0},
    },
}


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "share.db",
        dashboard_password=PASSWORD,
        session_secret="test-secret",
        retry_backoff=0.0,
    )


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.fixture
def database(settings):
    with Database(settings.database_path) as db:
        yield db


def store(database, *, score=0.62, strategies=("mobile", "desktop")) -> dict:
    """Record one PageSpeed run and hand back the share token per device."""
    # The real API envelope: the Lighthouse report sits under lighthouseResult.
    payload = {
        "id": "https://client.example/",
        "analysisUTCTimestamp": "2026-08-31T09:00:00.000Z",
        "lighthouseResult": {
            **LIGHTHOUSE,
            "categories": {"performance": {"score": score}},
        },
    }
    run_id = database.start_pagespeed_run(trigger="manual")
    for strategy in strategies:
        database.record_pagespeed_result(
            run_id,
            parse_response("client.example", "https://client.example/", strategy, payload),
        )
    database.finish_pagespeed_run(run_id, status="ok", urls_tested=len(strategies), failures=0)

    return {
        row["strategy"]: row["share_token"]
        for row in database.pagespeed_results(run_id=run_id)
    }


# -- what a client sees -------------------------------------------------------


def test_a_shared_report_opens_without_an_account(client, database):
    """The whole point: a client has no login here."""
    token = store(database)["mobile"]

    response = client.get(f"/share/speed/{token}", follow_redirects=False)

    assert response.status_code == 200
    assert "client.example" in response.text


def test_the_shared_report_shows_when_it_was_measured(client, database):
    token = store(database)["mobile"]

    body = client.get(f"/share/speed/{token}").text

    assert "August 2026" in body
    assert "record of that test, not a live measurement" in body


def test_the_shared_report_shows_both_devices_from_one_link(client, database):
    """A client asks how fast the site is, not how fast on mobile."""
    token = store(database)["mobile"]

    body = client.get(f"/share/speed/{token}").text

    assert "Mobile performance" in body
    assert "Desktop performance" in body
    assert "62" in body


def test_a_single_device_run_still_shares(client, database):
    token = store(database, strategies=("mobile",))["mobile"]

    body = client.get(f"/share/speed/{token}").text

    assert "Mobile performance" in body


def test_an_unknown_token_is_a_plain_404(client):
    response = client.get("/share/speed/not-a-real-token")

    assert response.status_code == 404


# -- the part that makes it checkable ----------------------------------------


def test_the_raw_google_report_can_be_downloaded(client, database):
    """Google's own viewer renders this JSON back into the official report."""
    token = store(database)["mobile"]

    response = client.get(f"/share/speed/{token}/lighthouse.json")

    assert response.status_code == 200
    assert "attachment" in response.headers["content-disposition"]
    assert "client.example" in response.headers["content-disposition"]
    assert response.json()["categories"]["performance"]["score"] == 0.62
    assert response.json()["fetchTime"] == "2026-08-31T09:00:00.000Z"


def test_the_stored_report_survives_the_round_trip(database):
    store(database)
    row = database.pagespeed_results(limit=1)[0]

    assert unpack_report(row["report_json"]) == {
        **LIGHTHOUSE,
        "categories": {"performance": {"score": 0.62}},
    }


def test_a_result_without_a_stored_report_says_so_rather_than_500ing(
    client, database
):
    run_id = database.start_pagespeed_run(trigger="manual")
    database.record_pagespeed_result(
        run_id,
        PageSpeedResult(domain="a.example", url="https://a.example/",
                        strategy="mobile", performance=80.0),
    )
    database.finish_pagespeed_run(run_id, status="ok", urls_tested=1, failures=0)
    token = database.pagespeed_results(run_id=run_id)[0]["share_token"]

    # The page still works -- the measurement is what matters.
    assert client.get(f"/share/speed/{token}").status_code == 200
    # The attachment does not exist, and says so.
    assert client.get(f"/share/speed/{token}/lighthouse.json").status_code == 404


# -- honesty about what the Google link is -----------------------------------


def test_the_page_does_not_pass_a_re_test_off_as_the_measurement(client, database):
    token = store(database)["mobile"]

    body = client.get(f"/share/speed/{token}").text

    assert "run a new test on pagespeed.web.dev" in body
    assert "the score will differ" in body


# -- tokens are the only thing guarding this ---------------------------------


def test_tokens_are_unguessable_and_unique(database):
    seen = {store(database)["mobile"] for _ in range(20)}

    assert len(seen) == 20
    assert all(len(token) >= 16 for token in seen)


def test_sharing_does_not_open_a_hole_into_the_dashboard(client, database):
    """A /share/ path must not become a way past the login for anything else."""
    store(database)

    for path in ("/", "/sites", "/runs", "/settings", "/pagespeed"):
        response = client.get(path, follow_redirects=False)
        assert response.status_code in (302, 303, 307), path
        assert response.headers["location"] == "/login", path


def test_a_shared_report_is_not_indexable(client, database):
    token = store(database)["mobile"]

    assert "noindex" in client.get(f"/share/speed/{token}").text


# -- keeping the database bounded --------------------------------------------


def test_pruning_drops_old_report_blobs_but_keeps_the_measurements(database):
    for _ in range(4):
        store(database, strategies=("mobile",))

    dropped = database.prune_pagespeed_reports(keep_per_url=2)
    rows = database.pagespeed_results(limit=10)

    assert dropped == 2
    assert sum(1 for row in rows if row["report_json"] is not None) == 2
    # Every measurement, and every share link, still exists.
    assert len(rows) == 4
    assert all(row["performance"] == 62.0 for row in rows)
    assert all(row["share_token"] for row in rows)


def test_pruning_keeps_each_device_separately(database):
    for _ in range(3):
        store(database)  # mobile and desktop each time

    database.prune_pagespeed_reports(keep_per_url=1)
    kept = [
        row["strategy"]
        for row in database.pagespeed_results(limit=10)
        if row["report_json"] is not None
    ]

    assert sorted(kept) == ["desktop", "mobile"]


def test_pruning_with_zero_keeps_everything(database):
    store(database)

    assert database.prune_pagespeed_reports(keep_per_url=0) == 0
    assert all(
        row["report_json"] is not None
        for row in database.pagespeed_results(limit=10)
    )


def test_a_sweep_prunes_its_own_backlog(tmp_path):
    """Retention runs after the sweep, so this run's reports are never the
    ones dropped -- they are the likeliest to be shared."""
    import asyncio

    import httpx

    from site_monitor.config import Settings, Site
    from site_monitor.pagespeed import run_pagespeed

    payload = {
        "lighthouseResult": {
            **LIGHTHOUSE,
            "categories": {"performance": {"score": 0.9}},
        }
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    settings = Settings(
        database_path=tmp_path / "sweep.db",
        pagespeed_keep_reports=1,
        retry_backoff=0.0,
    )
    site = Site(domain="client.example", pages=["https://client.example/"])

    with Database(settings.database_path) as db:
        for _ in range(3):
            asyncio.run(
                run_pagespeed(settings, db, [site],
                              transport=httpx.MockTransport(handler))
            )
        rows = db.pagespeed_results(limit=20)

    with_report = [r for r in rows if r["report_json"] is not None]

    # One kept per URL and device; the rest keep their scores and links.
    assert sorted(r["strategy"] for r in with_report) == ["desktop", "mobile"]
    assert len(rows) == 6
    assert all(r["share_token"] for r in rows)


def test_an_existing_database_upgrades_without_falling_over(tmp_path):
    """Opening a database written by the previous release must just work.

    This failed for real: the share_token index lived in SCHEMA, which runs
    before migrations, so on an existing pagespeed_results table the index
    referenced a column that did not exist yet and CREATE INDEX raised --
    breaking startup on the deploy that introduced it. Every test passed,
    because every test built its database from scratch.
    """
    import sqlite3

    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE pagespeed_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
            finished_at TEXT, status TEXT NOT NULL DEFAULT 'running',
            urls_tested INTEGER DEFAULT 0, failures INTEGER DEFAULT 0, error TEXT);
        CREATE TABLE pagespeed_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL REFERENCES pagespeed_runs(id) ON DELETE CASCADE,
            domain TEXT NOT NULL, url TEXT NOT NULL, strategy TEXT NOT NULL,
            performance REAL, fcp_ms REAL, lcp_ms REAL, cls REAL, tbt_ms REAL,
            speed_index REAL, tti_ms REAL, error TEXT, tested_at TEXT NOT NULL,
            report_url TEXT NOT NULL DEFAULT '');
        INSERT INTO pagespeed_runs (started_at) VALUES ('2026-08-30T06:00:00Z');
        INSERT INTO pagespeed_results
               (run_id, domain, url, strategy, performance, tested_at)
        VALUES (1, 'old.example', 'https://old.example/', 'mobile', 71.0,
                '2026-08-30T06:00:00Z');
        """
    )
    conn.commit()
    conn.close()

    with Database(path) as db:
        rows = db.pagespeed_results(limit=10)

        # The existing measurement survives, and the new columns are there.
        assert len(rows) == 1
        assert rows[0]["performance"] == 71.0
        assert rows[0]["report_json"] is None
        # Rows written before this release have no token, and that is fine:
        # the partial index only covers rows that have one.
        assert rows[0]["share_token"] is None
        assert db.pagespeed_result_by_token("") is None

        # New results still get tokens, and the unique index is in force.
        run_id = db.start_pagespeed_run(trigger="manual")
        db.record_pagespeed_result(
            run_id,
            PageSpeedResult(domain="new.example", url="https://new.example/",
                            strategy="mobile", performance=88.0),
        )
        fresh = [r for r in db.pagespeed_results(run_id=run_id)]
        assert fresh[0]["share_token"]
