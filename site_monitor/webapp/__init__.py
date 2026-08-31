"""The dashboard: manage sites and schedules, trigger runs, read and export
results. Everything this tool does is reachable from a browser."""

from __future__ import annotations

import asyncio
import hmac
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlparse

import yaml
from fastapi import FastAPI, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from ..config import ConfigError, Settings, apply_overrides, load_sites
from ..cron import CronError, describe, next_run, parse as parse_cron
from ..db import Database
from ..exports import (
    BROKEN_COLUMNS,
    PAGESPEED_COLUMNS,
    RUNS_COLUMNS,
    timestamp,
    to_csv,
    to_xlsx,
)
from ..runner import RunManager, resolve_sites
from ..scheduler import Scheduler
from . import auth

log = logging.getLogger(__name__)

TEMPLATES = Path(__file__).parent / "templates"

# Cost of a wrong password. Long enough to make online guessing
# hopeless, short enough that a typo is not annoying.
LOGIN_FAILURE_DELAY = 0.75


def _normalize_domain(raw: str) -> str:
    value = raw.strip().lower()
    if not value:
        return ""
    if "//" not in value:
        value = f"https://{value}"
    parsed = urlparse(value)
    return (parsed.netloc or parsed.path).strip("/").split("/")[0]


def _parse_pages(raw: str) -> tuple[list[str], list[str]]:
    """Split a textarea into (valid URLs, rejected lines)."""
    good: list[str] = []
    bad: list[str] = []
    for line in raw.replace(",", "\n").splitlines():
        url = line.strip()
        if not url:
            continue
        if url.startswith(("http://", "https://")):
            if url not in good:
                good.append(url)
        else:
            bad.append(url)
    return good, bad


def _same_origin_path(request: Request, fallback: str = "/") -> str:
    """The page the request came from, if it is one of ours.

    Used to return someone to where they pressed the button. Only a path from
    this host is accepted -- taking the Referer at face value would be an open
    redirect.
    """
    referer = request.headers.get("referer") or ""
    if not referer:
        return fallback
    parsed = urlparse(referer)
    if parsed.netloc and parsed.netloc != request.url.netloc:
        return fallback
    return parsed.path or fallback


def _redirect(path: str, **params) -> RedirectResponse:
    from urllib.parse import urlencode

    query = urlencode({k: v for k, v in params.items() if v})
    return RedirectResponse(f"{path}?{query}" if query else path, status_code=303)


def _local_filter(tz_name: str):
    """Render a stored UTC timestamp in the timezone schedules are written in.

    Storing UTC and showing UTC means someone who typed "3am" sees "07:00" and
    reasonably concludes the schedule is wrong.
    """
    from datetime import datetime

    from ..cron import get_zone

    zone = get_zone(tz_name)

    def render(value: str | None, fmt: str = "%d %b, %H:%M") -> str:
        if not value:
            return "—"
        try:
            moment = datetime.fromisoformat(str(value))
        except ValueError:
            return str(value)
        if moment.tzinfo is None:
            from datetime import timezone as _tz

            moment = moment.replace(tzinfo=_tz.utc)
        local = moment.astimezone(zone)
        label = local.tzname() or tz_name
        return f"{local.strftime(fmt)} {label}"

    return render


def create_app(base_settings: Settings) -> FastAPI:
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.filters["local"] = _local_filter(base_settings.timezone or "UTC")
    templates.env.globals["tz_name"] = base_settings.timezone or "UTC"

    def db() -> Database:
        return Database(base_settings.database_path)

    def current_settings() -> Settings:
        """Environment, with anything the dashboard has saved layered on top."""
        with db() as database:
            return apply_overrides(base_settings, database.get_settings())

    manager = RunManager(current_settings())
    scheduler = Scheduler(manager, base_settings)
    throttle = auth.LoginThrottle()

    # Built before the lifespan closure so the closure can start it. A mounted
    # sub-app's lifespan is not run by the parent, and this one needs its task
    # group started or every request fails with "Task group is not initialized".
    mcp_app = None
    if base_settings.mcp_enabled:
        from ..mcp_server import build_mcp_server

        mcp_app = build_mcp_server(
            base_settings, manager, current_settings
        ).streamable_http_app(
            streamable_http_path="/", stateless_http=True, json_response=True,
        )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        with db() as database:
            orphaned = database.close_orphaned_runs()
            if orphaned:
                log.warning(
                    "marked %s run(s) as interrupted -- the app restarted while "
                    "they were in progress",
                    orphaned,
                )
            # Arm any schedule that has no next fire time yet, so a restart
            # neither loses a schedule nor fires it immediately.
            for row in database.list_schedules():
                if not row["next_run_at"] and row["enabled"]:
                    scheduler.reschedule(database, row["id"], row["cron"])
        scheduler.start()
        try:
            if mcp_app is not None:
                async with mcp_app.router.lifespan_context(mcp_app):
                    yield
            else:
                yield
        finally:
            await scheduler.stop()

    app = FastAPI(
        title="Site Monitor", docs_url=None, redoc_url=None, lifespan=lifespan
    )

    # Mounted only when a token exists: an unauthenticated endpoint that can
    # edit sites and start runs is not something to serve by accident.
    if mcp_app is not None:
        app.mount("/mcp", mcp_app)
        log.info("MCP server mounted at /mcp")
    app.state.manager = manager
    app.state.scheduler = scheduler

    def authed(request: Request) -> bool:
        return auth.verify(
            current_settings().session_secret, request.cookies.get(auth.COOKIE_NAME)
        )

    def render(request: Request, name: str, **context) -> HTMLResponse:
        return templates.TemplateResponse(
            request,
            name,
            {
                "progress": manager.progress,
                "ps_progress": manager.ps_progress,
                "active": "",
                **context,
            },
        )

    # ---- auth ----------------------------------------------------------

    @app.middleware("http")
    async def require_login(request: Request, call_next):
        if request.url.path in {"/login", "/healthz"}:
            return await call_next(request)

        # MCP authenticates with a bearer token rather than a browser session.
        if request.url.path.startswith("/mcp"):
            token = base_settings.mcp_token
            supplied = request.headers.get("authorization", "")
            prefix, _, value = supplied.partition(" ")
            if (
                not token
                or prefix.lower() != "bearer"
                or not hmac.compare_digest(value.strip(), token)
            ):
                client = request.client.host if request.client else "unknown"
                log.warning("rejected MCP request from %s", client)
                return JSONResponse(
                    {"error": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": "Bearer"},
                )
            return await call_next(request)
        if not authed(request):
            if request.url.path.startswith("/api/"):
                return JSONResponse({"error": "unauthorized"}, status_code=401)
            return RedirectResponse("/login", status_code=303)
        return await call_next(request)

    @app.get("/healthz")
    async def healthz():
        return {"ok": True}

    @app.get("/login", response_class=HTMLResponse)
    async def login_form(request: Request):
        if authed(request):
            return RedirectResponse("/", status_code=303)
        return render(request, "login.html", error=None)

    @app.post("/login")
    async def login(request: Request, password: str = Form("")):
        settings = current_settings()
        key = auth.client_key(request)

        remaining = throttle.locked_for(key)
        if remaining:
            minutes = max(1, round(remaining / 60))
            return render(
                request,
                "login.html",
                error=f"Too many attempts. Try again in {minutes} minute"
                f"{'s' if minutes != 1 else ''}.",
            )

        if not auth.password_matches(settings.dashboard_password, password):
            throttle.record_failure(key)
            log.warning("failed dashboard login from %s", key)
            # A fixed cost per guess, which no spoofed header can avoid.
            await asyncio.sleep(LOGIN_FAILURE_DELAY)
            return render(request, "login.html", error="That password is not correct.")

        throttle.reset(key)
        response = RedirectResponse("/", status_code=303)
        response.set_cookie(
            auth.COOKIE_NAME,
            auth.issue(settings.session_secret, hours=settings.session_hours),
            httponly=True,
            samesite="lax",
            secure=request.url.scheme == "https",
            max_age=settings.session_hours * 3600,
        )
        return response

    @app.post("/logout")
    async def logout():
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(auth.COOKIE_NAME)
        return response

    # ---- overview ------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    async def dashboard(request: Request):
        with db() as database:
            sites = database.list_sites()
            latest = database.latest_run()
            broken = database.broken_assets_for_run(latest["id"]) if latest else []
            errors = database.page_errors_for_run(latest["id"]) if latest else []
            recent = database.recent_runs(6)
            schedules = database.list_schedules()
            ps_latest = database.latest_pagespeed_run()
            ps_results = (
                database.pagespeed_results(
                    run_id=ps_latest["id"], sort="performance", direction="asc", limit=6
                )
                if ps_latest
                else []
            )

        grouped: dict[str, list] = {}
        for row in broken:
            grouped.setdefault(row["domain"], []).append(row)

        enabled = [s for s in sites if s.enabled]
        return render(
            request,
            "dashboard.html",
            active="home",
            sites=sites,
            enabled=enabled,
            total_pages=sum(len(s.pages) for s in enabled),
            latest=latest,
            grouped=grouped,
            errors=errors,
            recent=recent,
            schedules=schedules,
            ps_latest=ps_latest,
            ps_results=ps_results,
        )

    # ---- triggers ------------------------------------------------------

    @app.post("/run")
    async def trigger_run(request: Request):
        started, message = await manager.trigger(trigger="dashboard")
        # Back to wherever it was pressed: from the Sites page you want to
        # watch the rows tick off, not be bounced to the Overview.
        target = _same_origin_path(request, "/")
        return _redirect(
            target, **({"message": message} if started else {"error": message})
        )

    @app.post("/pagespeed/run")
    async def trigger_pagespeed():
        manager.refresh_settings(current_settings())
        started, message = await manager.trigger_pagespeed(trigger="dashboard")
        return _redirect(
            "/pagespeed", **({"message": message} if started else {"error": message})
        )

    @app.get("/api/run/status")
    async def run_status():
        return manager.progress.as_dict()

    @app.get("/api/pagespeed/status")
    async def ps_status():
        return manager.ps_progress.as_dict()

    # ---- runs ----------------------------------------------------------

    @app.get("/runs", response_class=HTMLResponse)
    async def runs(request: Request, source: str = Query("all")):
        if source not in {"all", *Database.SOURCE_FILTERS}:
            source = "all"
        with db() as database:
            return render(
                request,
                "runs.html",
                active="runs",
                runs=database.runs_by_source(source, limit=100),
                counts=database.run_source_counts(),
                source=source,
            )

    @app.get("/runs/{run_id}", response_class=HTMLResponse)
    async def run_detail(request: Request, run_id: int):
        with db() as database:
            record = database.run(run_id)
            if record is None:
                return RedirectResponse("/runs", status_code=303)
            grouped: dict[str, list] = {}
            for row in database.broken_assets_for_run(run_id):
                grouped.setdefault(row["domain"], []).append(row)
            return render(
                request,
                "run_detail.html",
                active="runs",
                run=record,
                grouped=grouped,
                site_runs=database.site_runs_for_run(run_id),
                errors=database.page_errors_for_run(run_id),
            )

    # ---- sites ---------------------------------------------------------

    @app.get("/sites", response_class=HTMLResponse)
    async def sites_index(request: Request, message: str = "", error: str = ""):
        with db() as database:
            return render(
                request,
                "sites.html",
                active="sites",
                sites=database.list_sites(),
                message=message,
                error=error,
            )

    @app.get("/sites/new", response_class=HTMLResponse)
    async def site_new(request: Request):
        return render(
            request, "site_edit.html", active="sites",
            site=None, pages=[], history=[], error=None,
        )

    @app.get("/sites/edit/{domain}", response_class=HTMLResponse)
    async def site_edit(request: Request, domain: str):
        with db() as database:
            row = database.get_site(domain)
            if row is None:
                return _redirect("/sites", error=f"No site called {domain}")
            return render(
                request,
                "site_edit.html",
                active="sites",
                site=dict(row),
                pages=database.site_pages(row["id"]),
                history=database.breakage_history(domain, 25),
                error=None,
            )

    @app.post("/sites/save")
    async def site_save(
        request: Request,
        domain: str = Form(""),
        sitemap: str = Form(""),
        pages: str = Form(""),
        enabled: str = Form(""),
        max_pages: str = Form(""),
        original: str = Form(""),
    ):
        clean_domain = _normalize_domain(domain)
        page_urls, rejected = _parse_pages(pages)
        sitemap = sitemap.strip()

        def fail(message: str):
            return render(
                request,
                "site_edit.html",
                active="sites",
                site={"domain": domain, "sitemap": sitemap,
                      "enabled": 1 if enabled else 0, "max_pages": max_pages},
                pages=page_urls,
                history=[],
                original=original,
                error=message,
            )

        if not clean_domain:
            return fail("Enter a domain, for example dvlfirm.com.")
        # Report malformed lines before "you gave us nothing": if every line
        # someone pasted was rejected, the useful message names the bad line,
        # not the empty result it produced.
        if rejected:
            return fail(
                f"{len(rejected)} line(s) are not full URLs, starting with "
                f"“{rejected[0]}”. Every page needs http:// or https:// in front."
            )
        if not sitemap and not page_urls:
            return fail("Add either a sitemap URL or at least one page URL.")
        if sitemap and not sitemap.startswith(("http://", "https://")):
            return fail("The sitemap needs to be a full URL starting with https://.")
        try:
            limit = int(max_pages) if str(max_pages).strip() else None
        except ValueError:
            return fail("Max pages needs to be a whole number, or left empty.")

        with db() as database:
            if original and original != clean_domain:
                database.delete_site(original)
            database.upsert_site(
                domain=clean_domain,
                sitemap=sitemap,
                pages=page_urls,
                enabled=bool(enabled),
                max_pages=limit,
            )
        return _redirect("/sites", message=f"Saved {clean_domain}")

    @app.post("/sites/{domain}/check")
    async def site_check(request: Request, domain: str):
        """Run a check against one site only — a spot check without waiting
        for, or paying for, the whole fleet."""
        with db() as database:
            match = [s for s in database.list_sites() if s.domain == domain]
        if not match:
            return _redirect("/sites", error=f"No site called {domain}")

        manager.refresh_settings(current_settings())
        started, message = await manager.trigger(trigger=f"site:{domain}", only=match)
        target = _same_origin_path(request, "/sites")
        return _redirect(
            target, **({"message": message} if started else {"error": message})
        )

    @app.post("/sites/{domain}/pagespeed")
    async def site_pagespeed(request: Request, domain: str):
        """Run a PageSpeed test against one site, without sweeping the fleet."""
        with db() as database:
            match = [s for s in database.list_sites() if s.domain == domain]
        if not match:
            return _redirect("/sites", error=f"No site called {domain}")

        manager.refresh_settings(current_settings())
        started, message = await manager.trigger_pagespeed(
            trigger=f"site:{domain}", only=match
        )
        target = _same_origin_path(request, "/sites")
        return _redirect(
            target, **({"message": message} if started else {"error": message})
        )

    @app.post("/sites/{domain}/toggle")
    async def site_toggle(domain: str):
        with db() as database:
            row = database.get_site(domain)
            if row is not None:
                database.set_site_enabled(domain, not row["enabled"])
        return _redirect("/sites")

    @app.post("/sites/{domain}/delete")
    async def site_delete(domain: str):
        with db() as database:
            database.delete_site(domain)
        return _redirect("/sites", message=f"Removed {domain}")

    @app.get("/import", response_class=HTMLResponse)
    async def import_form(request: Request):
        return render(request, "import.html", active="sites", error=None)

    @app.post("/import")
    async def import_yaml(
        request: Request, content: str = Form(""), replace: str = Form("")
    ):
        if not content.strip():
            return render(
                request, "import.html", active="sites",
                error="Paste a site list first.",
            )
        temp = Path(base_settings.database_path).parent / ".import.yaml"
        try:
            temp.parent.mkdir(parents=True, exist_ok=True)
            temp.write_text(content, encoding="utf-8")
            sites = load_sites(temp)
        except yaml.YAMLError as exc:
            # A raw parser dump ("while parsing a flow node...") tells someone
            # pasting a list nothing useful. Say what is wrong, then show it.
            detail = str(exc).split("\n")[0]
            return render(
                request,
                "import.html",
                active="sites",
                error=f"Could not read that as YAML — check the indentation. ({detail})",
            )
        except ConfigError as exc:
            return render(request, "import.html", active="sites", error=str(exc))
        finally:
            temp.unlink(missing_ok=True)

        with db() as database:
            count = database.import_sites(list(sites), replace=bool(replace))
        return _redirect("/sites", message=f"Imported {count} sites")

    # ---- pagespeed -----------------------------------------------------

    @app.get("/pagespeed", response_class=HTMLResponse)
    async def pagespeed(
        request: Request,
        sort: str = Query("tested_at"),
        dir: str = Query("desc"),
        domain: str = Query(""),
        strategy: str = Query(""),
        run_id: str = Query(""),
        view: str = Query("paired"),
        message: str = "",
        error: str = "",
    ):
        with db() as database:
            selected_run = int(run_id) if str(run_id).isdigit() else None
            pairs = database.pagespeed_pairs(
                run_id=selected_run,
                domain=domain or None,
                sort=sort,
                direction=dir,
                limit=500,
            )
            results = database.pagespeed_results(
                run_id=selected_run,
                domain=domain or None,
                strategy=strategy or None,
                sort=sort,
                direction=dir,
                limit=1000,
            )
            # Every configured site belongs in the filter, not just the ones
            # that happen to have results -- otherwise a site that has never
            # been tested is invisible, which is the thing worth noticing.
            tested = set(database.pagespeed_domains())
            configured = [site.domain for site in database.list_sites()]
            domains = [
                {"domain": name, "tested": name in tested} for name in configured
            ]
            # Anything with results but no longer configured still gets listed,
            # so its history stays reachable.
            domains += [
                {"domain": name, "tested": True}
                for name in sorted(tested - set(configured))
            ]

            return render(
                request,
                "pagespeed.html",
                active="pagespeed",
                results=results,
                pairs=pairs,
                view="flat" if view == "flat" else "paired",
                missing=[p for p in pairs if not p["mobile"] or not p["desktop"]],
                runs=database.pagespeed_runs(30),
                domains=domains,
                tested_count=len(tested & set(configured)),
                site_count=len(configured),
                sort=sort,
                dir=dir,
                domain=domain,
                strategy=strategy,
                run_id=run_id,
                configured=bool(current_settings().pagespeed_api_key),
                message=message,
                error=error,
            )

    # ---- schedules -----------------------------------------------------

    @app.get("/schedules", response_class=HTMLResponse)
    async def schedules(request: Request, message: str = "", error: str = ""):
        settings = current_settings()
        with db() as database:
            rows = [dict(row) for row in database.list_schedules()]
        for row in rows:
            row["description"] = describe(row["cron"])
        return render(
            request,
            "schedules.html",
            active="schedules",
            schedules=rows,
            tz=settings.timezone,
            message=message,
            error=error,
        )

    @app.post("/schedules/save")
    async def schedule_save(
        schedule_id: str = Form(""),
        name: str = Form(""),
        kind: str = Form("css_check"),
        cron: str = Form(""),
        cron_custom: str = Form(""),
        enabled: str = Form(""),
    ):
        name = name.strip() or "Untitled schedule"
        # The dropdown posts a ready-made expression; "Custom schedule" posts
        # the sentinel and the typed value comes through separately. Reading
        # both means the form still works with JavaScript disabled.
        cron = (cron_custom.strip() or cron).strip()
        if cron == "custom":
            return _redirect(
                "/schedules", error="Enter a custom schedule, or pick one from the list"
            )
        if kind not in {"css_check", "pagespeed"}:
            kind = "css_check"
        try:
            parse_cron(cron)
        except CronError as exc:
            return _redirect("/schedules", error=str(exc))

        with db() as database:
            if str(schedule_id).isdigit():
                identifier = int(schedule_id)
                database.update_schedule(
                    identifier, name=name, kind=kind, cron=cron, enabled=bool(enabled)
                )
            else:
                identifier = database.create_schedule(
                    name=name, kind=kind, cron=cron, enabled=bool(enabled)
                )
            scheduler.reschedule(database, identifier, cron)
        return _redirect("/schedules", message=f"Saved “{name}”")

    @app.post("/schedules/{schedule_id}/delete")
    async def schedule_delete(schedule_id: int):
        with db() as database:
            database.delete_schedule(schedule_id)
        return _redirect("/schedules", message="Schedule removed")

    @app.post("/schedules/{schedule_id}/toggle")
    async def schedule_toggle(schedule_id: int):
        with db() as database:
            row = database.get_schedule(schedule_id)
            if row is not None:
                enabled = not row["enabled"]
                database.update_schedule(
                    schedule_id,
                    name=row["name"],
                    kind=row["kind"],
                    cron=row["cron"],
                    enabled=enabled,
                )
                scheduler.reschedule(database, schedule_id, row["cron"])
        return _redirect("/schedules")

    @app.post("/schedules/{schedule_id}/run")
    async def schedule_run_now(schedule_id: int):
        with db() as database:
            row = database.get_schedule(schedule_id)
        if row is None:
            return _redirect("/schedules", error="No such schedule")
        manager.refresh_settings(current_settings())
        if row["kind"] == "pagespeed":
            started, message = await manager.trigger_pagespeed(trigger="manual")
        else:
            started, message = await manager.trigger(trigger="manual")
        return _redirect(
            "/schedules", **({"message": message} if started else {"error": message})
        )

    # ---- settings ------------------------------------------------------

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_page(request: Request, message: str = "", error: str = ""):
        settings = current_settings()
        with db() as database:
            stored = database.get_settings()
            configured = len(resolve_sites(settings, database))
        return render(
            request,
            "settings.html",
            active="settings",
            settings=settings,
            stored=stored,
            configured=configured,
            message=message,
            error=error,
        )

    @app.post("/settings/save")
    async def settings_save(
        telegram_bot_token: str = Form(""),
        telegram_chat_id: str = Form(""),
        pagespeed_api_key: str = Form(""),
        site_concurrency: str = Form(""),
        page_concurrency: str = Form(""),
        asset_concurrency: str = Form(""),
        request_timeout: str = Form(""),
        max_retries: str = Form(""),
        max_pages_per_site: str = Form(""),
        user_agent: str = Form(""),
        pagespeed_strategies: str = Form(""),
        pagespeed_concurrency: str = Form(""),
    ):
        values = {
            "telegram_bot_token": telegram_bot_token,
            "telegram_chat_id": telegram_chat_id,
            "pagespeed_api_key": pagespeed_api_key,
            "site_concurrency": site_concurrency,
            "page_concurrency": page_concurrency,
            "asset_concurrency": asset_concurrency,
            "request_timeout": request_timeout,
            "max_retries": max_retries,
            "max_pages_per_site": max_pages_per_site,
            "user_agent": user_agent,
            "pagespeed_strategies": pagespeed_strategies,
            "pagespeed_concurrency": pagespeed_concurrency,
        }
        for key in ("site_concurrency", "page_concurrency", "asset_concurrency",
                    "max_retries", "max_pages_per_site", "pagespeed_concurrency"):
            raw = values[key].strip()
            if raw and not raw.lstrip("-").isdigit():
                return _redirect("/settings", error=f"{key} needs to be a whole number")
        with db() as database:
            database.set_settings(values)
        manager.refresh_settings(current_settings())
        return _redirect("/settings", message="Settings saved")

    @app.post("/settings/test-telegram")
    async def test_telegram():
        from ..notifier import TelegramNotifier

        settings = current_settings()
        if not settings.telegram_enabled:
            return _redirect(
                "/settings", error="Add a bot token and chat ID first"
            )
        notifier = TelegramNotifier(
            settings.telegram_bot_token,
            settings.telegram_chat_id,
            timeout=settings.request_timeout,
            max_retries=1,
        )
        sent = await notifier.send(
            ["✅ <b>Site Monitor</b>\nTest message from the dashboard."]
        )
        if sent:
            return _redirect("/settings", message="Test message sent")
        return _redirect(
            "/settings", error="Could not send — check the token and chat ID"
        )

    # ---- exports -------------------------------------------------------

    def _download(body, filename: str, media_type: str) -> Response:
        return Response(
            content=body,
            media_type=media_type,
            headers={"Content-Disposition": f'attachment; filename="{filename}"'},
        )

    @app.get("/export/{report}.{fmt}")
    async def export(
        report: str,
        fmt: str,
        run_id: str = Query(""),
        domain: str = Query(""),
        strategy: str = Query(""),
        sort: str = Query("tested_at"),
        dir: str = Query("desc"),
        source: str = Query("all"),
    ):
        if fmt not in {"csv", "xlsx"}:
            return JSONResponse({"error": "unknown format"}, status_code=404)

        with db() as database:
            if report == "pagespeed":
                rows = database.pagespeed_results(
                    run_id=int(run_id) if str(run_id).isdigit() else None,
                    domain=domain or None,
                    strategy=strategy or None,
                    sort=sort,
                    direction=dir,
                    limit=10000,
                )
                columns, title = PAGESPEED_COLUMNS, "PageSpeed"
            elif report == "broken":
                if str(run_id).isdigit():
                    rows = database.broken_assets_for_run(int(run_id))
                else:
                    latest = database.latest_run()
                    rows = (
                        database.broken_assets_for_run(latest["id"]) if latest else []
                    )
                columns, title = BROKEN_COLUMNS, "Broken CSS"
            elif report == "runs":
                # Respect whichever tab the download was started from.
                rows = database.runs_by_source(source or "all", limit=1000)
                columns, title = RUNS_COLUMNS, "Runs"
            else:
                return JSONResponse({"error": "unknown report"}, status_code=404)

        name = f"site-monitor-{report}-{timestamp()}.{fmt}"
        if fmt == "csv":
            return _download(to_csv(rows, columns), name, "text/csv; charset=utf-8")
        return _download(
            to_xlsx(rows, columns, sheet_title=title),
            name,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

    return app
