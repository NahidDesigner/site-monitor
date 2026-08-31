"""SQLite persistence for check runs.

Only summaries and breakages are stored. Recording every healthy stylesheet on
every page would add tens of thousands of rows per run and answer no question
anyone asks of this tool.
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Iterator

if TYPE_CHECKING:  # pragma: no cover
    from .crawler import SiteResult

SCHEMA = """
CREATE TABLE IF NOT EXISTS runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at     TEXT    NOT NULL,
    finished_at    TEXT,
    status         TEXT    NOT NULL DEFAULT 'running',
    sites_checked  INTEGER NOT NULL DEFAULT 0,
    pages_checked  INTEGER NOT NULL DEFAULT 0,
    assets_checked INTEGER NOT NULL DEFAULT 0,
    broken_assets  INTEGER NOT NULL DEFAULT 0,
    error          TEXT,
    trigger        TEXT    NOT NULL DEFAULT '',
    scope          TEXT    NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS site_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id         INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    domain         TEXT    NOT NULL,
    sitemap        TEXT    NOT NULL,
    pages_found    INTEGER NOT NULL DEFAULT 0,
    pages_checked  INTEGER NOT NULL DEFAULT 0,
    assets_checked INTEGER NOT NULL DEFAULT 0,
    broken_pages   INTEGER NOT NULL DEFAULT 0,
    broken_assets  INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER NOT NULL DEFAULT 0,
    error          TEXT
);

CREATE TABLE IF NOT EXISTS broken_assets (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    site_run_id  INTEGER NOT NULL REFERENCES site_runs(id) ON DELETE CASCADE,
    domain       TEXT    NOT NULL,
    page_url     TEXT    NOT NULL,
    asset_url    TEXT    NOT NULL,
    status_code  INTEGER,
    content_type TEXT,
    reason       TEXT,
    detected_at  TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS page_errors (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id      INTEGER NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    site_run_id INTEGER NOT NULL REFERENCES site_runs(id) ON DELETE CASCADE,
    domain      TEXT    NOT NULL,
    page_url    TEXT    NOT NULL,
    status_code INTEGER,
    error       TEXT    NOT NULL,
    detected_at TEXT    NOT NULL
);

-- The site list itself. This is the source of truth, not sites.yaml: the
-- dashboard edits it, so it has to live somewhere writable at runtime.
CREATE TABLE IF NOT EXISTS monitored_sites (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    domain     TEXT    NOT NULL UNIQUE,
    sitemap    TEXT    NOT NULL DEFAULT '',
    enabled    INTEGER NOT NULL DEFAULT 1,
    max_pages  INTEGER,
    created_at TEXT    NOT NULL,
    updated_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS monitored_pages (
    id       INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
    site_id  INTEGER NOT NULL REFERENCES monitored_sites(id) ON DELETE CASCADE,
    url      TEXT    NOT NULL,
    position INTEGER NOT NULL DEFAULT 0,
    UNIQUE(site_id, url)
);

-- Settings the dashboard can edit. Anything stored here overrides the
-- environment, so Telegram and API keys can be changed without a redeploy.
CREATE TABLE IF NOT EXISTS app_settings (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- Cron schedules owned by the app itself, so the whole thing is managed from
-- the dashboard rather than the hosting platform.
CREATE TABLE IF NOT EXISTS schedules (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    name         TEXT    NOT NULL,
    kind         TEXT    NOT NULL DEFAULT 'css_check',
    cron         TEXT    NOT NULL,
    enabled      INTEGER NOT NULL DEFAULT 1,
    last_run_at  TEXT,
    last_status  TEXT,
    next_run_at  TEXT,
    created_at   TEXT    NOT NULL
);

-- PageSpeed Insights history.
CREATE TABLE IF NOT EXISTS pagespeed_runs (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at   TEXT    NOT NULL,
    finished_at  TEXT,
    status       TEXT    NOT NULL DEFAULT 'running',
    urls_tested  INTEGER NOT NULL DEFAULT 0,
    failures     INTEGER NOT NULL DEFAULT 0,
    error        TEXT,
    trigger      TEXT    NOT NULL DEFAULT '',
    expected     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS pagespeed_results (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id       INTEGER NOT NULL REFERENCES pagespeed_runs(id) ON DELETE CASCADE,
    domain       TEXT    NOT NULL,
    url          TEXT    NOT NULL,
    strategy     TEXT    NOT NULL,
    performance  REAL,
    fcp_ms       REAL,
    lcp_ms       REAL,
    cls          REAL,
    tbt_ms       REAL,
    speed_index  REAL,
    tti_ms       REAL,
    error        TEXT,
    tested_at    TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_ps_results_run    ON pagespeed_results(run_id);
CREATE INDEX IF NOT EXISTS idx_ps_results_date   ON pagespeed_results(tested_at DESC);
CREATE INDEX IF NOT EXISTS idx_ps_results_domain ON pagespeed_results(domain, tested_at DESC);
CREATE INDEX IF NOT EXISTS idx_pages_site       ON monitored_pages(site_id, position);
CREATE INDEX IF NOT EXISTS idx_site_runs_run    ON site_runs(run_id);
CREATE INDEX IF NOT EXISTS idx_broken_run       ON broken_assets(run_id);
CREATE INDEX IF NOT EXISTS idx_broken_domain    ON broken_assets(domain, detected_at);
CREATE INDEX IF NOT EXISTS idx_page_errors_run  ON page_errors(run_id);
"""


def utcnow() -> str:
    """Timestamps are ISO-8601 UTC so they sort lexicographically."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """A small wrapper over one SQLite file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent and str(self.path.parent) not in ("", "."):
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.executescript(SCHEMA)
        self._migrate()
        self._conn.commit()

    # Columns added after the first release. CREATE TABLE IF NOT EXISTS does
    # nothing to a table that already exists, so new columns need adding by
    # hand for anyone already running this.
    MIGRATIONS = (
        ("runs", "trigger", "trigger TEXT NOT NULL DEFAULT ''"),
        ("runs", "scope", "scope TEXT NOT NULL DEFAULT ''"),
        ("pagespeed_runs", "trigger", "trigger TEXT NOT NULL DEFAULT ''"),
        ("pagespeed_runs", "expected", "expected INTEGER NOT NULL DEFAULT 0"),
    )

    def _migrate(self) -> None:
        for table, column, ddl in self.MIGRATIONS:
            existing = {
                row[1] for row in self._conn.execute(f"PRAGMA table_info({table})")
            }
            if column not in existing:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {ddl}")

    # -- lifecycle ---------------------------------------------------------

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn:
            yield self._conn

    # -- writes ------------------------------------------------------------

    def start_run(self, *, trigger: str = "", scope: str = "") -> int:
        """`trigger` is what started it; `scope` names the sites it covered."""
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO runs (started_at, status, trigger, scope)
                VALUES (?, 'running', ?, ?)
                """,
                (utcnow(), trigger, scope),
            )
        return int(cursor.lastrowid)

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        sites_checked: int,
        pages_checked: int,
        assets_checked: int,
        broken_assets: int,
        error: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE runs
                   SET finished_at = ?, status = ?, sites_checked = ?,
                       pages_checked = ?, assets_checked = ?, broken_assets = ?,
                       error = ?
                 WHERE id = ?
                """,
                (
                    utcnow(),
                    status,
                    sites_checked,
                    pages_checked,
                    assets_checked,
                    broken_assets,
                    error,
                    run_id,
                ),
            )

    def record_site_result(self, run_id: int, result: "SiteResult") -> int:
        """Persist one site's summary plus its breakages, in a single tx."""
        now = utcnow()
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO site_runs (
                    run_id, domain, sitemap, pages_found, pages_checked,
                    assets_checked, broken_pages, broken_assets, duration_ms, error
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.domain,
                    result.sitemap,
                    result.pages_found,
                    result.pages_checked,
                    result.assets_checked,
                    result.broken_page_count,
                    result.broken_asset_count,
                    result.duration_ms,
                    result.error,
                ),
            )
            site_run_id = int(cursor.lastrowid)

            conn.executemany(
                """
                INSERT INTO broken_assets (
                    run_id, site_run_id, domain, page_url, asset_url,
                    status_code, content_type, reason, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        site_run_id,
                        result.domain,
                        page.url,
                        asset.url,
                        asset.status_code,
                        asset.content_type,
                        asset.reason,
                        now,
                    )
                    for page in result.pages
                    for asset in page.broken
                ],
            )

            conn.executemany(
                """
                INSERT INTO page_errors (
                    run_id, site_run_id, domain, page_url, status_code,
                    error, detected_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        run_id,
                        site_run_id,
                        result.domain,
                        page.url,
                        page.status_code,
                        page.error,
                        now,
                    )
                    for page in result.pages
                    if page.error
                ],
            )
        return site_run_id


    # -- the site list -----------------------------------------------------
    #
    # sites.yaml is an import format, not the source of truth. The dashboard
    # edits sites at runtime, so they live in the database where a running
    # container can write them.

    def list_sites(self, *, enabled_only: bool = False) -> list["Site"]:
        """Every configured site, with its pages, in domain order."""
        from .config import Site

        query = "SELECT * FROM monitored_sites"
        if enabled_only:
            query += " WHERE enabled = 1"
        query += " ORDER BY domain"

        rows = list(self._conn.execute(query))
        pages_by_site: dict[int, list[str]] = {}
        for page in self._conn.execute(
            "SELECT site_id, url FROM monitored_pages ORDER BY site_id, position, id"
        ):
            pages_by_site.setdefault(page["site_id"], []).append(page["url"])

        return [
            Site(
                domain=row["domain"],
                sitemap=row["sitemap"] or "",
                pages=tuple(pages_by_site.get(row["id"], ())),
                enabled=bool(row["enabled"]),
                max_pages=row["max_pages"] or None,
            )
            for row in rows
        ]

    def get_site(self, domain: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM monitored_sites WHERE domain = ?", (domain,)
        ).fetchone()

    def site_pages(self, site_id: int) -> list[str]:
        return [
            row["url"]
            for row in self._conn.execute(
                "SELECT url FROM monitored_pages WHERE site_id = ? ORDER BY position, id",
                (site_id,),
            )
        ]

    def upsert_site(
        self,
        *,
        domain: str,
        sitemap: str = "",
        pages: "list[str] | tuple[str, ...]" = (),
        enabled: bool = True,
        max_pages: int | None = None,
    ) -> int:
        """Create or replace one site and its page list."""
        now = utcnow()
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO monitored_sites
                       (domain, sitemap, enabled, max_pages, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(domain) DO UPDATE SET
                       sitemap = excluded.sitemap,
                       enabled = excluded.enabled,
                       max_pages = excluded.max_pages,
                       updated_at = excluded.updated_at
                """,
                (domain, sitemap, int(enabled), max_pages, now, now),
            )
            site_id = int(
                conn.execute(
                    "SELECT id FROM monitored_sites WHERE domain = ?", (domain,)
                ).fetchone()["id"]
            )
            # Replacing wholesale keeps page order exactly as given, which a
            # merge would not.
            conn.execute("DELETE FROM monitored_pages WHERE site_id = ?", (site_id,))
            seen: set[str] = set()
            ordered = []
            for url in pages:
                url = url.strip()
                if url and url not in seen:
                    seen.add(url)
                    ordered.append(url)
            conn.executemany(
                "INSERT INTO monitored_pages (site_id, url, position) VALUES (?, ?, ?)",
                [(site_id, url, index) for index, url in enumerate(ordered)],
            )
        return site_id

    def set_site_enabled(self, domain: str, enabled: bool) -> bool:
        with self._tx() as conn:
            cursor = conn.execute(
                "UPDATE monitored_sites SET enabled = ?, updated_at = ? WHERE domain = ?",
                (int(enabled), utcnow(), domain),
            )
        return cursor.rowcount > 0

    def delete_site(self, domain: str) -> bool:
        with self._tx() as conn:
            cursor = conn.execute(
                "DELETE FROM monitored_sites WHERE domain = ?", (domain,)
            )
        return cursor.rowcount > 0

    def site_count(self) -> int:
        return int(
            self._conn.execute("SELECT COUNT(*) FROM monitored_sites").fetchone()[0]
        )

    def import_sites(self, sites, *, replace: bool = False) -> int:
        """Bulk-load Site records. Returns how many were written."""
        if replace:
            with self._tx() as conn:
                conn.execute("DELETE FROM monitored_sites")
        for site in sites:
            self.upsert_site(
                domain=site.domain,
                sitemap=site.sitemap,
                pages=site.pages,
                enabled=site.enabled,
                max_pages=site.max_pages,
            )
        return len(sites)


    # -- editable settings -------------------------------------------------

    # Keys the dashboard is allowed to write. Anything outside this set is
    # environment-only, so a compromised session cannot repoint the database
    # or change where the app runs.
    EDITABLE_SETTINGS = (
        "telegram_bot_token",
        "telegram_chat_id",
        "pagespeed_api_key",
        "site_concurrency",
        "page_concurrency",
        "asset_concurrency",
        "request_timeout",
        "max_retries",
        "max_pages_per_site",
        "user_agent",
        "pagespeed_strategies",
        "pagespeed_concurrency",
    )

    def get_settings(self) -> dict[str, str]:
        return {
            row["key"]: row["value"]
            for row in self._conn.execute("SELECT key, value FROM app_settings")
        }

    def set_settings(self, values: dict[str, str]) -> None:
        """Write settings. An empty value clears the override."""
        now = utcnow()
        with self._tx() as conn:
            for key, value in values.items():
                if key not in self.EDITABLE_SETTINGS:
                    continue
                if value is None or str(value).strip() == "":
                    conn.execute("DELETE FROM app_settings WHERE key = ?", (key,))
                else:
                    conn.execute(
                        """
                        INSERT INTO app_settings (key, value, updated_at)
                        VALUES (?, ?, ?)
                        ON CONFLICT(key) DO UPDATE SET
                            value = excluded.value, updated_at = excluded.updated_at
                        """,
                        (key, str(value).strip(), now),
                    )

    def close_orphaned_runs(self) -> int:
        """Mark runs left mid-flight by a restart, and report how many.

        Only one process owns runs, and it starts with nothing in flight, so
        anything still `running` when the app boots was killed by a restart,
        a redeploy or a crash. Left alone it sits in the Reports list forever
        claiming to be in progress.
        """
        with self._tx() as conn:
            css = conn.execute(
                """
                UPDATE runs SET status = 'interrupted', finished_at = ?,
                       error = 'the app restarted while this run was in progress'
                 WHERE status = 'running'
                """,
                (utcnow(),),
            ).rowcount
            pagespeed = conn.execute(
                """
                UPDATE pagespeed_runs SET status = 'interrupted', finished_at = ?,
                       error = 'the app restarted while this test was in progress'
                 WHERE status = 'running'
                """,
                (utcnow(),),
            ).rowcount
        return css + pagespeed

    # -- schedules ---------------------------------------------------------

    def list_schedules(self, *, enabled_only: bool = False) -> list[sqlite3.Row]:
        query = "SELECT * FROM schedules"
        if enabled_only:
            query += " WHERE enabled = 1"
        return list(self._conn.execute(query + " ORDER BY id"))

    def get_schedule(self, schedule_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM schedules WHERE id = ?", (schedule_id,)
        ).fetchone()

    def create_schedule(
        self, *, name: str, kind: str, cron: str, enabled: bool = True
    ) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO schedules (name, kind, cron, enabled, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, kind, cron, int(enabled), utcnow()),
            )
        return int(cursor.lastrowid)

    def update_schedule(
        self, schedule_id: int, *, name: str, kind: str, cron: str, enabled: bool
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE schedules SET name = ?, kind = ?, cron = ?, enabled = ?
                 WHERE id = ?
                """,
                (name, kind, cron, int(enabled), schedule_id),
            )

    def delete_schedule(self, schedule_id: int) -> bool:
        with self._tx() as conn:
            cursor = conn.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
        return cursor.rowcount > 0

    def mark_schedule_run(
        self, schedule_id: int, *, status: str, next_run_at: str | None = None
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE schedules
                   SET last_run_at = ?, last_status = ?, next_run_at = ?
                 WHERE id = ?
                """,
                (utcnow(), status, next_run_at, schedule_id),
            )

    def set_schedule_next_run(self, schedule_id: int, next_run_at: str | None) -> None:
        with self._tx() as conn:
            conn.execute(
                "UPDATE schedules SET next_run_at = ? WHERE id = ?",
                (next_run_at, schedule_id),
            )

    # -- pagespeed ---------------------------------------------------------

    def start_pagespeed_run(self, *, trigger: str = "", expected: int = 0) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                """
                INSERT INTO pagespeed_runs (started_at, status, trigger, expected)
                VALUES (?, 'running', ?, ?)
                """,
                (utcnow(), trigger, expected),
            )
        return int(cursor.lastrowid)

    def finish_pagespeed_run(
        self,
        run_id: int,
        *,
        status: str,
        urls_tested: int,
        failures: int,
        error: str | None = None,
    ) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                UPDATE pagespeed_runs
                   SET finished_at = ?, status = ?, urls_tested = ?,
                       failures = ?, error = ?
                 WHERE id = ?
                """,
                (utcnow(), status, urls_tested, failures, error, run_id),
            )

    def record_pagespeed_result(self, run_id: int, result) -> None:
        with self._tx() as conn:
            conn.execute(
                """
                INSERT INTO pagespeed_results (
                    run_id, domain, url, strategy, performance, fcp_ms, lcp_ms,
                    cls, tbt_ms, speed_index, tti_ms, error, tested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    result.domain,
                    result.url,
                    result.strategy,
                    result.performance,
                    result.fcp_ms,
                    result.lcp_ms,
                    result.cls,
                    result.tbt_ms,
                    result.speed_index,
                    result.tti_ms,
                    result.error,
                    utcnow(),
                ),
            )

    def pagespeed_runs(self, limit: int = 50) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM pagespeed_runs ORDER BY id DESC LIMIT ?", (limit,)
            )
        )

    def pagespeed_results(
        self,
        *,
        run_id: int | None = None,
        domain: str | None = None,
        strategy: str | None = None,
        sort: str = "tested_at",
        direction: str = "desc",
        limit: int = 500,
    ) -> list[sqlite3.Row]:
        """Filtered, sorted results. Sort keys are whitelisted, never interpolated raw."""
        allowed = {
            "tested_at": "tested_at",
            "domain": "domain",
            "performance": "performance",
            "lcp": "lcp_ms",
            "cls": "cls",
            "tbt": "tbt_ms",
            "url": "url",
        }
        column = allowed.get(sort, "tested_at")
        order = "ASC" if str(direction).lower() == "asc" else "DESC"

        clauses: list[str] = []
        params: list = []
        if run_id is not None:
            clauses.append("run_id = ?")
            params.append(run_id)
        if domain:
            clauses.append("domain = ?")
            params.append(domain)
        if strategy:
            clauses.append("strategy = ?")
            params.append(strategy)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""

        # NULLs last so failed tests do not top a "worst performance" sort.
        params.append(limit)
        return list(
            self._conn.execute(
                f"SELECT * FROM pagespeed_results{where} "
                f"ORDER BY ({column} IS NULL), {column} {order}, id DESC LIMIT ?",
                params,
            )
        )

    def latest_pagespeed_run(self) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM pagespeed_runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def pagespeed_domains(self) -> list[str]:
        return [
            row[0]
            for row in self._conn.execute(
                "SELECT DISTINCT domain FROM pagespeed_results ORDER BY domain"
            )
        ]

    # -- reads -------------------------------------------------------------

    # How a run's stored trigger maps to the tabs on the reports page.
    # "manual" is the catch-all: it also covers runs recorded before triggers
    # were stored at all, which is the honest place to put them.
    SOURCE_FILTERS = {
        "scheduled": "trigger LIKE 'schedule:%'",
        "spot": "trigger LIKE 'site:%'",
        "manual": "trigger NOT LIKE 'schedule:%' AND trigger NOT LIKE 'site:%'",
    }

    def runs_by_source(self, source: str = "all", limit: int = 60) -> list[sqlite3.Row]:
        clause = self.SOURCE_FILTERS.get(source)
        where = f" WHERE {clause}" if clause else ""
        return list(
            self._conn.execute(
                f"SELECT * FROM runs{where} ORDER BY id DESC LIMIT ?", (limit,)
            )
        )

    def run_source_counts(self) -> dict[str, int]:
        """How many runs sit behind each tab."""
        counts = {
            "all": int(self._conn.execute("SELECT COUNT(*) FROM runs").fetchone()[0])
        }
        for name, clause in self.SOURCE_FILTERS.items():
            counts[name] = int(
                self._conn.execute(
                    f"SELECT COUNT(*) FROM runs WHERE {clause}"
                ).fetchone()[0]
            )
        return counts

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
            )
        )

    def latest_run(self) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM runs ORDER BY id DESC LIMIT 1"
        ).fetchone()

    def site_runs_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM site_runs WHERE run_id = ? ORDER BY domain", (run_id,)
            )
        )

    def page_errors_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM page_errors WHERE run_id = ? ORDER BY domain, page_url",
                (run_id,),
            )
        )

    def run(self, run_id: int) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT * FROM runs WHERE id = ?", (run_id,)
        ).fetchone()

    def breakage_history(self, domain: str, limit: int = 50) -> list[sqlite3.Row]:
        """Every recorded breakage for one site, newest first."""
        return list(
            self._conn.execute(
                """
                SELECT * FROM broken_assets
                 WHERE domain = ?
                 ORDER BY detected_at DESC, id DESC
                 LIMIT ?
                """,
                (domain, limit),
            )
        )

    def broken_assets_for_run(self, run_id: int) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                """
                SELECT * FROM broken_assets
                 WHERE run_id = ?
                 ORDER BY domain, page_url, asset_url
                """,
                (run_id,),
            )
        )

