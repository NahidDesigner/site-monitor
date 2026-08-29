"""Shared fixtures modelled on the real dvlfirm.com breakage.

The page references 15 Elementor stylesheets. One of them --
post-39321.css?ver=1787903551 -- is the stale reference: WordPress answers it
with a 404 and its own HTML error page, which is exactly the failure that makes
a layout collapse without any console error.
"""

from __future__ import annotations

BASE = "https://dvlfirm.com"
PAGE_URL = f"{BASE}/business-law/trust-restatement/"
CSS_BASE = f"{BASE}/wp-content/uploads/elementor/css"

BROKEN_CSS = f"{CSS_BASE}/post-39321.css?ver=1787903551"

HEALTHY_CSS = [
    f"{CSS_BASE}/post-6.css?ver=1787903200",
    f"{CSS_BASE}/global.css?ver=1787903200",
    f"{CSS_BASE}/post-4088.css?ver=1787903200",
    f"{CSS_BASE}/post-12105.css?ver=1787903200",
    f"{CSS_BASE}/post-12345.css?ver=1787903200",
    f"{CSS_BASE}/post-15001.css?ver=1787903200",
    f"{CSS_BASE}/post-15002.css?ver=1787903200",
    f"{CSS_BASE}/post-15003.css?ver=1787903200",
    f"{CSS_BASE}/post-15004.css?ver=1787903200",
    f"{CSS_BASE}/post-15005.css?ver=1787903200",
    f"{CSS_BASE}/post-15006.css?ver=1787903200",
    f"{CSS_BASE}/post-15007.css?ver=1787903200",
    f"{CSS_BASE}/post-15008.css?ver=1787903200",
    f"{CSS_BASE}/post-15009.css?ver=1787903200",
]

# Non-Elementor stylesheets that must be ignored entirely.
UNRELATED_CSS = [
    f"{BASE}/wp-includes/css/dist/block-library/style.min.css?ver=6.5.2",
    f"{BASE}/wp-content/plugins/contact-form-7/includes/css/styles.css?ver=5.9",
    f"{BASE}/wp-content/themes/hello-elementor/style.css?ver=3.0",
]

WORDPRESS_404_HTML = (
    "<!DOCTYPE html><html><head><title>Page not found &#8211; DVL Firm</title>"
    "</head><body><h1>Oops! That page can&#8217;t be found.</h1></body></html>"
)


def build_page_html() -> str:
    """A realistic WordPress <head> mixing Elementor and unrelated stylesheets."""
    links = []
    for index, href in enumerate(HEALTHY_CSS[:2]):
        links.append(f"<link rel='stylesheet' id='elementor-post-{index}-css' href='{href}' media='all' />")
    for href in UNRELATED_CSS:
        links.append(f'<link rel="stylesheet" href="{href}" media="all" />')
    for index, href in enumerate(HEALTHY_CSS[2:], start=2):
        links.append(f'<link rel="stylesheet" id="elementor-post-{index}-css" href="{href}" media="all" />')
    # The stale one, written the way WordPress emits it.
    links.append(
        f'<link rel="stylesheet" id="elementor-post-39321-css" href="{BROKEN_CSS}" media="all" />'
    )
    return (
        "<!DOCTYPE html><html lang='en'><head><meta charset='UTF-8' />"
        "<title>Trust Restatement</title>" + "".join(links) + "</head>"
        "<body><div class='elementor'>content</div></body></html>"
    )
