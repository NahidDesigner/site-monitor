"""The MCP server: what it exposes, and that it is not exposed unguarded."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from site_monitor.config import Settings
from site_monitor.db import Database
from site_monitor.mcp_server import build_mcp_server
from site_monitor.runner import RunManager
from site_monitor.webapp import create_app

TOKEN = "test-mcp-token"


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_path=tmp_path / "mcp.db",
        dashboard_password="pw",
        session_secret="secret",
        mcp_token=TOKEN,
        retry_backoff=0.0,
    )


@pytest.fixture
def server(settings):
    return build_mcp_server(settings, RunManager(settings), lambda: settings)


@pytest.fixture
def database(settings):
    with Database(settings.database_path) as db:
        yield db


async def call(server, tool: str, **args):
    """Invoke a tool the way a client would, and parse what comes back."""
    result = await server.call_tool(tool, args)
    structured = getattr(result, "structured_content", None)
    if structured:
        return structured
    for block in getattr(result, "content", []) or []:
        text = getattr(block, "text", None)
        if text is not None:
            return json.loads(text)
    return result


# -- exposure -----------------------------------------------------------------


def test_mcp_is_not_mounted_without_a_token(tmp_path):
    app = create_app(
        Settings(database_path=tmp_path / "a.db", dashboard_password="pw",
                 session_secret="s")
    )

    assert not any(getattr(r, "path", "") == "/mcp" for r in app.routes)


def test_mcp_is_mounted_when_a_token_exists(settings):
    app = create_app(settings)

    assert any(getattr(r, "path", "") == "/mcp" for r in app.routes)


@pytest.mark.parametrize(
    "headers",
    [{}, {"Authorization": "Bearer wrong"}, {"Authorization": TOKEN}],
)
def test_mcp_rejects_anything_but_the_right_bearer_token(settings, headers):
    with TestClient(create_app(settings)) as client:
        response = client.post(
            "/mcp/",
            headers={**headers, "Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 401
    assert response.headers.get("www-authenticate") == "Bearer"


def test_mcp_auth_does_not_accept_a_dashboard_session(settings):
    """The two credentials are separate on purpose."""
    with TestClient(create_app(settings)) as client:
        client.post("/login", data={"password": "pw"})
        response = client.post(
            "/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )

    assert response.status_code == 401


async def test_every_tool_is_registered(server):
    names = {tool.name for tool in await server.list_tools()}

    assert names == {
        "list_sites", "get_site", "add_site", "set_site_enabled", "remove_site",
        "run_check", "run_pagespeed", "get_run_status", "current_breakages",
        "list_reports", "get_report", "pagespeed_results", "list_schedules",
        "create_schedule", "delete_schedule", "get_status",
    }


async def test_destructive_tools_are_annotated_as_such(server):
    """A client should be able to treat "remove" differently from "list"."""
    tools = {t.name: t for t in await server.list_tools()}

    assert tools["remove_site"].annotations.destructive_hint
    assert tools["delete_schedule"].annotations.destructive_hint
    assert tools["list_sites"].annotations.read_only_hint
    assert not tools["run_check"].annotations.destructive_hint


# -- reading ------------------------------------------------------------------


async def test_list_sites_reports_counts(server, database):
    database.upsert_site(domain="a.com", pages=["https://a.com/", "https://a.com/x/"])
    database.upsert_site(domain="b.com", pages=["https://b.com/"], enabled=False)

    result = await call(server, "list_sites")

    assert result["count"] == 2
    assert result["total_pages"] == 3
    assert (await call(server, "list_sites", enabled_only=True))["count"] == 1


def _record_breakage(database, run_id, domain, count=1):
    """Persist a finding through the real path, parents and all."""
    from site_monitor.crawler import SiteResult
    from site_monitor.elementor import AssetResult, PageResult

    broken = tuple(
        AssetResult(
            url=f"https://{domain}/wp-content/uploads/elementor/css/post-{i}.css?ver=1",
            status_code=404, content_type="text/html", ok=False,
            reason="HTTP 404 (content-type: text/html)", elapsed_ms=10,
        )
        for i in range(count)
    )
    database.record_site_result(
        run_id,
        SiteResult(
            domain=domain, sitemap="", pages_found=1,
            pages=[PageResult(url=f"https://{domain}/", status_code=200,
                              assets_checked=15, broken=broken)],
        ),
    )


async def test_get_site_includes_breakage_history(server, database):
    database.upsert_site(domain="a.com", pages=["https://a.com/"])
    _record_breakage(database, database.start_run(), "a.com")

    result = await call(server, "get_site", domain="a.com")

    assert result["pages"] == ["https://a.com/"]
    assert result["breakage_history"][0]["status_code"] == 404


async def test_get_site_on_an_unknown_domain_explains_itself(server):
    assert "error" in await call(server, "get_site", domain="nope.com")


async def test_current_breakages_groups_by_site(server, database):
    run_id = database.start_run()
    _record_breakage(database, run_id, "a.com", count=2)
    _record_breakage(database, run_id, "b.com", count=1)
    database.finish_run(run_id, status="completed", sites_checked=2,
                        pages_checked=3, assets_checked=9, broken_assets=3)

    result = await call(server, "current_breakages")

    assert result["sites_affected"] == 2
    assert len(result["by_site"]["a.com"]) == 2


async def test_current_breakages_before_any_run(server):
    assert "message" in await call(server, "current_breakages")


async def test_list_reports_filters_by_source(server, database):
    database.start_run(trigger="schedule:Nightly", scope="2 sites")
    database.start_run(trigger="site:a.com", scope="a.com")

    scheduled = await call(server, "list_reports", source="scheduled")

    assert len(scheduled["reports"]) == 1
    assert scheduled["reports"][0]["trigger"] == "schedule:Nightly"
    assert scheduled["counts_by_source"]["all"] == 2


async def test_list_reports_rejects_an_unknown_source(server):
    assert "error" in await call(server, "list_reports", source="../etc")


async def test_pagespeed_results_report_coverage_and_share_links(server, database):
    from site_monitor.pagespeed import PageSpeedResult

    database.upsert_site(domain="tested.com", pages=["https://tested.com/"])
    database.upsert_site(domain="never.com", pages=["https://never.com/"])
    run_id = database.start_pagespeed_run()
    for strategy, score in (("mobile", 51.0), ("desktop", 82.0)):
        database.record_pagespeed_result(
            run_id,
            PageSpeedResult(domain="tested.com", url="https://tested.com/",
                            strategy=strategy, performance=score),
        )

    result = await call(server, "pagespeed_results")

    assert result["coverage"]["never_tested"] == ["never.com"]
    row = result["results"][0]
    assert row["mobile"]["performance"] == 51.0
    assert row["desktop"]["performance"] == 82.0
    assert "pagespeed.web.dev" in row["mobile"]["report_url"]


# -- writing ------------------------------------------------------------------


async def test_add_site_creates_it(server, database):
    result = await call(
        server, "add_site", domain="New.COM ", pages=["https://new.com/"]
    )

    assert result["saved"] == "new.com"
    assert database.get_site("new.com") is not None


async def test_add_site_rejects_relative_urls(server, database):
    result = await call(server, "add_site", domain="a.com", pages=["/about/"])

    assert "not absolute" in result["error"]
    assert database.site_count() == 0


async def test_add_site_needs_pages_or_a_sitemap(server):
    assert "error" in await call(server, "add_site", domain="a.com")


async def test_pause_and_remove(server, database):
    database.upsert_site(domain="a.com", pages=["https://a.com/"])

    await call(server, "set_site_enabled", domain="a.com", enabled=False)
    assert not database.get_site("a.com")["enabled"]

    assert (await call(server, "remove_site", domain="a.com"))["removed"]
    assert database.site_count() == 0


async def test_create_schedule_validates_and_arms(server, database):
    result = await call(
        server, "create_schedule", name="Six hourly", cron="0 */6 * * *"
    )

    assert result["in_words"] == "Every 6 hours"
    assert result["next_run_at"] is not None
    assert database.get_schedule(result["id"])["next_run_at"] is not None


async def test_create_schedule_rejects_a_bad_expression(server, database):
    result = await call(server, "create_schedule", name="x", cron="99 * * * *")

    assert "out of range" in result["error"]
    assert database.list_schedules() == []


async def test_create_schedule_constrains_the_kind(server):
    assert "error" in await call(
        server, "create_schedule", name="x", cron="0 * * * *", kind="rm -rf"
    )


# -- triggering ---------------------------------------------------------------


async def test_run_check_on_an_unknown_site_is_reported(server):
    assert "error" in await call(server, "run_check", domain="nope.com")


async def test_run_check_without_sites_does_not_pretend_to_start(server):
    result = await call(server, "run_check")

    assert result["started"] is False
    assert "No sites" in result["message"]


async def test_get_run_status_reports_both_jobs(server):
    result = await call(server, "get_run_status")

    assert result["css_check"]["state"] == "idle"
    assert result["pagespeed"]["state"] == "idle"


async def test_get_status_never_returns_secrets(server, settings):
    """It reports whether credentials exist, never what they are."""
    result = await call(server, "get_status")
    body = json.dumps(result)

    assert result["telegram_configured"] is False
    assert TOKEN not in body
    assert "pw" not in [v for v in result.values() if isinstance(v, str)]


# -- reaching it under a real hostname ----------------------------------------


def _initialize(client, host: str, token: str | None = TOKEN):
    headers = {
        "Host": host,
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return client.post(
        "/mcp/",
        headers=headers,
        json={
            "jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18", "capabilities": {},
                "clientInfo": {"name": "t", "version": "1"},
            },
        },
    )


def test_mcp_answers_under_a_real_hostname(settings):
    """The SDK's DNS-rebinding protection defaults to 127.0.0.1 only, so behind
    a domain every authenticated request came back 421 Misdirected Request.
    Testing against localhost cannot catch this -- the Host header matches there.
    """
    with TestClient(create_app(settings)) as client:
        response = _initialize(client, "bosseo.vibecodingfield.com")

    assert response.status_code != 421, "rejected as a misdirected request"
    assert response.status_code == 200


def test_mcp_still_answers_under_localhost(settings):
    with TestClient(create_app(settings)) as client:
        assert _initialize(client, "127.0.0.1").status_code == 200


def test_auth_is_still_required_under_a_real_hostname(settings):
    """Loosening host checking must not loosen authentication."""
    with TestClient(create_app(settings)) as client:
        response = _initialize(client, "bosseo.vibecodingfield.com", token=None)

    assert response.status_code == 401


def test_hosts_can_be_pinned(tmp_path):
    """For anyone who wants the host check back, narrowed to their own domain."""
    pinned = Settings(
        database_path=tmp_path / "p.db", dashboard_password="pw",
        session_secret="s", mcp_token=TOKEN,
        mcp_allowed_hosts=("bosseo.vibecodingfield.com",),
    )
    with TestClient(create_app(pinned)) as client:
        assert _initialize(client, "bosseo.vibecodingfield.com").status_code == 200
        assert _initialize(client, "attacker.example.com").status_code == 421
