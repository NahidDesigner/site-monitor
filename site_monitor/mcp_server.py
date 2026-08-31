"""An MCP server over the monitor, so Claude can operate it conversationally.

Mounted into the same FastAPI app at /mcp, sharing its database and its
RunManager -- a check started from Claude is the same run, under the same
one-at-a-time lock, as one started from the dashboard.
"""

from __future__ import annotations

import logging
from typing import Any

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

from .config import Settings
from .cron import CronError, describe, next_run
from .cron import parse as parse_cron
from .db import Database
from .runner import RunManager, resolve_sites

log = logging.getLogger(__name__)

INSTRUCTIONS = """\
Site Monitor watches WordPress/Elementor sites for two problems.

**Broken Elementor CSS.** Cached HTML can reference a stylesheet whose ?ver=
timestamp no longer exists. That URL 404s and WordPress answers with its own
HTML error page, so the browser parses zero rules and the layout silently
breaks with nothing in the console. A stylesheet is healthy only when it
returns HTTP 200 AND a content type containing text/css.

A site can also come back with a `warning` instead of a verdict: every page
loaded, but none of them referenced an Elementor stylesheet, so the check had
nothing to judge. Do not report those sites as healthy -- zero broken is only
good news when something was actually checked.

**PageSpeed.** Lighthouse performance history per site, mobile and desktop.

Checks run in the background on the server: trigger one, then poll
get_run_status. Only one check and one PageSpeed sweep run at a time, because
two concurrent passes would double the request load on every monitored origin.

A full check of every site takes a few minutes. A full PageSpeed sweep takes
much longer -- roughly 20-40 seconds per URL -- so prefer scoping either to a
single site when investigating one.
"""

# Read-only tools are hinted as such so a client can surface them differently
# from ones that change something or cost real requests.
READ = ToolAnnotations(read_only_hint=True, destructive_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False)
DESTRUCTIVE = ToolAnnotations(read_only_hint=False, destructive_hint=True)


def _site_dict(site) -> dict[str, Any]:
    return {
        "domain": site.domain,
        "enabled": site.enabled,
        "pages": len(site.pages),
        "source": "curated list" if site.pages else "sitemap",
        "sitemap": site.sitemap or None,
        "max_pages": site.max_pages,
    }


def _row(row, *keys) -> dict[str, Any]:
    return {key: row[key] for key in keys}


def build_mcp_server(
    base_settings: Settings,
    manager: RunManager,
    current_settings,
) -> MCPServer:
    """Wire the monitor's operations up as MCP tools."""

    server = MCPServer(
        name="site-monitor",
        title="Site Monitor",
        instructions=INSTRUCTIONS,
        version="1.0.0",
    )

    def db() -> Database:
        return Database(base_settings.database_path)

    # ---- sites -----------------------------------------------------------

    @server.tool(
        description="List every configured site, with page counts and whether "
        "it is included in scheduled checks.",
        annotations=READ,
    )
    async def list_sites(enabled_only: bool = False) -> dict:
        with db() as database:
            sites = database.list_sites(enabled_only=enabled_only)
        return {
            "count": len(sites),
            "total_pages": sum(len(s.pages) for s in sites),
            "sites": [_site_dict(s) for s in sites],
        }

    @server.tool(
        description="Everything known about one site: its page list, whether it "
        "is active, and every stylesheet breakage ever recorded for it.",
        annotations=READ,
    )
    async def get_site(domain: str, history_limit: int = 20) -> dict:
        with db() as database:
            row = database.get_site(domain)
            if row is None:
                return {"error": f"No site called {domain}"}
            pages = database.site_pages(row["id"])
            history = database.breakage_history(domain, history_limit)
        return {
            "domain": row["domain"],
            "enabled": bool(row["enabled"]),
            "sitemap": row["sitemap"] or None,
            "pages": pages,
            "breakage_history": [
                _row(h, "page_url", "asset_url", "status_code", "content_type",
                     "reason", "detected_at")
                for h in history
            ],
        }

    @server.tool(
        description="Add a site, or replace an existing one's configuration. "
        "Give either a list of page URLs (exact, preferred) or a sitemap URL "
        "to crawl. Page URLs must be absolute.",
        annotations=WRITE,
    )
    async def add_site(
        domain: str,
        pages: list[str] | None = None,
        sitemap: str = "",
        enabled: bool = True,
        max_pages: int | None = None,
    ) -> dict:
        pages = [p.strip() for p in (pages or []) if p and p.strip()]
        bad = [p for p in pages if not p.startswith(("http://", "https://"))]
        if bad:
            return {"error": f"These page URLs are not absolute: {bad[:3]}"}
        if not pages and not sitemap:
            return {"error": "Give either pages or a sitemap URL."}

        with db() as database:
            database.upsert_site(
                domain=domain.strip().lower(),
                sitemap=sitemap.strip(),
                pages=pages,
                enabled=enabled,
                max_pages=max_pages,
            )
            saved = database.get_site(domain.strip().lower())
        return {"saved": domain.strip().lower(), "pages": len(pages),
                "enabled": bool(saved["enabled"])}

    @server.tool(
        description="Pause or resume a site. Paused sites are left out of "
        "scheduled checks but can still be checked on demand.",
        annotations=WRITE,
    )
    async def set_site_enabled(domain: str, enabled: bool) -> dict:
        with db() as database:
            if not database.set_site_enabled(domain, enabled):
                return {"error": f"No site called {domain}"}
        return {"domain": domain, "enabled": enabled}

    @server.tool(
        description="Permanently remove a site and its page list. Past reports "
        "are kept. This cannot be undone.",
        annotations=DESTRUCTIVE,
    )
    async def remove_site(domain: str) -> dict:
        with db() as database:
            removed = database.delete_site(domain)
        return {"removed": removed, "domain": domain}

    # ---- running checks --------------------------------------------------

    @server.tool(
        description="Start an Elementor CSS check. Omit `domain` to check every "
        "active site, or give one to check just that site. Returns immediately; "
        "poll get_run_status for progress.",
        annotations=WRITE,
    )
    async def run_check(domain: str | None = None) -> dict:
        manager.refresh_settings(current_settings())
        only = None
        if domain:
            with db() as database:
                only = [s for s in database.list_sites() if s.domain == domain]
            if not only:
                return {"error": f"No site called {domain}"}

        started, message = await manager.trigger(
            trigger=f"site:{domain}" if domain else "mcp", only=only
        )
        return {"started": started, "message": message}

    @server.tool(
        description="Start a PageSpeed test. Omit `domain` to test every site "
        "(slow: 20-40 seconds per URL), or give one for a single site. Returns "
        "immediately; poll get_run_status.",
        annotations=WRITE,
    )
    async def run_pagespeed(domain: str | None = None) -> dict:
        manager.refresh_settings(current_settings())
        only = None
        if domain:
            with db() as database:
                only = [s for s in database.list_sites() if s.domain == domain]
            if not only:
                return {"error": f"No site called {domain}"}

        started, message = await manager.trigger_pagespeed(
            trigger=f"site:{domain}" if domain else "mcp", only=only
        )
        return {"started": started, "message": message}

    @server.tool(
        description="Progress of the current or most recent check and PageSpeed "
        "sweep: state, how far through, and what has been found so far.",
        annotations=READ,
    )
    async def get_run_status() -> dict:
        return {
            "css_check": manager.progress.as_dict(),
            "pagespeed": manager.ps_progress.as_dict(),
        }

    # ---- reading results -------------------------------------------------

    @server.tool(
        description="Every Elementor stylesheet broken in the most recent check, "
        "grouped by site. This is the answer to 'what is broken right now'. "
        "Always read `not_verified` too: those sites had no stylesheets to "
        "judge, so they are unproven rather than healthy.",
        annotations=READ,
    )
    async def current_breakages() -> dict:
        with db() as database:
            latest = database.latest_run()
            if latest is None:
                return {"message": "No checks have run yet."}
            broken = database.broken_assets_for_run(latest["id"])
            errors = database.page_errors_for_run(latest["id"])
            sites = database.site_runs_for_run(latest["id"])

        by_site: dict[str, list] = {}
        for row in broken:
            by_site.setdefault(row["domain"], []).append(
                _row(row, "page_url", "asset_url", "status_code",
                     "content_type", "reason")
            )
        return {
            "run_id": latest["id"],
            "checked_at": latest["started_at"],
            "status": latest["status"],
            "pages_checked": latest["pages_checked"],
            "broken_count": latest["broken_assets"],
            "sites_affected": len(by_site),
            "by_site": by_site,
            "unreachable_pages": [
                _row(e, "domain", "page_url", "error") for e in errors
            ],
            # Sites whose pages all loaded but referenced no Elementor
            # stylesheet at all. Nothing was verified for these, so their
            # absence from `by_site` is not evidence that they are healthy.
            "not_verified": [
                _row(s, "domain", "pages_checked", "warning")
                for s in sites
                if s["warning"]
            ],
        }

    @server.tool(
        description="Recent check reports. Filter by source: 'all', 'scheduled', "
        "'manual' or 'spot' (single-site checks).",
        annotations=READ,
    )
    async def list_reports(source: str = "all", limit: int = 20) -> dict:
        if source not in {"all", *Database.SOURCE_FILTERS}:
            return {"error": f"source must be one of: all, {', '.join(Database.SOURCE_FILTERS)}"}
        with db() as database:
            runs = database.runs_by_source(source, limit=limit)
            counts = database.run_source_counts()
        return {
            "counts_by_source": counts,
            "reports": [
                _row(r, "id", "started_at", "status", "trigger", "scope",
                     "sites_checked", "pages_checked", "assets_checked",
                     "broken_assets")
                for r in runs
            ],
        }

    @server.tool(
        description="One check report in full: per-site totals, every broken "
        "stylesheet, and any pages that could not be reached.",
        annotations=READ,
    )
    async def get_report(run_id: int) -> dict:
        with db() as database:
            run = database.run(run_id)
            if run is None:
                return {"error": f"No report {run_id}"}
            broken = database.broken_assets_for_run(run_id)
            sites = database.site_runs_for_run(run_id)
            errors = database.page_errors_for_run(run_id)
        return {
            "report": _row(run, "id", "started_at", "finished_at", "status",
                           "trigger", "scope", "sites_checked", "pages_checked",
                           "assets_checked", "broken_assets", "error"),
            "broken": [
                _row(b, "domain", "page_url", "asset_url", "status_code",
                     "content_type", "reason")
                for b in broken
            ],
            "per_site": [
                _row(s, "domain", "pages_checked", "assets_checked",
                     "broken_assets", "duration_ms", "error", "warning")
                for s in sites
            ],
            "unreachable_pages": [
                _row(e, "domain", "page_url", "error") for e in errors
            ],
        }

    @server.tool(
        description="PageSpeed results, one row per page with mobile and desktop "
        "side by side. Each carries a shareable report link. Sort by "
        "'performance', 'lcp', 'cls', 'tbt', 'domain' or 'tested_at'.",
        annotations=READ,
    )
    async def pagespeed_results(
        domain: str | None = None,
        sort: str = "performance",
        direction: str = "asc",
        limit: int = 50,
    ) -> dict:
        with db() as database:
            pairs = database.pagespeed_pairs(
                domain=domain, sort=sort, direction=direction, limit=limit
            )
            tested = set(database.pagespeed_domains())
            configured = [s.domain for s in database.list_sites()]

        def side(row):
            if row is None:
                return None
            return _row(row, "performance", "lcp_ms", "cls", "tbt_ms",
                        "fcp_ms", "error", "report_url")

        return {
            "coverage": {
                "sites_configured": len(configured),
                "sites_with_results": len(tested & set(configured)),
                "never_tested": sorted(set(configured) - tested),
            },
            "results": [
                {
                    "domain": p["domain"],
                    "url": p["url"],
                    "tested_at": p["tested_at"],
                    "mobile": side(p["mobile"]),
                    "desktop": side(p["desktop"]),
                }
                for p in pairs
            ],
        }

    # ---- schedules -------------------------------------------------------

    @server.tool(
        description="The schedules this app runs itself, with their next fire "
        "times and what happened last time.",
        annotations=READ,
    )
    async def list_schedules() -> dict:
        settings = current_settings()
        with db() as database:
            rows = database.list_schedules()
        return {
            "timezone": settings.timezone,
            "schedules": [
                {
                    **_row(r, "id", "name", "kind", "cron", "last_run_at",
                           "last_status", "next_run_at"),
                    "enabled": bool(r["enabled"]),
                    "in_words": describe(r["cron"]),
                }
                for r in rows
            ],
        }

    @server.tool(
        description="Create a schedule. `kind` is 'css_check' or 'pagespeed'. "
        "`cron` is a five-field expression such as '0 */6 * * *', read in the "
        "app's configured timezone.",
        annotations=WRITE,
    )
    async def create_schedule(
        name: str, cron: str, kind: str = "css_check", enabled: bool = True
    ) -> dict:
        if kind not in {"css_check", "pagespeed"}:
            return {"error": "kind must be css_check or pagespeed"}
        try:
            parse_cron(cron)
        except CronError as exc:
            return {"error": str(exc)}

        settings = current_settings()
        with db() as database:
            schedule_id = database.create_schedule(
                name=name, kind=kind, cron=cron, enabled=enabled
            )
            upcoming = next_run(cron, tz=settings.timezone)
            database.set_schedule_next_run(
                schedule_id,
                upcoming.isoformat(timespec="seconds") if upcoming else None,
            )
        return {
            "id": schedule_id,
            "name": name,
            "in_words": describe(cron),
            "next_run_at": upcoming.isoformat(timespec="seconds") if upcoming else None,
        }

    @server.tool(
        description="Delete a schedule. Does not affect reports it already produced.",
        annotations=DESTRUCTIVE,
    )
    async def delete_schedule(schedule_id: int) -> dict:
        with db() as database:
            return {"deleted": database.delete_schedule(schedule_id)}

    # ---- configuration ---------------------------------------------------

    @server.tool(
        description="Current configuration and coverage: how many sites and "
        "pages are watched, crawl limits, and whether alerting and PageSpeed "
        "are configured. Never returns secrets.",
        annotations=READ,
    )
    async def get_status() -> dict:
        settings = current_settings()
        with db() as database:
            sites = database.list_sites()
            active = resolve_sites(settings, database)
            latest = database.latest_run()
            ps_latest = database.latest_pagespeed_run()
            schedules = database.list_schedules(enabled_only=True)
        return {
            "sites_configured": len(sites),
            "sites_active": len(active),
            "pages_watched": sum(len(s.pages) for s in active),
            "telegram_configured": settings.telegram_enabled,
            "pagespeed_key_configured": bool(settings.pagespeed_api_key),
            "timezone": settings.timezone,
            "limits": {
                "sites_at_once": settings.site_concurrency,
                "pages_at_once": settings.page_concurrency,
                "stylesheets_at_once": settings.asset_concurrency,
                "pagespeed_at_once": settings.pagespeed_concurrency,
            },
            "active_schedules": len(schedules),
            "last_check": _row(latest, "id", "started_at", "status", "broken_assets")
            if latest else None,
            "last_pagespeed": _row(ps_latest, "id", "started_at", "status",
                                   "urls_tested", "failures")
            if ps_latest else None,
        }

    return server
