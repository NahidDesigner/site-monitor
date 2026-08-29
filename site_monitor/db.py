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
    error          TEXT
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
        self._conn.commit()

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

    def start_run(self) -> int:
        with self._tx() as conn:
            cursor = conn.execute(
                "INSERT INTO runs (started_at, status) VALUES (?, 'running')",
                (utcnow(),),
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

    # -- reads -------------------------------------------------------------

    def recent_runs(self, limit: int = 10) -> list[sqlite3.Row]:
        return list(
            self._conn.execute(
                "SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)
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

