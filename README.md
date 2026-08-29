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

## What you get

A single container running a web dashboard that manages everything:

| | |
|---|---|
| **Overview** | What is broken right now, grouped by site, with recent history |
| **Sites** | Add, edit, pause and remove sites and their page lists |
| **Reports** | Every check ever run, with per-site detail, downloadable |
| **PageSpeed** | Lighthouse history, sortable by score, LCP, CLS, TBT or date |
| **Schedules** | Cron schedules the app runs itself — no platform cron needed |
| **Settings** | Telegram, PageSpeed key and crawl limits, editable without a redeploy |

Every report downloads as CSV or a formatted Excel workbook.

---

## Quick start

```bash
git clone https://github.com/NahidDesigner/site-monitor.git
cd site-monitor

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env       # set DASHBOARD_PASSWORD at minimum
python -m site_monitor serve
```

Then open <http://localhost:8080>, sign in, and add a site — or paste a whole
list into **Sites → Import**.

To try the detector against one page without any setup:

```bash
python -m site_monitor check-url https://dvlfirm.com/business-law/trust-restatement/
```

---

## Where configuration lives

**The database is the source of truth.** Sites, schedules and most settings are
stored in SQLite and edited in the dashboard, because a monitoring tool you
have to redeploy to change is a monitoring tool nobody updates.

Three things stay environment-only, since letting a web form change them would
mean a stolen session could repoint the app: `DASHBOARD_PASSWORD`,
`DATABASE_PATH` and `TIMEZONE`.

| Variable | Required | Purpose |
|---|---|---|
| `DASHBOARD_PASSWORD` | **yes** | The app refuses to start without it. |
| `DATABASE_PATH` | | Default `data/site-monitor.db`. Put it on a volume. |
| `TIMEZONE` | | How schedule times are read and shown. Default `UTC`. |
| `SESSION_SECRET` | | Signs cookies. Derived from the password if unset. |
| `SITES_FILE` | | Optional seed, imported once if the database is empty. |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_CHAT_ID` | | Also settable in the dashboard. |
| `PAGESPEED_API_KEY` | | Also settable in the dashboard. |
| `SITE_` / `PAGE_` / `ASSET_CONCURRENCY` | | Also settable in the dashboard. |

Anything saved in **Settings** overrides the matching environment variable.

---

## Schedules

Schedules live in the app, not the platform. Add one in the dashboard with a
standard five-field cron expression (or `@daily`, `@hourly`), pick whether it
runs the CSS check or a PageSpeed sweep, and the app fires it itself.

Times are read and displayed in `TIMEZONE`, so "3am" means 3am where you are.
A schedule is armed rather than fired when you save it, and a restart neither
loses nor double-fires one.

Two runs of the same kind never overlap: a second trigger while one is in
flight is refused, because two concurrent passes would double the request load
on every monitored origin.

---

## Command line

The CLI still does everything, which is what cron used to need and what
scripting still wants.

| Command | Does |
|---|---|
| `serve` | Run the dashboard and scheduler. **The container entry point.** |
| `check` | Run one full check, store it, alert if anything is broken. |
| `check-url <url>` | Check one page. Needs no configuration at all. |
| `check-site <domain>` | Check one configured site. |
| `sites list` / `sites import <file>` / `sites remove <domain>` | Manage the site list. |
| `discover <domains>` | Resolve sitemaps for a domain list, emit `sites.yaml`. |
| `validate` | Pre-flight every site without a full check. |
| `history` | Recent runs. |

**Exit codes:** `0` nothing broken · `1` breakages found · `2` the monitor itself failed.

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

## Migrating from the legacy `sites` table

The previous PHP monitor stored each site's curated page list as a JSON array
in a MySQL TEXT column. To convert a phpMyAdmin YAML export of that table:

```bash
python scripts/import_bosseo_export.py u195624314_bosseo.yml -o sites.yaml
```

It drops off-domain URLs rather than monitoring them, removes duplicates,
merges rows that share a hostname, and reports every change it made on stderr
so nothing disappears quietly.

---

## Development

```bash
pip install -r requirements-dev.txt
python -m pytest
```

217 tests, no network access required — every HTTP interaction runs through
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
  discovery.py  sitemap discovery and pre-flight probing
  pagespeed.py  PageSpeed Insights client and sweep
  cron.py       five-field cron parsing, timezone aware
  scheduler.py  in-process cron loop
  runner.py     executing a run, and tracking one in progress
  db.py         SQLite schema, site list, settings, history
  notifier.py   Telegram formatting and delivery
  exports.py    CSV and Excel reports
  webapp/       the dashboard (FastAPI + Jinja templates)
  cli.py        argparse entry points
```

`elementor.py` and `crawler.py` are deliberately independent of the CLI and the
database, so the planned MCP server and PageSpeed phases can drive the same
crawler without any of it moving.
