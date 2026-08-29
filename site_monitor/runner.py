"""Executing a check run, and tracking one in progress.

The CLI and the dashboard both go through here, so a run triggered from a
button behaves exactly like the one cron fires -- same persistence, same
alerting, same result.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone

from .config import Settings, Site
from .crawler import RunResult, SiteResult, run_checks
from .db import Database
from .notifier import TelegramNotifier, format_alert
from .pagespeed import run_pagespeed

log = logging.getLogger(__name__)


class NoSitesConfigured(RuntimeError):
    """Raised when there is nothing to check."""


def resolve_sites(settings: Settings, database: Database) -> list[Site]:
    """The site list to check, from the database.

    The database is the source of truth because the dashboard edits it. A
    sites.yaml is treated as an import source: if the database is empty and a
    file is configured, it is imported once so an existing deployment keeps
    working without anyone having to do anything.
    """
    sites = database.list_sites(enabled_only=True)
    if sites:
        return sites

    if database.site_count() == 0 and settings.sites:
        log.info(
            "no sites in the database; importing %s from %s",
            len(settings.sites),
            settings.sites_file,
        )
        database.import_sites(list(settings.sites))
        return database.list_sites(enabled_only=True)

    return sites


@dataclass
class RunProgress:
    """A snapshot of the current or most recent run, safe to serialise."""

    state: str = "idle"  # idle | running | finished | failed
    run_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    sites_total: int = 0
    sites_done: int = 0
    pages_checked: int = 0
    assets_checked: int = 0
    broken_assets: int = 0
    sites_with_findings: int = 0
    current: str = ""
    alert_sent: int | None = None
    error: str | None = None
    trigger: str = ""

    @property
    def percent(self) -> int:
        if not self.sites_total:
            return 0
        return min(100, round(self.sites_done / self.sites_total * 100))

    def as_dict(self) -> dict:
        data = asdict(self)
        data["percent"] = self.percent
        return data


async def execute_run(
    settings: Settings,
    database: Database,
    sites: list[Site],
    *,
    on_site: "callable | None" = None,
) -> tuple[RunResult, int]:
    """Run the checks, persisting each site's result as it lands."""
    run_id = database.start_run()
    scoped = Settings(**{**settings.__dict__, "sites": tuple(sites)})

    def record(result: SiteResult) -> None:
        database.record_site_result(run_id, result)
        if on_site is not None:
            on_site(result)

    try:
        run = await run_checks(scoped, on_site_complete=record)
    except Exception as exc:
        database.finish_run(
            run_id,
            status="failed",
            sites_checked=0,
            pages_checked=0,
            assets_checked=0,
            broken_assets=0,
            error=f"{type(exc).__name__}: {exc}",
        )
        raise

    database.finish_run(
        run_id,
        status="completed",
        sites_checked=len(run.sites),
        pages_checked=run.pages_checked,
        assets_checked=run.assets_checked,
        broken_assets=run.broken_asset_count,
    )
    return run, run_id


async def deliver_alert(settings: Settings, run: RunResult) -> int:
    """Send the Telegram alert if there is anything to say. Returns messages sent."""
    messages = format_alert(run)
    if not messages:
        return 0
    if not settings.telegram_enabled:
        log.warning("breakages found but Telegram credentials are not configured")
        return 0

    notifier = TelegramNotifier(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
    )
    return await notifier.send(messages)


@dataclass
class PageSpeedProgress:
    """Snapshot of a PageSpeed sweep."""

    state: str = "idle"
    run_id: int | None = None
    started_at: str | None = None
    finished_at: str | None = None
    total: int = 0
    done: int = 0
    failures: int = 0
    current: str = ""
    error: str | None = None
    trigger: str = ""

    @property
    def percent(self) -> int:
        if not self.total:
            return 0
        return min(100, round(self.done / self.total * 100))

    def as_dict(self) -> dict:
        data = asdict(self)
        data["percent"] = self.percent
        return data


class RunManager:
    """Owns the single in-flight run, so two triggers cannot overlap.

    A second request while a run is in progress is refused rather than queued:
    two concurrent passes would double the request load on every monitored
    origin, which is the one thing this tool must not do.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._lock = asyncio.Lock()
        self._task: asyncio.Task | None = None
        self._progress = RunProgress()
        self._ps_lock = asyncio.Lock()
        self._ps_task: asyncio.Task | None = None
        self._ps_progress = PageSpeedProgress()

    @property
    def settings(self) -> Settings:
        return self._settings

    @property
    def progress(self) -> RunProgress:
        return self._progress

    @property
    def is_running(self) -> bool:
        return self._progress.state == "running"

    @property
    def ps_progress(self) -> PageSpeedProgress:
        return self._ps_progress

    @property
    def ps_running(self) -> bool:
        return self._ps_progress.state == "running"

    def refresh_settings(self, settings: Settings) -> None:
        """Adopt settings edited in the dashboard, without a restart."""
        self._settings = settings

    async def trigger(self, *, trigger: str = "manual") -> tuple[bool, str]:
        """Start a run in the background. Returns (started, message)."""
        async with self._lock:
            if self.is_running:
                return False, "A check is already running."

            with Database(self._settings.database_path) as database:
                sites = resolve_sites(self._settings, database)
            if not sites:
                return False, "No sites are configured yet."

            self._progress = RunProgress(
                state="running",
                started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                sites_total=len(sites),
                trigger=trigger,
                current="starting",
            )
            self._task = asyncio.create_task(self._run(sites))
            return True, f"Checking {len(sites)} sites."

    async def _run(self, sites: list[Site]) -> None:
        progress = self._progress
        started = time.perf_counter()
        try:
            with Database(self._settings.database_path) as database:

                def on_site(result: SiteResult) -> None:
                    progress.sites_done += 1
                    progress.pages_checked += result.pages_checked
                    progress.assets_checked += result.assets_checked
                    progress.broken_assets += result.broken_asset_count
                    if result.has_findings:
                        progress.sites_with_findings += 1
                    progress.current = result.domain

                run, run_id = await execute_run(
                    self._settings, database, sites, on_site=on_site
                )
                progress.run_id = run_id

            progress.alert_sent = await deliver_alert(self._settings, run)
            progress.state = "finished"
            progress.current = ""
        except Exception as exc:
            log.exception("run failed")
            progress.state = "failed"
            progress.error = f"{type(exc).__name__}: {exc}"
        finally:
            progress.finished_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
            log.info(
                "run finished in %.1fs (%s)",
                time.perf_counter() - started,
                progress.state,
            )

    # -- PageSpeed ---------------------------------------------------------

    async def trigger_pagespeed(
        self, *, trigger: str = "manual", strategies: tuple[str, ...] | None = None
    ) -> tuple[bool, str]:
        async with self._ps_lock:
            if self.ps_running:
                return False, "A PageSpeed sweep is already running."

            with Database(self._settings.database_path) as database:
                sites = resolve_sites(self._settings, database)
            if not sites:
                return False, "No sites are configured yet."

            picked = strategies or self._settings.pagespeed_strategies
            self._ps_progress = PageSpeedProgress(
                state="running",
                started_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                total=len(sites) * max(1, len(picked)),
                trigger=trigger,
                current="starting",
            )
            self._ps_task = asyncio.create_task(self._run_pagespeed(sites, picked))
            return True, f"Testing {len(sites)} sites."

    async def _run_pagespeed(self, sites, strategies) -> None:
        progress = self._ps_progress
        try:
            with Database(self._settings.database_path) as database:

                def on_result(result) -> None:
                    progress.done += 1
                    if not result.ok:
                        progress.failures += 1
                    progress.current = f"{result.domain} ({result.strategy})"

                run_id, tested, failures = await run_pagespeed(
                    self._settings,
                    database,
                    sites,
                    strategies=strategies,
                    on_result=on_result,
                )
                progress.run_id = run_id
                progress.done = tested
                progress.failures = failures
            progress.state = "finished"
            progress.current = ""
        except Exception as exc:
            log.exception("pagespeed run failed")
            progress.state = "failed"
            progress.error = f"{type(exc).__name__}: {exc}"
        finally:
            progress.finished_at = datetime.now(timezone.utc).isoformat(
                timespec="seconds"
            )
