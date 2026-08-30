"""Command line entry point. `python -m site_monitor check` is the cron target."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

from pathlib import Path

from .config import ConfigError, Settings, _env_int
from .crawler import RunResult, check_site, run_checks
from .db import Database
from .discovery import (
    SiteProbe,
    discover_many,
    probe_sitemap,
    read_domains,
    render_sites_yaml,
    sample_page,
)
from .elementor import check_page
from .http import Fetcher, build_client
from .notifier import TelegramNotifier, format_alert
from .runner import deliver_alert, execute_run, resolve_sites

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
        sites = resolve_sites(settings, database)
        if not sites:
            log.error(
                "no sites configured -- add them in the dashboard, or run "
                "'site-monitor sites import <file.yaml>'"
            )
            return EXIT_ERROR

        log.info("checking %s sites", len(sites))
        run, _ = await execute_run(settings, database, sites)

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

    sent = await deliver_alert(settings, run)
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
    with Database(settings.database_path) as database:
        configured = resolve_sites(settings, database)
    matches = [site for site in configured if site.domain == args.domain]
    if not matches:
        known = ", ".join(site.domain for site in configured) or "(none)"
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



def _probe_line(probe: SiteProbe) -> str:
    """One aligned status line per site."""
    if not probe.ok:
        return f"  ✗ {probe.domain:<32} {probe.error}"
    elementor = {
        True: f"{probe.sample_css_count} Elementor CSS on sample",
        False: "no Elementor CSS on sample",
        None: "sample not fetched",
    }[probe.uses_elementor]
    return (
        f"  ✓ {probe.domain:<32} {probe.pages_found:>5} pages  "
        f"[{probe.source}]  {elementor}"
    )


async def _with_fetcher(settings: Settings):
    client = build_client(
        user_agent=settings.user_agent,
        timeout=settings.request_timeout,
        max_connections=max(settings.page_concurrency, settings.asset_concurrency),
    )
    return client, Fetcher(
        client, max_retries=settings.max_retries, backoff=settings.retry_backoff
    )


async def cmd_discover(settings: Settings, args: argparse.Namespace) -> int:
    """Resolve each domain's sitemap and emit a ready-to-use sites.yaml.

    robots.txt is consulted first, then the usual SEO-plugin paths.
    """
    if args.from_file:
        domains = read_domains(Path(args.from_file).read_text(encoding="utf-8"))
    elif args.domains:
        domains = read_domains(" ".join(args.domains))
    else:
        domains = read_domains(sys.stdin.read())

    if not domains:
        print("no domains given", file=sys.stderr)
        return EXIT_ERROR

    print(f"Resolving sitemaps for {len(domains)} domain(s)...\n", file=sys.stderr)
    client, fetcher = await _with_fetcher(settings)
    async with client:
        probes = await discover_many(
            fetcher,
            domains,
            concurrency=settings.site_concurrency,
            sample_pages=0 if args.no_sample else 1,
        )

    for probe in probes:
        print(_probe_line(probe), file=sys.stderr)

    resolved = [probe for probe in probes if probe.ok]
    no_elementor = [probe for probe in resolved if probe.uses_elementor is False]
    print(
        f"\n{len(resolved)}/{len(probes)} resolved"
        + (f", {len(no_elementor)} with no Elementor CSS on the sampled page" if no_elementor else ""),
        file=sys.stderr,
    )

    body = render_sites_yaml(probes)
    if args.output:
        target = Path(args.output)
        if target.exists() and not args.force:
            print(
                f"\n{target} already exists; pass --force to overwrite",
                file=sys.stderr,
            )
            return EXIT_ERROR
        target.write_text(body, encoding="utf-8")
        print(f"\nwrote {target}", file=sys.stderr)
    else:
        print(body, end="")

    return EXIT_OK if len(resolved) == len(probes) else EXIT_BROKEN


async def cmd_validate(settings: Settings, args: argparse.Namespace) -> int:
    """Pre-flight the configured sites without running a full check.

    Confirms every sitemap resolves, reports how many pages each will crawl,
    and samples one page per site to confirm Elementor stylesheets are found.
    """
    with Database(settings.database_path) as database:
        sites = resolve_sites(settings, database)
    if not sites:
        print("no enabled sites configured", file=sys.stderr)
        return EXIT_ERROR

    print(f"Validating {len(sites)} site(s)\n")

    semaphore = asyncio.Semaphore(settings.site_concurrency)
    client, fetcher = await _with_fetcher(settings)

    async def probe(site) -> SiteProbe:
        async with semaphore:
            result = SiteProbe(
                domain=site.domain,
                sitemap=site.sitemap,
                source="pages" if site.has_explicit_pages else "sitemap",
            )
            try:
                if site.has_explicit_pages:
                    result.pages_found = len(site.pages)
                    if not args.no_sample:
                        (
                            result.sample_url,
                            result.sample_css_count,
                        ) = await sample_page(fetcher, site.pages)
                else:
                    count, sample, css_count = await probe_sitemap(
                        fetcher, site.sitemap, sample_pages=0 if args.no_sample else 1
                    )
                    result.pages_found = count
                    result.sample_url = sample
                    result.sample_css_count = css_count
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                return result
            if result.pages_found == 0:
                result.error = "no pages to check"
            return result

    async with client:
        probes = list(await asyncio.gather(*(probe(site) for site in sites)))

    for result in probes:
        print(_probe_line(result))

    failed = [result for result in probes if not result.ok]
    total_pages = sum(result.pages_found for result in probes)
    print(f"\n{len(probes) - len(failed)}/{len(probes)} sites OK, {total_pages} pages per run")

    if total_pages > 5000:
        print(
            f"  note: {total_pages} pages per run is a lot -- consider max_pages "
            "per site, or a less frequent cron"
        )
    return EXIT_BROKEN if failed else EXIT_OK


async def cmd_sites(settings: Settings, args: argparse.Namespace) -> int:
    """List, import or remove sites in the database."""
    from .config import load_sites

    with Database(settings.database_path) as database:
        if args.sites_action == "import":
            source = Path(args.file)
            if not source.is_file():
                print(f"no such file: {source}", file=sys.stderr)
                return EXIT_ERROR
            imported = database.import_sites(
                list(load_sites(source)), replace=args.replace
            )
            total = database.site_count()
            print(f"imported {imported} site(s); {total} now configured")
            return EXIT_OK

        if args.sites_action == "remove":
            if database.delete_site(args.domain):
                print(f"removed {args.domain}")
                return EXIT_OK
            print(f"no such site: {args.domain}", file=sys.stderr)
            return EXIT_ERROR

        sites = database.list_sites()
        if not sites:
            print("no sites configured")
            return EXIT_OK
        print(f"{'domain':<36} {'pages':>6}  source")
        for site in sites:
            source = site.sitemap or "curated list"
            flag = "" if site.enabled else "  (disabled)"
            count = len(site.pages) if site.has_explicit_pages else 0
            print(f"{site.domain:<36} {count:>6}  {source}{flag}")
        print(f"\n{len(sites)} site(s)")
    return EXIT_OK


async def cmd_serve(settings: Settings, args: argparse.Namespace) -> int:
    """Run the dashboard."""
    import uvicorn

    from .webapp import create_app

    if not settings.dashboard_password:
        log.error(
            "DASHBOARD_PASSWORD is not set -- refusing to serve an unprotected "
            "dashboard that can edit the site list and trigger runs"
        )
        return EXIT_ERROR

    config = uvicorn.Config(
        create_app(settings),
        host=args.host,
        port=args.port,
        log_level=settings.log_level.lower(),
        access_log=False,
    )
    await uvicorn.Server(config).serve()
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

    discover = sub.add_parser(
        "discover",
        help="resolve sitemaps for a list of domains and emit sites.yaml",
    )
    discover.add_argument("domains", nargs="*", help="domains; omit to read stdin")
    discover.add_argument("--from-file", help="read domains from a file, one per line")
    discover.add_argument("-o", "--output", help="write sites.yaml here instead of stdout")
    discover.add_argument("--force", action="store_true", help="overwrite --output")
    discover.add_argument(
        "--no-sample",
        action="store_true",
        help="skip the per-site Elementor sample fetch (faster)",
    )
    discover.set_defaults(handler=cmd_discover)

    validate = sub.add_parser(
        "validate", help="pre-flight the configured sites without a full check"
    )
    validate.add_argument("--no-sample", action="store_true")
    validate.set_defaults(handler=cmd_validate)

    sites_cmd = sub.add_parser("sites", help="manage the configured site list")
    sites_sub = sites_cmd.add_subparsers(dest="sites_action")
    sites_sub.add_parser("list", help="list configured sites")
    imp = sites_sub.add_parser("import", help="import sites from a YAML file")
    imp.add_argument("file")
    imp.add_argument(
        "--replace", action="store_true", help="delete existing sites first"
    )
    rm = sites_sub.add_parser("remove", help="remove one site")
    rm.add_argument("domain")
    sites_cmd.set_defaults(handler=cmd_sites, sites_action="list")

    serve = sub.add_parser("serve", help="run the web dashboard")
    serve.add_argument("--host", default=os.getenv("HOST", "0.0.0.0"))
    # Follow the platform's PORT if it sets one. Coolify, Railway and friends
    # pick a port, route their proxy to it, and expect the app to listen there;
    # hardcoding a different one is the usual cause of a 502 in front of a
    # perfectly healthy container.
    serve.add_argument("--port", type=int, default=_env_int("PORT", 8080))
    serve.set_defaults(handler=cmd_serve)

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
            if args.handler not in (cmd_check_url, cmd_discover):
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
