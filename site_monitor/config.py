"""Runtime configuration, loaded from environment (.env) and sites.yaml."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml
from dotenv import load_dotenv

DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class ConfigError(RuntimeError):
    """Raised when configuration is missing or malformed."""


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from exc


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from exc


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class Site:
    """A single monitored WordPress site.

    Pages come from one of two sources: an explicit `pages` list, or a
    `sitemap` to walk. An explicit list is exact and cheap; a sitemap is
    broader but crawls whatever the SEO plugin happens to publish. When both
    are given the explicit list wins -- it is the more specific instruction --
    and the sitemap is kept only as documentation.
    """

    domain: str
    sitemap: str = ""
    pages: tuple[str, ...] = ()
    enabled: bool = True
    max_pages: int | None = None

    @property
    def key(self) -> str:
        return self.domain

    @property
    def has_explicit_pages(self) -> bool:
        return bool(self.pages)


@dataclass(frozen=True)
class Settings:
    """Everything tunable, sourced from the environment."""

    sites_file: Path = Path("sites.yaml")
    database_path: Path = Path("data/site-monitor.db")

    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None

    site_concurrency: int = 3
    page_concurrency: int = 8
    asset_concurrency: int = 12

    request_timeout: float = 20.0
    max_retries: int = 3
    retry_backoff: float = 1.0

    user_agent: str = DEFAULT_USER_AGENT
    max_pages_per_site: int = 0  # 0 = no limit

    log_level: str = "INFO"
    dry_run: bool = False

    # PageSpeed Insights
    pagespeed_api_key: str | None = None
    pagespeed_strategies: tuple[str, ...] = ("mobile", "desktop")

    timezone: str = "UTC"

    # Dashboard
    dashboard_password: str | None = None
    session_secret: str = ""
    session_hours: int = 720  # 30 days

    sites: tuple[Site, ...] = field(default_factory=tuple)

    @classmethod
    def from_env(cls, env_file: str | os.PathLike[str] | None = ".env") -> "Settings":
        if env_file is not None and Path(env_file).is_file():
            load_dotenv(env_file, override=False)
        else:
            load_dotenv(override=False)

        sites_file = Path(os.getenv("SITES_FILE", "sites.yaml"))
        return cls(
            sites_file=sites_file,
            database_path=Path(os.getenv("DATABASE_PATH", "data/site-monitor.db")),
            telegram_bot_token=os.getenv("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=os.getenv("TELEGRAM_CHAT_ID") or None,
            site_concurrency=_env_int("SITE_CONCURRENCY", 3),
            page_concurrency=_env_int("PAGE_CONCURRENCY", 8),
            asset_concurrency=_env_int("ASSET_CONCURRENCY", 12),
            request_timeout=_env_float("REQUEST_TIMEOUT", 20.0),
            max_retries=_env_int("MAX_RETRIES", 3),
            retry_backoff=_env_float("RETRY_BACKOFF", 1.0),
            user_agent=os.getenv("USER_AGENT") or DEFAULT_USER_AGENT,
            max_pages_per_site=_env_int("MAX_PAGES_PER_SITE", 0),
            log_level=os.getenv("LOG_LEVEL", "INFO").upper(),
            dry_run=_env_bool("DRY_RUN", False),
            pagespeed_api_key=os.getenv("PAGESPEED_API_KEY") or None,
            timezone=os.getenv("TIMEZONE", "UTC"),
            dashboard_password=os.getenv("DASHBOARD_PASSWORD") or None,
            # Without an explicit secret, sessions are signed with a key
            # derived from the password. That means changing the password
            # invalidates every existing session, which is the behaviour you
            # want anyway.
            session_secret=os.getenv("SESSION_SECRET")
            or f"derived:{os.getenv('DASHBOARD_PASSWORD', '')}",
            session_hours=_env_int("SESSION_HOURS", 720),
            # sites.yaml is now only an import source; the database is the
            # source of truth, so a missing file is not an error.
            sites=load_sites(sites_file) if sites_file.is_file() else (),
        )

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_chat_id)


def load_sites(path: str | os.PathLike[str]) -> tuple[Site, ...]:
    """Parse sites.yaml into Site records."""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(
            f"sites file not found: {path} (copy sites.example.yaml to {path})"
        )

    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if isinstance(raw, list):
        entries = raw
    elif isinstance(raw, dict):
        entries = raw.get("sites") or []
    else:
        raise ConfigError(f"{path}: expected a mapping or a list at the top level")

    if not entries:
        raise ConfigError(f"{path}: no sites defined")

    sites: list[Site] = []
    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            raise ConfigError(f"{path}: site #{index + 1} must be a mapping")
        domain = str(entry.get("domain") or "").strip()
        sitemap = str(entry.get("sitemap") or "").strip()
        if not domain:
            raise ConfigError(f"{path}: site #{index + 1} is missing 'domain'")

        raw_pages = entry.get("pages") or []
        if not isinstance(raw_pages, list):
            raise ConfigError(f"{path}: site '{domain}' pages must be a list")

        pages: list[str] = []
        for page in raw_pages:
            url = str(page or "").strip()
            if not url:
                continue
            if not url.startswith(("http://", "https://")):
                raise ConfigError(
                    f"{path}: site '{domain}' page must be an absolute URL: {url!r}"
                )
            if url not in pages:  # a curated list often repeats entries
                pages.append(url)

        if not sitemap and not pages:
            raise ConfigError(
                f"{path}: site '{domain}' needs either 'sitemap' or 'pages'"
            )
        if sitemap and not sitemap.startswith(("http://", "https://")):
            raise ConfigError(
                f"{path}: site '{domain}' sitemap must be an absolute URL"
            )
        if domain in seen:
            raise ConfigError(f"{path}: duplicate site '{domain}'")
        seen.add(domain)

        max_pages = entry.get("max_pages")
        sites.append(
            Site(
                domain=domain,
                sitemap=sitemap,
                pages=tuple(pages),
                enabled=bool(entry.get("enabled", True)),
                max_pages=int(max_pages) if max_pages else None,
            )
        )
    return tuple(sites)


# Settings the dashboard stores in the database, and how to coerce them back
# out of TEXT columns.
_OVERRIDE_CASTS = {
    "telegram_bot_token": str,
    "telegram_chat_id": str,
    "pagespeed_api_key": str,
    "user_agent": str,
    "site_concurrency": int,
    "page_concurrency": int,
    "asset_concurrency": int,
    "max_retries": int,
    "max_pages_per_site": int,
    "request_timeout": float,
}


def apply_overrides(base: Settings, overrides: dict[str, str]) -> Settings:
    """Layer database-stored settings over the environment.

    The dashboard has to be able to change a Telegram token without a
    redeploy, so what is stored wins over what the environment supplied. A
    malformed stored value is ignored rather than crashing the run -- the
    environment value is still there to fall back on.
    """
    if not overrides:
        return base

    values = dict(base.__dict__)
    for key, cast in _OVERRIDE_CASTS.items():
        raw = overrides.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            values[key] = cast(raw)
        except (TypeError, ValueError):
            continue

    raw_strategies = (overrides.get("pagespeed_strategies") or "").strip()
    if raw_strategies:
        picked = tuple(
            part.strip().lower()
            for part in raw_strategies.split(",")
            if part.strip().lower() in {"mobile", "desktop"}
        )
        if picked:
            values["pagespeed_strategies"] = picked

    return Settings(**values)
