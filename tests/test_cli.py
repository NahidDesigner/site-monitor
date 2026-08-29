"""CLI wiring: exit codes, alert suppression, persistence."""

from __future__ import annotations

import site_monitor.cli as cli
from fixtures import BROKEN_CSS, PAGE_URL
from site_monitor.crawler import RunResult, SiteResult
from site_monitor.db import Database
from site_monitor.elementor import AssetResult, PageResult


def _sites_yaml(tmp_path):
    path = tmp_path / "sites.yaml"
    path.write_text(
        "sites:\n  - domain: dvlfirm.com\n"
        "    sitemap: https://dvlfirm.com/sitemap_index.xml\n",
        encoding="utf-8",
    )
    return path


def _env(tmp_path, extra: str = ""):
    env = tmp_path / ".env"
    env.write_text(
        f"SITES_FILE={_sites_yaml(tmp_path)}\n"
        f"DATABASE_PATH={tmp_path / 'monitor.db'}\n" + extra,
        encoding="utf-8",
    )
    return env


def _broken_run() -> RunResult:
    return RunResult(
        sites=[
            SiteResult(
                domain="dvlfirm.com",
                sitemap="https://dvlfirm.com/sitemap_index.xml",
                pages_found=1,
                pages=[
                    PageResult(
                        url=PAGE_URL,
                        status_code=200,
                        assets_checked=15,
                        broken=(
                            AssetResult(
                                url=BROKEN_CSS,
                                status_code=404,
                                content_type="text/html",
                                ok=False,
                                reason="HTTP 404 (content-type: text/html)",
                                elapsed_ms=10,
                            ),
                        ),
                    )
                ],
            )
        ]
    )


def _healthy_run() -> RunResult:
    return RunResult(
        sites=[
            SiteResult(
                domain="dvlfirm.com",
                sitemap="https://dvlfirm.com/sitemap_index.xml",
                pages_found=1,
                pages=[
                    PageResult(
                        url=PAGE_URL, status_code=200, assets_checked=15, broken=()
                    )
                ],
            )
        ]
    )


def _patch_run(monkeypatch, run: RunResult):
    async def fake(settings, *, transport=None, on_site_complete=None):
        for site in run.sites:
            if on_site_complete is not None:
                on_site_complete(site)
        return run

    monkeypatch.setattr(cli, "run_checks", fake)


def test_check_exits_zero_and_sends_nothing_when_healthy(
    tmp_path, monkeypatch, capsys
):
    _patch_run(monkeypatch, _healthy_run())
    sent: list = []
    monkeypatch.setattr(
        cli.TelegramNotifier, "send", lambda self, messages: sent.append(messages)
    )

    code = cli.main(["--env-file", str(_env(tmp_path)), "check"])

    assert code == cli.EXIT_OK
    assert sent == []
    assert "All Elementor stylesheets resolved correctly." in capsys.readouterr().out


def test_check_exits_one_and_alerts_when_broken(tmp_path, monkeypatch):
    _patch_run(monkeypatch, _broken_run())
    sent: list[list[str]] = []

    async def fake_send(self, messages):
        sent.append(messages)
        return len(messages)

    monkeypatch.setattr(cli.TelegramNotifier, "send", fake_send)
    env = _env(
        tmp_path, "TELEGRAM_BOT_TOKEN=token\nTELEGRAM_CHAT_ID=-100\n"
    )
    code = cli.main(["--env-file", str(env), "check"])

    assert code == cli.EXIT_BROKEN
    assert len(sent) == 1
    assert "post-39321.css?ver=1787903551" in sent[0][0]


def test_no_alert_flag_prints_instead_of_sending(tmp_path, monkeypatch, capsys):
    _patch_run(monkeypatch, _broken_run())

    async def fail(self, messages):  # pragma: no cover
        raise AssertionError("should not send")

    monkeypatch.setattr(cli.TelegramNotifier, "send", fail)

    code = cli.main(["--env-file", str(_env(tmp_path)), "check", "--no-alert"])
    out = capsys.readouterr().out

    assert code == cli.EXIT_BROKEN
    assert "--- telegram message ---" in out
    assert "post-39321.css" in out


def test_check_records_the_run_in_sqlite(tmp_path, monkeypatch):
    _patch_run(monkeypatch, _broken_run())

    cli.main(["--env-file", str(_env(tmp_path)), "check", "--no-alert"])

    with Database(tmp_path / "monitor.db") as database:
        run_row = database.recent_runs(1)[0]
        assert run_row["status"] == "completed"
        assert run_row["broken_assets"] == 1
        assert database.broken_assets_for_run(run_row["id"])[0]["asset_url"] == BROKEN_CSS


def test_bad_config_exits_with_the_error_code(tmp_path):
    env = tmp_path / ".env"
    env.write_text(f"SITES_FILE={tmp_path / 'missing.yaml'}\n", encoding="utf-8")

    assert cli.main(["--env-file", str(env), "check"]) == cli.EXIT_ERROR


def test_check_site_rejects_an_unknown_domain(tmp_path):
    code = cli.main(
        ["--env-file", str(_env(tmp_path)), "check-site", "not-configured.com"]
    )

    assert code == cli.EXIT_ERROR


def test_history_lists_recorded_runs(tmp_path, monkeypatch, capsys):
    _patch_run(monkeypatch, _broken_run())
    env = _env(tmp_path)
    cli.main(["--env-file", str(env), "check", "--no-alert"])

    code = cli.main(["--env-file", str(env), "history"])

    assert code == cli.EXIT_OK
    assert "completed" in capsys.readouterr().out


def test_no_subcommand_prints_help(capsys):
    assert cli.main([]) == cli.EXIT_ERROR
    assert "usage: site-monitor" in capsys.readouterr().out
