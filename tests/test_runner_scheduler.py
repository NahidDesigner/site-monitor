"""The run manager's guarantees, and the in-process scheduler."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from site_monitor.config import Settings, Site
from site_monitor.db import Database
from site_monitor.runner import RunManager
from site_monitor.scheduler import Scheduler
import site_monitor.runner as runner_module

PAGE = (
    "<html><head><link rel='stylesheet' "
    "href='https://a.com/wp-content/uploads/elementor/css/post-1.css?ver=1'>"
    "</head></html>"
)


def handler(request: httpx.Request) -> httpx.Response:
    url = str(request.url)
    if url.endswith(".css") or ".css?" in url:
        return httpx.Response(404, headers={"content-type": "text/html"})
    return httpx.Response(200, text=PAGE, headers={"content-type": "text/html"})


def settings_for(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "run.db", retry_backoff=0.0, max_retries=1
    )


def seed(tmp_path, domain="a.com"):
    with Database(tmp_path / "run.db") as database:
        database.upsert_site(domain=domain, pages=[f"https://{domain}/"])


async def test_manager_runs_and_records(tmp_path, monkeypatch):
    seed(tmp_path)
    manager = RunManager(settings_for(tmp_path))

    real = runner_module.run_checks

    async def patched(settings, *, transport=None, on_site_complete=None):
        return await real(
            settings,
            transport=httpx.MockTransport(handler),
            on_site_complete=on_site_complete,
        )

    monkeypatch.setattr(runner_module, "run_checks", patched)

    started, message = await manager.trigger()
    assert started, message
    await manager._task

    assert manager.progress.state == "finished"
    assert manager.progress.broken_assets == 1
    assert manager.progress.percent == 100
    with Database(tmp_path / "run.db") as database:
        assert database.latest_run()["broken_assets"] == 1


async def test_a_second_run_is_refused_while_one_is_in_flight(tmp_path):
    """Two concurrent passes would double the load on every monitored origin."""
    seed(tmp_path)
    manager = RunManager(settings_for(tmp_path))
    manager._progress.state = "running"

    started, message = await manager.trigger()

    assert not started
    assert "already running" in message


async def test_trigger_refuses_when_no_sites_are_configured(tmp_path):
    manager = RunManager(settings_for(tmp_path))

    started, message = await manager.trigger()

    assert not started
    assert "No sites" in message


async def test_a_failing_run_is_reported_not_swallowed(tmp_path, monkeypatch):
    seed(tmp_path)
    manager = RunManager(settings_for(tmp_path))

    async def explode(*args, **kwargs):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(runner_module, "run_checks", explode)

    started, _ = await manager.trigger()
    assert started
    await manager._task

    assert manager.progress.state == "failed"
    assert "kaboom" in manager.progress.error
    with Database(tmp_path / "run.db") as database:
        assert database.latest_run()["status"] == "failed"


# -- scheduler ----------------------------------------------------------------


class FakeManager:
    """Records what the scheduler asked for, without doing any work."""

    def __init__(self, settings):
        self.settings = settings
        self.calls: list[str] = []

    async def trigger(self, *, trigger="manual"):
        self.calls.append(f"css:{trigger}")
        return True, "started"

    async def trigger_pagespeed(self, *, trigger="manual", strategies=None):
        self.calls.append(f"ps:{trigger}")
        return True, "started"


@pytest.fixture
def scheduler(tmp_path):
    settings = settings_for(tmp_path)
    manager = FakeManager(settings)
    return Scheduler(manager, settings), manager


async def test_a_due_schedule_fires_and_is_rearmed(tmp_path, scheduler):
    sched, manager = scheduler
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with Database(tmp_path / "run.db") as database:
        schedule_id = database.create_schedule(name="s", kind="css_check", cron="*/5 * * * *")
        database.set_schedule_next_run(schedule_id, past)

    await sched._tick()

    assert manager.calls == ["css:schedule:s"]
    with Database(tmp_path / "run.db") as database:
        row = database.get_schedule(schedule_id)
    assert row["last_status"] == "started"
    assert datetime.fromisoformat(row["next_run_at"]) > datetime.now(timezone.utc)


async def test_a_schedule_not_yet_due_does_not_fire(tmp_path, scheduler):
    sched, manager = scheduler
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(timespec="seconds")
    with Database(tmp_path / "run.db") as database:
        schedule_id = database.create_schedule(name="s", kind="css_check", cron="0 * * * *")
        database.set_schedule_next_run(schedule_id, future)

    await sched._tick()

    assert manager.calls == []


async def test_a_new_schedule_is_armed_rather_than_fired_immediately(tmp_path, scheduler):
    """Adding a schedule must not kick off a run the moment you save it."""
    sched, manager = scheduler
    with Database(tmp_path / "run.db") as database:
        schedule_id = database.create_schedule(name="s", kind="css_check", cron="0 * * * *")

    await sched._tick()

    assert manager.calls == []
    with Database(tmp_path / "run.db") as database:
        assert database.get_schedule(schedule_id)["next_run_at"] is not None


async def test_disabled_schedules_are_skipped(tmp_path, scheduler):
    sched, manager = scheduler
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with Database(tmp_path / "run.db") as database:
        schedule_id = database.create_schedule(
            name="s", kind="css_check", cron="*/5 * * * *", enabled=False
        )
        database.set_schedule_next_run(schedule_id, past)

    await sched._tick()

    assert manager.calls == []


async def test_pagespeed_schedules_route_to_the_pagespeed_job(tmp_path, scheduler):
    sched, manager = scheduler
    past = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
    with Database(tmp_path / "run.db") as database:
        schedule_id = database.create_schedule(name="nightly", kind="pagespeed", cron="0 3 * * *")
        database.set_schedule_next_run(schedule_id, past)

    await sched._tick()

    assert manager.calls == ["ps:schedule:nightly"]


async def test_a_corrupt_next_run_time_is_repaired_not_fatal(tmp_path, scheduler):
    sched, manager = scheduler
    with Database(tmp_path / "run.db") as database:
        schedule_id = database.create_schedule(name="s", kind="css_check", cron="0 * * * *")
        database.set_schedule_next_run(schedule_id, "not-a-timestamp")

    await sched._tick()

    assert manager.calls == []
    with Database(tmp_path / "run.db") as database:
        assert database.get_schedule(schedule_id)["next_run_at"] != "not-a-timestamp"
