"""Command line entry point. `python -m site_monitor check` is the cron target."""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import ConfigError, Settings, load_sites
from .crawler import RunResult, check_site, run_checks
from .db import Database
from .elementor import check_page
from .http import Fetcher, build_client
from .notifier import TelegramNotifier, format_alert

log = logging.getLogger("site_monitor")

EXIT_OK = 0
EXIT_BROKEN = 1
EXIT_ERROR = 2


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S%z",
        stream=sys.stderr,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def print_summary(run: RunResult) -> None:
    """Human-readable summary on stdout, for cron logs."""
    print(
        f"\nChecked {len(run.sites)} site(s), {run.pages_checked} page(s), "
        f"{run.assets_checked} Elementor stylesheet(s) in {run.duration_ms / 1000:.1f}s"
    )
    if not run.has_findings:
        print("All Elementor stylesheets resolved correctly.")
        return

    print(f"\n{run.broken_asset_count} broken stylesheet(s):\n")
    for site in run.sites_with_findings:
        if site.error:
            print(f"  {site.domain}: ERROR {site.error}")
            continue
        print(f"  {site.domain} ({site.broken_asset_count} broken)")
        for page in site.broken_pages:
            print(f"    {page.url}")
            if page.error:
                print(f"      ! {page.error}")
            for asset in page.broken:
                print(f"      - {asset.url}  [{asset.reason}]")


async def cmd_check(settings: Settings, args: argparse.Namespace) -> int:
    """Full run: crawl every site, persist, alert if anything is broken."""
    with Database(settings.database_path) as database:
        run_id = database.start_run()
        log.info("run %s started (%s sites)", run_id, len(settings.sites))

        try:
            run = await run_checks(
                settings,
                on_site_complete=lambda result: database.record_site_result(
                    run_id, result
                ),
            )
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

    print_summary(run)

    messages = format_alert(run)
    if not messages:
        log.info("nothing broken; no alert sent")
        return EXIT_OK

    if args.no_alert or settings.dry_run:
        log.info("alert suppressed (dry run); would have sent %s message(s)", len(messages))
        for message in messages:
            print("\n--- telegram message ---")
            print(message)
        return EXIT_BROKEN

    if not settings.telegram_enabled:
        log.warning(
            "breakages found but TELEGRAM_BOT_TOKEN/TELEGRAM_CHAT_ID are not set"
        )
        return EXIT_BROKEN

    notifier = TelegramNotifier(
        settings.telegram_bot_token,
        settings.telegram_chat_id,
        timeout=settings.request_timeout,
        max_retries=settings.max_retries,
    )
    sent = await notifier.send(messages)
    log.info("sent %s/%s telegram message(s)", sent, len(messages))
    return EXIT_BROKEN


async def cmd_check_url(settings: Settings, args: argparse.Namespace) -> int:
    """Check a single page. The quickest way to confirm a suspected breakage."""
    asset_semaphore = asyncio.Semaphore(settings.asset_concurrency)
    async with build_client(
        user_agent=settings.user_agent,
        timeout=settings.request_timeout,
        max_connections=settings.asset_concurrency,
    ) as client:
        fetcher = Fetcher(
            client,
            max_retries=settings.max_retries,
            backoff=settings.retry_backoff,
        )
        result = await check_page(fetcher, args.url, asset_semaphore=asset_semaphore)

    print(f"{result.url}  (HTTP {result.status_code})")
    print(f"  Elementor stylesheets referenced: {result.assets_checked}")
    if result.error:
        print(f"  ERROR: {result.error}")
        return EXIT_BROKEN
    if not result.broken:
        print("  All resolved correctly.")
        return EXIT_OK
    print(f"  {len(result.broken)} broken:")
    for asset in result.broken:
        print(f"    - {asset.url}  [{asset.reason}]")
    return EXIT_BROKEN


async def cmd_check_site(settings: Settings, args: argparse.Namespace) -> int:
    """Check one configured site by domain, without touching the others."""
    matches = [site for site in settings.sites if site.domain == args.domain]
    if not matches:
        known = ", ".join(site.domain for site in settings.sites) or "(none)"
        print(f"unknown site {args.domain!r}; configured sites: {known}", file=sys.stderr)
        return EXIT_ERROR

    async with build_client(
        user_agent=settings.user_agent,
        timeout=settings.request_timeout,
        max_connections=max(settings.page_concurrency, settings.asset_concurrency),
    ) as client:
        fetcher = Fetcher(
            client,
            max_retries=settings.max_retries,
            backoff=settings.retry_backoff,
        )
        result = await check_site(fetcher, matches[0], settings=settings)

    run = RunResult(sites=[result], duration_ms=result.duration_ms)
    print_summary(run)
    return EXIT_BROKEN if run.has_findings else EXIT_OK


async def cmd_history(settings: Settings, args: argparse.Namespace) -> int:
    """Show recent runs from the database."""
    with Database(settings.database_path) as database:
        rows = database.recent_runs(args.limit)
        if not rows:
            print("no runs recorded yet")
            return EXIT_OK
        print(f"{'id':>5}  {'started':25}  {'status':10}  {'pages':>6}  {'broken':>6}")
        for row in rows:
            print(
                f"{row['id']:>5}  {row['started_at']:25}  {row['status']:10}  "
                f"{row['pages_checked']:>6}  {row['broken_assets']:>6}"
            )
    return EXIT_OK


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="site-monitor",
        description="Detect Elementor stylesheets that stale cached HTML still references.",
    )
    parser.add_argument(
        "--env-file", default=".env", help="path to the .env file (default: .env)"
    )
    parser.add_argument("--log-level", help="override LOG_LEVEL")
    sub = parser.add_subparsers(dest="command")

    check = sub.add_parser("check", help="check every configured site (cron entry point)")
    check.add_argument(
        "--no-alert",
        action="store_true",
        help="print the Telegram message instead of sending it",
    )
    check.set_defaults(handler=cmd_check)

    check_url = sub.add_parser("check-url", help="check one page URL")
    check_url.add_argument("url")
    check_url.set_defaults(handler=cmd_check_url)

    check_site_cmd = sub.add_parser("check-site", help="check one configured site")
    check_site_cmd.add_argument("domain")
    check_site_cmd.set_defaults(handler=cmd_check_site)

    history = sub.add_parser("history", help="list recent runs")
    history.add_argument("--limit", type=int, default=10)
    history.set_defaults(handler=cmd_history)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "handler", None):
        parser.print_help()
        return EXIT_ERROR

    try:
        # check-url needs no sites.yaml, so tolerate its absence there.
        try:
            settings = Settings.from_env(args.env_file)
        except ConfigError:
            if args.handler is not cmd_check_url:
                raise
            settings = Settings()
    except ConfigError as exc:
        configure_logging(args.log_level or "INFO")
        log.error("%s", exc)
        return EXIT_ERROR

    configure_logging(args.log_level or settings.log_level)

    try:
        return asyncio.run(args.handler(settings, args))
    except KeyboardInterrupt:
        log.warning("interrupted")
        return EXIT_ERROR
    except Exception as exc:
        log.exception("run failed: %s", exc)
        return EXIT_ERROR


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
