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
    """A single monitored WordPress site."""

    domain: str
    sitemap: str
    enabled: bool = True
    max_pages: int | None = None

    @property
    def key(self) -> str:
        return self.domain


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
            sites=load_sites(sites_file),
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
        if not sitemap:
            raise ConfigError(f"{path}: site '{domain}' is missing 'sitemap'")
        if not sitemap.startswith(("http://", "https://")):
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
                enabled=bool(entry.get("enabled", True)),
                max_pages=int(max_pages) if max_pages else None,
            )
        )
    return tuple(sites)
