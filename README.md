# site-monitor

Detects WordPress/Elementor pages whose cached HTML references stylesheets that
no longer exist — the failure that breaks a layout without any browser error.

**Task 1 (this repo, today): broken Elementor CSS detection.**
PageSpeed Insights reporting and an MCP server over the same crawler are planned
later phases and are deliberately not built yet.

---

## The failure it catches

Nginx FastCGI cache and Cloudflare can keep serving HTML long after Elementor
has regenerated its per-post stylesheets. The cached HTML still points at the
old `?ver=` timestamp:

```html
<link rel="stylesheet" id="elementor-post-39321-css"
      href="/wp-content/uploads/elementor/css/post-39321.css?ver=1787903551" />
```

That URL 404s. WordPress answers the 404 with its own **HTML** error page —
`content-type: text/html` — so the browser parses it as a stylesheet containing
zero rules. No console error, no failed request badge, just a page with its
styling silently gone.

So a stylesheet counts as healthy only when **both** hold:

| Check | Healthy |
|---|---|
| HTTP status | `200` |
| `content-type` | contains `text/css` |

Anything else is reported.

### Detection steps

1. Fetch the page HTML with a desktop-browser User-Agent, following redirects.
2. Regex every `elementor/css/*.css` href out of the HTML (typically ~15 per page).
3. `HEAD` each stylesheet URL.
4. Flag it if the status is not 200, or the content type is not CSS.

Verified against `https://dvlfirm.com/business-law/trust-restatement/`, where it
flags `post-39321.css?ver=1787903551` (404, `text/html`) and nothing else. That
exact case is pinned as a regression test in
`tests/test_elementor.py::test_end_to_end_flags_only_the_stale_stylesheet`.

---

## Quick start

```bash
git clone https://github.com/NahidDesigner/site-monitor.git
cd site-monitor

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env            # fill in the Telegram credentials
cp sites.example.yaml sites.yaml # add your sites

# Confirm the detector against a single page before wiring up cron:
python -m site_monitor check-url https://dvlfirm.com/business-law/trust-restatement/

# Full run, printing the Telegram message instead of sending it:
python -m site_monitor check --no-alert
```

---

## Configuration

### `sites.yaml`

```yaml
sites:
  - domain: dvlfirm.com
    sitemap: https://dvlfirm.com/sitemap_index.xml

  - domain: example.com
    sitemap: https://example.com/wp-sitemap.xml
    max_pages: 200   # optional per-site cap
    enabled: true    # optional; false skips the site
```

Sitemap **indexes** are followed automatically (nested up to 4 levels), and
`.xml.gz` sitemaps are decompressed. Yoast, Rank Math and WP core sitemaps all
work as-is.

### `.env`

Real environment variables take precedence over `.env`, so Coolify's injected
variables win — as they should.

| Variable | Default | Purpose |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | — | From @BotFather. |
| `TELEGRAM_CHAT_ID` | — | Your user id, or a group id (`-100…`). |
| `SITES_FILE` | `sites.yaml` | Site list location. |
| `DATABASE_PATH` | `data/site-monitor.db` | SQLite file; put it on a volume. |
| `SITE_CONCURRENCY` | `3` | Sites checked in parallel. |
| `PAGE_CONCURRENCY` | `8` | Pages in parallel, per site. |
| `ASSET_CONCURRENCY` | `12` | Stylesheet `HEAD`s in parallel, per page. |
| `REQUEST_TIMEOUT` | `20` | Seconds. |
| `MAX_RETRIES` | `3` | Attempts per request. |
| `RETRY_BACKOFF` | `1.0` | Seconds; doubles each attempt, with jitter. |
| `USER_AGENT` | desktop Chrome | Some stacks vary HTML by UA. |
| `MAX_PAGES_PER_SITE` | `0` | `0` = every page in the sitemap. |
| `LOG_LEVEL` | `INFO` | |
| `DRY_RUN` | `false` | `true` prints alerts instead of sending. |

Concurrency is enforced at all three levels with semaphores, and the connection
pool is sized to match, so the ceiling on in-flight requests is
`SITE_CONCURRENCY × max(PAGE_CONCURRENCY, ASSET_CONCURRENCY)`.

Retries cover transport errors and transient statuses (408, 425, 429, 5xx) with
exponential backoff plus jitter. **404 is never retried** — it is the answer this
tool is looking for.

---

## Commands

| Command | Does |
|---|---|
| `python -m site_monitor check` | Check every site, store the run, alert if broken. **The cron entry point.** |
| `python -m site_monitor check --no-alert` | Same, but print the Telegram message instead of sending it. |
| `python -m site_monitor check-url <url>` | Check one page. Needs no `sites.yaml`. |
| `python -m site_monitor check-site <domain>` | Check one configured site. |
| `python -m site_monitor history [--limit N]` | Recent runs from the database. |

**Exit codes:** `0` nothing broken · `1` breakages found · `2` the run itself failed.
That split lets a cron wrapper tell "the site is broken" from "the monitor is broken".

---

## Alerts

A Telegram message is sent **only when something is broken**. A clean run sends
nothing at all.

Findings are grouped by site, then by page:

```
🚨 Broken Elementor CSS — 1 site affected
1 broken stylesheet(s) · 143 pages · 2104 stylesheets checked

dvlfirm.com — 1 broken CSS on 1 page (of 143 checked)
  /business-law/trust-restatement/
    • post-39321.css?ver=1787903551 — HTTP 404 (content-type: text/html)
```

Reports longer than Telegram's message limit are split across messages, and very
large sites are truncated per page and per site with an "…and N more" line.

---

## Storage

SQLite, at `DATABASE_PATH`. Only summaries and breakages are stored — recording
every healthy stylesheet would add tens of thousands of rows per run and answer
no question anyone asks of this tool.

| Table | Holds |
|---|---|
| `runs` | One row per invocation: timings, totals, status. |
| `site_runs` | Per-site summary within a run. |
| `broken_assets` | Every failing stylesheet: page, URL, status, content type, reason. |
| `page_errors` | Pages that could not be fetched at all. |

```sql
-- Which pages have been broken most often over the last month?
SELECT domain, page_url, COUNT(*) AS hits
  FROM broken_assets
 WHERE detected_at > datetime('now', '-30 days')
 GROUP BY domain, page_url
 ORDER BY hits DESC;
```

---

## Deploying on Coolify

1. Create a new resource from this repo (Dockerfile or Docker Compose).
2. Add a **persistent volume** mounted at `/data`, and set
   `DATABASE_PATH=/data/site-monitor.db`.
3. Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` as environment variables.
4. Mount or commit your `sites.yaml`.
5. Add a **Scheduled Task**:

   | Field | Value |
   |---|---|
   | Command | `python -m site_monitor check` |
   | Frequency | `0 */6 * * *` (every 6 hours) |

Because the cache staleness this detects is usually cleared by a purge, every
few hours is a sensible cadence — often enough to catch a broken deploy, rare
enough not to hammer the sites.

Plain crontab works too:

```cron
0 */6 * * * cd /opt/site-monitor && .venv/bin/python -m site_monitor check >> /var/log/site-monitor.log 2>&1
```

---

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

77 tests, no network access required — every HTTP interaction runs through
`httpx.MockTransport`, including a fixture that reproduces the dvlfirm.com
breakage byte for byte.

### Layout

```
site_monitor/
  config.py     .env + sites.yaml -> Settings
  http.py       browser-like AsyncClient, retries with backoff
  sitemap.py    sitemap/index walking, gzip, dedupe
  elementor.py  the detection itself: extract hrefs, HEAD, judge
  crawler.py    orchestration and concurrency limits
  db.py         SQLite schema and writes
  notifier.py   Telegram formatting and delivery
  cli.py        argparse entry points
```

`elementor.py` and `crawler.py` are deliberately independent of the CLI and the
database, so the planned MCP server and PageSpeed phases can drive the same
crawler without any of it moving.
