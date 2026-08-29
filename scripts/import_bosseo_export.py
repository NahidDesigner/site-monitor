#!/usr/bin/env python3
"""Convert a phpMyAdmin YAML export of the legacy `sites` table into sites.yaml.

The legacy app stores each site's curated page list as a JSON array in a TEXT
column. This turns that into a site list this monitor can read, one-time.

    python scripts/import_bosseo_export.py export.yml -o sites.yaml

Off-domain URLs are dropped rather than monitored -- a stray third-party link
in someone's page list should not become a monitored "site" -- and every drop
is reported so nothing disappears quietly.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlparse

import yaml


def host_of(url: str) -> str:
    return urlparse(url).netloc.lower().split(":")[0]


def same_site(host: str, primary: str) -> bool:
    """www.example.com and example.com are the same site; amazon.com is not."""
    return host.removeprefix("www.") == primary.removeprefix("www.")


def build(rows: list[dict]) -> tuple[list[dict], list[str]]:
    """Return (site entries, human-readable notes about what was changed)."""
    sites: list[dict] = []
    notes: list[str] = []

    for row in rows:
        key = str(row.get("site_key") or "").strip()
        try:
            raw_pages = json.loads(row.get("pages") or "[]")
        except (TypeError, ValueError) as exc:
            notes.append(f"{key}: pages column is not valid JSON ({exc}) -- skipped")
            continue
        if not isinstance(raw_pages, list) or not raw_pages:
            notes.append(f"{key}: no pages -- skipped")
            continue

        urls = [
            str(url).strip()
            for url in raw_pages
            if str(url).strip().startswith(("http://", "https://"))
        ]
        if not urls:
            notes.append(f"{key}: no usable http(s) URLs -- skipped")
            continue

        primary = host_of(urls[0])
        kept: list[str] = []
        dropped: list[str] = []
        for url in urls:
            if not same_site(host_of(url), primary):
                dropped.append(url)
            elif url not in kept:  # curated lists repeat entries
                kept.append(url)

        if dropped:
            hosts = sorted({host_of(url) for url in dropped})
            notes.append(
                f"{primary}: dropped {len(dropped)} off-domain URL(s) "
                f"({', '.join(hosts)})"
            )
        removed = len(urls) - len(kept) - len(dropped)
        if removed:
            notes.append(f"{primary}: removed {removed} duplicate URL(s)")

        sites.append({"domain": primary, "site_key": key, "pages": kept})

    # A hostname appearing under two site_keys would silently merge in alerts.
    for host, count in Counter(site["domain"] for site in sites).items():
        if count > 1:
            notes.append(f"{host}: appears in {count} rows -- merged into one entry")

    merged: dict[str, dict] = {}
    for site in sites:
        entry = merged.setdefault(site["domain"], {"domain": site["domain"], "pages": []})
        for url in site["pages"]:
            if url not in entry["pages"]:
                entry["pages"].append(url)

    return list(merged.values()), notes


def render(sites: list[dict]) -> str:
    lines = [
        "# Generated from the legacy bosseo `sites` table.",
        "# Each site carries its curated page list; no sitemap crawl is performed.",
        "",
        "sites:",
    ]
    for site in sites:
        lines.append(f"  - domain: {site['domain']}")
        lines.append(f"    pages:  # {len(site['pages'])}")
        lines.extend(f"      - {url}" for url in site["pages"])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", help="phpMyAdmin YAML export of the sites table")
    parser.add_argument("-o", "--output", help="write here instead of stdout")
    args = parser.parse_args(argv)

    rows = yaml.safe_load(Path(args.export).read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        print("export did not contain a list of rows", file=sys.stderr)
        return 1

    sites, notes = build(rows)
    body = render(sites)

    for note in notes:
        print(f"  note: {note}", file=sys.stderr)
    print(
        f"\n{len(sites)} site(s), {sum(len(site['pages']) for site in sites)} page(s)",
        file=sys.stderr,
    )

    if args.output:
        Path(args.output).write_text(body, encoding="utf-8")
        print(f"wrote {args.output}", file=sys.stderr)
    else:
        print(body, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
