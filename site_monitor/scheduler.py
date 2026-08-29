"""In-process cron. The app owns its own schedules, so nothing outside it needs
configuring — that is the whole point of managing this from a dashboard.

The loop wakes every 30 seconds, fires anything due, and recomputes the next
time. Schedules are stored with their next fire time so a restart does not
lose or double-fire them.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from .config import Settings
from .cron import CronError, next_run
from .db import Database
from .runner import RunManager

log = logging.getLogger(__name__)

TICK_SECONDS = 30


class Scheduler:
    """Fires due schedules through the shared RunManager."""

    def __init__(self, manager: RunManager, settings: Settings) -> None:
        self._manager = manager
        self._settings = settings
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()

    def start(self) -> None:
        if self._task is None or self._task.done():
            self._stopping.clear()
            self._task = asyncio.create_task(self._loop())
            log.info("scheduler started")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def timezone_name(self) -> str:
        return self._manager.settings.timezone or "UTC"

    def compute_next(self, cron: str, *, after: datetime | None = None) -> str | None:
        moment = next_run(cron, tz=self.timezone_name(), after=after)
        return moment.isoformat(timespec="seconds") if moment else None

    def reschedule(self, database: Database, schedule_id: int, cron: str) -> None:
        """Recompute one schedule's next fire time (after an edit)."""
        try:
            database.set_schedule_next_run(schedule_id, self.compute_next(cron))
        except CronError:
            database.set_schedule_next_run(schedule_id, None)

    async def _loop(self) -> None:
        # Give the app a moment to finish starting before the first tick.
        await asyncio.sleep(2)
        while not self._stopping.is_set():
            try:
                await self._tick()
            except Exception:
                log.exception("scheduler tick failed")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=TICK_SECONDS)
            except asyncio.TimeoutError:
                pass

    async def _tick(self) -> None:
        now = datetime.now(timezone.utc)
        due: list[tuple[int, str, str, str]] = []

        with Database(self._settings.database_path) as database:
            for row in database.list_schedules(enabled_only=True):
                cron = row["cron"]
                next_at = row["next_run_at"]

                if not next_at:
                    # First sight of this schedule (or it was just edited):
                    # arm it rather than firing immediately.
                    self.reschedule(database, row["id"], cron)
                    continue

                try:
                    scheduled = datetime.fromisoformat(next_at)
                except ValueError:
                    self.reschedule(database, row["id"], cron)
                    continue

                if scheduled <= now:
                    due.append((row["id"], row["name"], row["kind"], cron))

        for schedule_id, name, kind, cron in due:
            await self._fire(schedule_id, name, kind, cron)

    async def _fire(self, schedule_id: int, name: str, kind: str, cron: str) -> None:
        log.info("schedule %s (%s) is due", name, kind)
        if kind == "pagespeed":
            started, message = await self._manager.trigger_pagespeed(
                trigger=f"schedule:{name}"
            )
        else:
            started, message = await self._manager.trigger(trigger=f"schedule:{name}")

        status = "started" if started else f"skipped: {message}"
        with Database(self._settings.database_path) as database:
            database.mark_schedule_run(
                schedule_id, status=status, next_run_at=self.compute_next(cron)
            )
        if not started:
            log.warning("schedule %s did not start — %s", name, message)
