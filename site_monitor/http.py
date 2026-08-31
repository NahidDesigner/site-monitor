"""Thin async HTTP layer: browser-like requests with bounded retries."""

from __future__ import annotations

import asyncio
import logging
import random
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)

# Transport-level failures and these statuses are worth another attempt.
# A 404 is a real answer -- it is exactly what we are hunting for -- so it is
# never retried.
RETRY_STATUSES = frozenset({408, 425, 429, 500, 502, 503, 504, 522, 524})


@dataclass(frozen=True)
class FetchError(Exception):
    """A request that never produced a response, after all retries."""

    url: str
    reason: str

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.url}: {self.reason}"


def browser_headers(user_agent: str) -> dict[str, str]:
    """Exactly what a browser sends on an ordinary navigation.

    The User-Agent matters because some stacks (and Cloudflare) vary their
    response by it, and we need the same HTML a visitor gets.

    Deliberately no Cache-Control or Pragma. A browser sends neither on a normal
    navigation -- only on a hard refresh -- so sending them marks the request as
    unusual. Worse, they ask intermediaries to bypass the cache, and the stale
    cache is the entire thing being measured: a layer that honoured them would
    serve freshly generated HTML and the stale ?ver= reference would never
    appear. We want the cached page, exactly as a visitor receives it.
    """
    return {
        "User-Agent": user_agent,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,"
        "image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
        "Accept-Encoding": "gzip, deflate, br",
    }


def build_client(
    *,
    user_agent: str,
    timeout: float,
    max_connections: int,
    transport: httpx.AsyncBaseTransport | None = None,
) -> httpx.AsyncClient:
    """An AsyncClient configured the way every request in this tool wants it."""
    return httpx.AsyncClient(
        follow_redirects=True,
        timeout=httpx.Timeout(timeout),
        headers=browser_headers(user_agent),
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=max_connections,
        ),
        transport=transport,
    )


class Fetcher:
    """Issues requests through a shared client, retrying transient failures."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        max_retries: int = 3,
        backoff: float = 1.0,
    ) -> None:
        self._client = client
        self._max_retries = max(1, max_retries)
        self._backoff = backoff

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Perform a request, retrying transport errors and transient statuses.

        Raises FetchError if every attempt failed to produce a response.
        """
        last_reason = "unknown error"
        for attempt in range(1, self._max_retries + 1):
            try:
                response = await self._client.request(method, url, **kwargs)
            except httpx.HTTPError as exc:
                last_reason = f"{type(exc).__name__}: {exc}"
                log.debug("%s %s failed (attempt %s): %s", method, url, attempt, exc)
            else:
                if response.status_code in RETRY_STATUSES and attempt < self._max_retries:
                    last_reason = f"HTTP {response.status_code}"
                    log.debug(
                        "%s %s returned %s (attempt %s), retrying",
                        method,
                        url,
                        response.status_code,
                        attempt,
                    )
                else:
                    return response

            if attempt < self._max_retries:
                await asyncio.sleep(self._sleep_for(attempt))

        raise FetchError(url=url, reason=last_reason)

    async def get(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("GET", url, **kwargs)

    async def head(self, url: str, **kwargs) -> httpx.Response:
        return await self.request("HEAD", url, **kwargs)

    def _sleep_for(self, attempt: int) -> float:
        """Exponential backoff with a little jitter to avoid lockstep retries."""
        return self._backoff * (2 ** (attempt - 1)) * (0.5 + random.random())
