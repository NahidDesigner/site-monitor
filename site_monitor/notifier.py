"""Telegram alerting: one grouped report, sent only when something is broken."""

from __future__ import annotations

import asyncio
import html
import logging
from urllib.parse import urlparse

import httpx

from .crawler import RunResult, SiteResult

log = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org"
# Telegram rejects messages over 4096 characters; leave room for the split
# marker rather than trimming right at the edge.
MAX_MESSAGE_CHARS = 3800

MAX_PAGES_PER_SITE = 15
MAX_ASSETS_PER_PAGE = 5


def _esc(text: str) -> str:
    return html.escape(str(text), quote=False)


def _short_path(url: str) -> str:
    """A page URL as its path, which is what makes a report skimmable."""
    parsed = urlparse(url)
    path = parsed.path or "/"
    if parsed.query:
        path += f"?{parsed.query}"
    return path


def _short_asset(url: str) -> str:
    """`.../elementor/css/post-39321.css?ver=178...` -> `post-39321.css?ver=178...`"""
    parsed = urlparse(url)
    name = parsed.path.rsplit("/", 1)[-1] or parsed.path
    return f"{name}?{parsed.query}" if parsed.query else name


def format_site_section(site: SiteResult) -> list[str]:
    """Render one site as a list of lines."""
    if site.error:
        return [
            f"<b>{_esc(site.domain)}</b>",
            f"  ⚠️ {_esc(site.error)}",
        ]

    if site.warning and not site.broken_pages:
        return [
            f"<b>{_esc(site.domain)}</b>",
            f"  ⚠️ {_esc(site.warning)}",
        ]

    broken_pages = site.broken_pages
    lines = [
        f"<b>{_esc(site.domain)}</b> — "
        f"{site.broken_asset_count} broken CSS on {len(broken_pages)} "
        f"page{'s' if len(broken_pages) != 1 else ''} "
        f"(of {site.pages_checked} checked)"
    ]

    for page in broken_pages[:MAX_PAGES_PER_SITE]:
        lines.append(f'  <a href="{_esc(page.url)}">{_esc(_short_path(page.url))}</a>')
        if page.error:
            lines.append(f"    ⚠️ {_esc(page.error)}")
        for asset in page.broken[:MAX_ASSETS_PER_PAGE]:
            lines.append(
                f"    • <code>{_esc(_short_asset(asset.url))}</code>"
                f" — {_esc(asset.reason or 'broken')}"
            )
        remaining = len(page.broken) - MAX_ASSETS_PER_PAGE
        if remaining > 0:
            lines.append(f"    • …and {remaining} more on this page")

    remaining_pages = len(broken_pages) - MAX_PAGES_PER_SITE
    if remaining_pages > 0:
        lines.append(f"  …and {remaining_pages} more affected pages")

    return lines


def format_alert(run: RunResult) -> list[str]:
    """Build the alert as one or more Telegram-sized messages.

    Returns an empty list when nothing is broken -- the caller sends nothing.
    """
    affected = run.sites_with_findings
    if not affected:
        return []

    header = (
        f"🚨 <b>Broken Elementor CSS</b> — "
        f"{len(affected)} site{'s' if len(affected) != 1 else ''} affected\n"
        f"<i>{run.broken_asset_count} broken stylesheet(s) · "
        f"{run.pages_checked} pages · {run.assets_checked} stylesheets checked</i>"
    )

    messages: list[str] = []
    current = header

    for site in affected:
        section = "\n\n" + "\n".join(format_site_section(site))
        if len(current) + len(section) > MAX_MESSAGE_CHARS and current:
            messages.append(current)
            current = section.lstrip("\n")
        else:
            current += section

    if current:
        messages.append(current)
    return messages


class TelegramNotifier:
    """Posts messages to one chat via the Bot API."""

    def __init__(
        self,
        token: str,
        chat_id: str,
        *,
        api_base: str = TELEGRAM_API,
        timeout: float = 20.0,
        max_retries: int = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._chat_id = chat_id
        self._api_base = api_base.rstrip("/")
        self._timeout = timeout
        self._max_retries = max(1, max_retries)
        self._transport = transport

    async def send(self, messages: list[str]) -> int:
        """Send each message in order. Returns how many were delivered."""
        if not messages:
            return 0

        url = f"{self._api_base}/bot{self._token}/sendMessage"
        sent = 0
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self._timeout), transport=self._transport
        ) as client:
            for message in messages:
                if await self._send_one(client, url, message):
                    sent += 1
        return sent

    async def _send_one(
        self, client: httpx.AsyncClient, url: str, text: str
    ) -> bool:
        payload = {
            "chat_id": self._chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await client.post(url, json=payload)
            except httpx.HTTPError as exc:
                log.warning("telegram send failed (attempt %s): %s", attempt, exc)
            else:
                if response.status_code == 200:
                    return True
                if response.status_code == 429:
                    # Telegram tells us exactly how long to wait.
                    retry_after = 1.0
                    try:
                        retry_after = float(
                            response.json().get("parameters", {}).get("retry_after", 1)
                        )
                    except (ValueError, AttributeError):
                        pass
                    log.warning("telegram rate limited, waiting %ss", retry_after)
                    await asyncio.sleep(retry_after)
                    continue
                log.warning(
                    "telegram returned HTTP %s: %s",
                    response.status_code,
                    response.text[:300],
                )
                if 400 <= response.status_code < 500:
                    return False  # bad token/chat id: retrying will not help

            if attempt < self._max_retries:
                await asyncio.sleep(2**attempt)
        return False
