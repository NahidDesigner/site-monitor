"""Alert formatting and delivery."""

from __future__ import annotations

import httpx

from site_monitor.crawler import RunResult, SiteResult
from site_monitor.elementor import AssetResult, PageResult
from site_monitor.notifier import (
    MAX_MESSAGE_CHARS,
    TelegramNotifier,
    format_alert,
)


def broken_asset(url: str, status=404, ctype="text/html") -> AssetResult:
    return AssetResult(
        url=url,
        status_code=status,
        content_type=ctype,
        ok=False,
        reason=f"HTTP {status} (content-type: {ctype})",
        elapsed_ms=12,
    )


def site(domain: str, pages: list[PageResult], **kwargs) -> SiteResult:
    return SiteResult(
        domain=domain,
        sitemap=f"https://{domain}/sitemap.xml",
        pages_found=len(pages),
        pages=pages,
        **kwargs,
    )


def page(url: str, assets: list[AssetResult], checked=15) -> PageResult:
    return PageResult(
        url=url, status_code=200, assets_checked=checked, broken=tuple(assets)
    )


def test_no_alert_when_nothing_is_broken():
    healthy = site(
        "dvlfirm.com", [page("https://dvlfirm.com/", [])]
    )

    assert format_alert(RunResult(sites=[healthy])) == []


def test_alert_groups_findings_by_site():
    run = RunResult(
        sites=[
            site(
                "dvlfirm.com",
                [
                    page(
                        "https://dvlfirm.com/business-law/trust-restatement/",
                        [
                            broken_asset(
                                "https://dvlfirm.com/wp-content/uploads/"
                                "elementor/css/post-39321.css?ver=1787903551"
                            )
                        ],
                    )
                ],
            ),
            site(
                "other.com",
                [
                    page(
                        "https://other.com/about/",
                        [
                            broken_asset(
                                "https://other.com/wp-content/uploads/"
                                "elementor/css/post-11.css?ver=1"
                            )
                        ],
                    )
                ],
            ),
        ]
    )

    messages = format_alert(run)
    body = "\n".join(messages)

    assert len(messages) == 1
    assert "<b>dvlfirm.com</b>" in body
    assert "<b>other.com</b>" in body
    assert "post-39321.css?ver=1787903551" in body
    assert "/business-law/trust-restatement/" in body
    assert "HTTP 404" in body
    # The two sites appear as separate sections, in configured order.
    assert body.index("dvlfirm.com") < body.index("other.com")


def test_healthy_sites_are_left_out_of_the_alert():
    run = RunResult(
        sites=[
            site("healthy.com", [page("https://healthy.com/", [])]),
            site(
                "broken.com",
                [page("https://broken.com/x/", [broken_asset("https://broken.com/a.css")])],
            ),
        ]
    )

    body = "\n".join(format_alert(run))

    assert "healthy.com" not in body
    assert "broken.com" in body


def test_site_level_errors_are_reported_too():
    run = RunResult(
        sites=[site("down.com", [], error="sitemap unreachable: ConnectError")]
    )

    body = "\n".join(format_alert(run))

    assert "down.com" in body
    assert "sitemap unreachable" in body


def test_html_in_urls_is_escaped():
    run = RunResult(
        sites=[
            site(
                "x.com",
                [
                    page(
                        "https://x.com/<script>/",
                        [broken_asset("https://x.com/elementor/css/a.css?ver=1&b=2")],
                    )
                ],
            )
        ]
    )

    body = "\n".join(format_alert(run))

    assert "<script>" not in body
    assert "&lt;script&gt;" in body
    assert "&amp;b=2" in body


def test_long_reports_are_split_into_sendable_messages():
    sites = [
        site(
            f"site{i}.com",
            [
                page(
                    f"https://site{i}.com/page-{j}/",
                    [
                        broken_asset(
                            f"https://site{i}.com/wp-content/uploads/"
                            f"elementor/css/post-{j}.css?ver=178790355{j}"
                        )
                    ],
                )
                for j in range(10)
            ],
        )
        for i in range(30)
    ]

    messages = format_alert(RunResult(sites=sites))

    assert len(messages) > 1
    assert all(len(message) <= MAX_MESSAGE_CHARS + 400 for message in messages)


def test_per_page_and_per_site_truncation_keeps_messages_readable():
    assets = [
        broken_asset(f"https://x.com/elementor/css/post-{i}.css?ver=1")
        for i in range(12)
    ]
    pages = [page(f"https://x.com/p{i}/", assets) for i in range(40)]

    body = "\n".join(format_alert(RunResult(sites=[site("x.com", pages)])))

    assert "more on this page" in body
    assert "more affected pages" in body


async def test_notifier_posts_to_the_bot_api():
    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        calls.append(
            {"url": str(request.url), "body": json.loads(request.content.decode())}
        )
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(
        "TOKEN", "-100123", transport=httpx.MockTransport(handler)
    )
    sent = await notifier.send(["one", "two"])

    assert sent == 2
    assert calls[0]["url"] == "https://api.telegram.org/botTOKEN/sendMessage"
    assert calls[0]["body"]["chat_id"] == "-100123"
    assert calls[0]["body"]["parse_mode"] == "HTML"
    assert calls[1]["body"]["text"] == "two"


async def test_notifier_sends_nothing_for_an_empty_report():
    def handler(request: httpx.Request) -> httpx.Response:  # pragma: no cover
        raise AssertionError("should not be called")

    notifier = TelegramNotifier(
        "TOKEN", "-1", transport=httpx.MockTransport(handler)
    )

    assert await notifier.send([]) == 0


async def test_notifier_honours_rate_limit_then_succeeds():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        if attempts["n"] == 1:
            return httpx.Response(
                429, json={"ok": False, "parameters": {"retry_after": 0}}
            )
        return httpx.Response(200, json={"ok": True})

    notifier = TelegramNotifier(
        "TOKEN", "-1", transport=httpx.MockTransport(handler), max_retries=3
    )

    assert await notifier.send(["hi"]) == 1
    assert attempts["n"] == 2


async def test_notifier_gives_up_immediately_on_a_bad_token():
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(401, json={"ok": False, "description": "Unauthorized"})

    notifier = TelegramNotifier(
        "BAD", "-1", transport=httpx.MockTransport(handler), max_retries=3
    )

    assert await notifier.send(["hi"]) == 0
    assert attempts["n"] == 1
