"""SQLite data layer: schema, migrations, and all queries used by the app.

Uses aiosqlite for async access from FastAPI and the scraper engine.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Optional

import aiosqlite

from .models import Company, RawJob

logger = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS companies (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    slug TEXT NOT NULL,
    ats_platform TEXT NOT NULL,
    ats_board_url TEXT,
    career_url TEXT,
    last_scraped_at DATETIME,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(slug, ats_platform)
);

CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_id INTEGER NOT NULL REFERENCES companies(id),
    external_id TEXT,
    title TEXT NOT NULL,
    location TEXT,
    remote_type TEXT NOT NULL DEFAULT 'unknown',
    experience_level TEXT NOT NULL DEFAULT 'entry',
    category TEXT NOT NULL DEFAULT 'other',
    description TEXT,
    apply_url TEXT NOT NULL,
    salary_min INTEGER,
    salary_max INTEGER,
    salary_currency TEXT,
    departments TEXT,
    first_seen_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    is_active INTEGER NOT NULL DEFAULT 1,
    UNIQUE(company_id, external_id)
);

CREATE TABLE IF NOT EXISTS notified_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id INTEGER NOT NULL REFERENCES jobs(id),
    notified_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    batch_id TEXT
);

CREATE TABLE IF NOT EXISTS preferences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL UNIQUE,
    value TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_company ON jobs(company_id);
CREATE INDEX IF NOT EXISTS idx_jobs_active ON jobs(is_active);
CREATE INDEX IF NOT EXISTS idx_jobs_level ON jobs(experience_level);
CREATE INDEX IF NOT EXISTS idx_jobs_category ON jobs(category);
"""


class Database:
    """Async wrapper around the SQLite database."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self._db: Optional[aiosqlite.Connection] = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self.path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(_SCHEMA)
        await self._db.commit()

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Database not connected; call connect() first")
        return self._db

    # -- companies --------------------------------------------------------

    async def upsert_company(self, company: Company) -> int:
        """Insert or update a company; returns its row id."""
        cursor = await self.db.execute(
            """
            INSERT INTO companies (name, slug, ats_platform, ats_board_url, career_url)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(slug, ats_platform) DO UPDATE SET
                name = excluded.name,
                ats_board_url = COALESCE(excluded.ats_board_url, companies.ats_board_url),
                career_url = COALESCE(excluded.career_url, companies.career_url)
            """,
            (
                company.name,
                company.slug,
                company.ats_platform,
                company.ats_board_url,
                company.career_url,
            ),
        )
        await self.db.commit()
        row = await self.db.execute(
            "SELECT id FROM companies WHERE slug = ? AND ats_platform = ?",
            (company.slug, company.ats_platform),
        )
        found = await row.fetchone()
        return int(found["id"])

    async def get_companies(self, platform: Optional[str] = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM companies WHERE is_active = 1"
        params: list[Any] = []
        if platform:
            sql += " AND ats_platform = ?"
            params.append(platform)
        sql += " ORDER BY name"
        cursor = await self.db.execute(sql, params)
        return [dict(r) for r in await cursor.fetchall()]

    async def count_companies(self) -> int:
        cursor = await self.db.execute("SELECT COUNT(*) AS n FROM companies WHERE is_active = 1")
        row = await cursor.fetchone()
        return int(row["n"])

    async def mark_company_scraped(self, company_id: int, at: str) -> None:
        await self.db.execute(
            "UPDATE companies SET last_scraped_at = ? WHERE id = ?", (at, company_id)
        )
        await self.db.commit()

    async def set_company_active(self, company_id: int, active: bool) -> None:
        await self.db.execute(
            "UPDATE companies SET is_active = ? WHERE id = ?",
            (1 if active else 0, company_id),
        )
        await self.db.commit()

    # -- jobs -------------------------------------------------------------

    async def upsert_job(self, company_id: int, job: RawJob) -> bool:
        """Insert a job if new. Returns True when a new row was created."""
        cursor = await self.db.execute(
            """
            INSERT INTO jobs (
                company_id, external_id, title, location, remote_type,
                experience_level, category, description, apply_url,
                salary_min, salary_max, salary_currency, departments
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(company_id, external_id) DO NOTHING
            """,
            (
                company_id,
                job.external_id,
                job.title,
                job.location,
                job.remote_type,
                job.experience_level,
                job.category,
                job.description,
                job.apply_url,
                job.salary_min,
                job.salary_max,
                job.salary_currency,
                json.dumps(job.departments) if job.departments else None,
            ),
        )
        await self.db.commit()
        return cursor.rowcount > 0

    async def get_external_ids(self, company_id: int) -> set[str]:
        cursor = await self.db.execute(
            "SELECT external_id FROM jobs WHERE company_id = ?", (company_id,)
        )
        return {row["external_id"] for row in await cursor.fetchall()}

    async def deactivate_missing_jobs(self, company_id: int, keep_external_ids: set[str]) -> int:
        """Mark this company's jobs inactive unless their external_id is in keep set."""
        if keep_external_ids:
            placeholders = ",".join("?" for _ in keep_external_ids)
            cursor = await self.db.execute(
                f"""
                UPDATE jobs SET is_active = 0
                WHERE company_id = ? AND is_active = 1
                  AND external_id NOT IN ({placeholders})
                """,
                (company_id, *keep_external_ids),
            )
        else:
            cursor = await self.db.execute(
                "UPDATE jobs SET is_active = 0 WHERE company_id = ? AND is_active = 1",
                (company_id,),
            )
        await self.db.commit()
        return cursor.rowcount or 0

    async def query_jobs(
        self,
        search: Optional[str] = None,
        levels: Optional[list[str]] = None,
        categories: Optional[list[str]] = None,
        platforms: Optional[list[str]] = None,
        remote_only: bool = False,
        active_only: bool = True,
        page: int = 1,
        per_page: int = 50,
        sort: str = "newest",
    ) -> tuple[list[dict[str, Any]], int]:
        """Filter/paginate jobs. Returns (rows, total_matching)."""
        where: list[str] = []
        params: list[Any] = []
        if active_only:
            where.append("j.is_active = 1")
        if search:
            where.append(
                "(j.title LIKE ? OR c.name LIKE ? OR j.description LIKE ?)"
            )
            like = f"%{search}%"
            params += [like, like, like]
        if levels:
            where.append(f"j.experience_level IN ({','.join('?' for _ in levels)})")
            params += levels
        if categories:
            where.append(f"j.category IN ({','.join('?' for _ in categories)})")
            params += categories
        if platforms:
            where.append(f"c.ats_platform IN ({','.join('?' for _ in platforms)})")
            params += platforms
        if remote_only:
            where.append("j.remote_type = 'remote'")

        where_sql = f"WHERE {' AND '.join(where)}" if where else ""
        order_sql = {
            "newest": "j.first_seen_at DESC, j.id DESC",
            "oldest": "j.first_seen_at ASC, j.id ASC",
            "company": "c.name ASC, j.title ASC",
            "title": "j.title ASC",
        }.get(sort, "j.first_seen_at DESC, j.id DESC")

        count_cursor = await self.db.execute(
            f"SELECT COUNT(*) AS n FROM jobs j JOIN companies c ON c.id = j.company_id {where_sql}",
            params,
        )
        total = int((await count_cursor.fetchone())["n"])

        offset = max(0, (page - 1) * per_page)
        cursor = await self.db.execute(
            f"""
            SELECT j.*, c.name AS company_name, c.slug AS company_slug,
                   c.ats_platform AS ats_platform
            FROM jobs j JOIN companies c ON c.id = j.company_id
            {where_sql}
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?
            """,
            [*params, per_page, offset],
        )
        rows = [dict(r) for r in await cursor.fetchall()]
        return rows, total

    async def get_job(self, job_id: int) -> Optional[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT j.*, c.name AS company_name, c.slug AS company_slug,
                   c.ats_platform AS ats_platform, c.career_url AS company_career_url
            FROM jobs j JOIN companies c ON c.id = j.company_id
            WHERE j.id = ?
            """,
            (job_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None

    async def other_jobs_at_company(self, company_id: int, exclude_job_id: int, limit: int = 10) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT id, title, location, experience_level, apply_url
            FROM jobs
            WHERE company_id = ? AND id != ? AND is_active = 1
            ORDER BY first_seen_at DESC LIMIT ?
            """,
            (company_id, exclude_job_id, limit),
        )
        return [dict(r) for r in await cursor.fetchall()]

    async def job_status(self, job_id: int) -> str:
        """Return the user status ('saved'/'applied'/'ignored'/'' ) stored in preferences."""
        cursor = await self.db.execute(
            "SELECT value FROM preferences WHERE key = ?", (f"job_status_{job_id}",)
        )
        row = await cursor.fetchone()
        return row["value"] if row else ""

    async def set_job_status(self, job_id: int, status: str) -> None:
        if status:
            await self.db.execute(
                """
                INSERT INTO preferences (key, value) VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (f"job_status_{job_id}", status),
            )
        else:
            await self.db.execute(
                "DELETE FROM preferences WHERE key = ?", (f"job_status_{job_id}",)
            )
        await self.db.commit()

    async def jobs_by_status(self, status: str) -> list[dict[str, Any]]:
        cursor = await self.db.execute(
            """
            SELECT j.*, c.name AS company_name FROM jobs j
            JOIN companies c ON c.id = j.company_id
            JOIN preferences p ON p.key = 'job_status_' || j.id AND p.value = ?
            ORDER BY j.first_seen_at DESC
            """,
            (status,),
        )
        return [dict(r) for r in await cursor.fetchall()]

    # -- notifications ----------------------------------------------------

    async def is_notified(self, job_id: int) -> bool:
        cursor = await self.db.execute(
            "SELECT 1 FROM notified_jobs WHERE job_id = ? LIMIT 1", (job_id,)
        )
        return await cursor.fetchone() is not None

    async def mark_notified(self, job_ids: list[int], batch_id: str) -> None:
        await self.db.executemany(
            "INSERT INTO notified_jobs (job_id, batch_id) VALUES (?, ?)",
            [(job_id, batch_id) for job_id in job_ids],
        )
        await self.db.commit()

    # -- stats ------------------------------------------------------------

    async def stats(self) -> dict[str, Any]:
        def _rows(sql: str) -> list[dict[str, Any]]:
            return []

        async def _fetch(sql: str) -> list[dict[str, Any]]:
            cursor = await self.db.execute(sql)
            return [dict(r) for r in await cursor.fetchall()]

        total_cursor = await self.db.execute("SELECT COUNT(*) AS n FROM jobs WHERE is_active = 1")
        total = int((await total_cursor.fetchone())["n"])
        return {
            "active_jobs": total,
            "companies": await self.count_companies(),
            "by_platform": await _fetch(
                """
                SELECT c.ats_platform AS label, COUNT(*) AS n FROM jobs j
                JOIN companies c ON c.id = j.company_id
                WHERE j.is_active = 1 GROUP BY c.ats_platform ORDER BY n DESC
                """
            ),
            "by_category": await _fetch(
                "SELECT category AS label, COUNT(*) AS n FROM jobs WHERE is_active = 1 GROUP BY category ORDER BY n DESC"
            ),
            "by_level": await _fetch(
                "SELECT experience_level AS label, COUNT(*) AS n FROM jobs WHERE is_active = 1 GROUP BY experience_level ORDER BY n DESC"
            ),
            "by_day": await _fetch(
                """
                SELECT DATE(first_seen_at) AS label, COUNT(*) AS n FROM jobs
                WHERE first_seen_at >= DATE('now', '-14 days')
                GROUP BY DATE(first_seen_at) ORDER BY label
                """
            ),
            "top_companies": await _fetch(
                """
                SELECT c.name AS label, COUNT(*) AS n FROM jobs j
                JOIN companies c ON c.id = j.company_id
                WHERE j.is_active = 1 GROUP BY c.name ORDER BY n DESC LIMIT 10
                """
            ),
        }

    async def scraper_status(self) -> dict[str, Any]:
        last_cursor = await self.db.execute("SELECT MAX(last_scraped_at) AS t FROM companies")
        last_run = (await last_cursor.fetchone())["t"]
        active_cursor = await self.db.execute("SELECT COUNT(*) AS n FROM jobs WHERE is_active = 1")
        active = int((await active_cursor.fetchone())["n"])
        return {"last_run": last_run, "active_jobs": active, "companies": await self.count_companies()}
