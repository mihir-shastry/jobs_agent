"""Database layer tests against a temp SQLite file."""

import pytest

from jobagent.database import Database
from jobagent.models import RawJob


@pytest.fixture
async def db(tmp_path):
    database = Database(tmp_path / "test.db")
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def company_id(db):
    from jobagent.models import Company

    return await db.upsert_company(
        Company(name="Acme", slug="acme", ats_platform="greenhouse",
                ats_board_url="https://boards.greenhouse.io/acme")
    )


def _job(external_id="j1", title="Software Engineer Intern", apply_url="https://apply/1"):
    return RawJob(external_id=external_id, title=title, apply_url=apply_url, location="Remote")


class TestCompanies:
    async def test_upsert_is_idempotent(self, db):
        from jobagent.models import Company

        c = Company(name="Acme", slug="acme", ats_platform="greenhouse")
        id1 = await db.upsert_company(c)
        id2 = await db.upsert_company(c)
        assert id1 == id2
        assert await db.count_companies() == 1

    async def test_get_by_platform(self, db):
        from jobagent.models import Company

        await db.upsert_company(Company(name="A", slug="a", ats_platform="greenhouse"))
        await db.upsert_company(Company(name="B", slug="b", ats_platform="lever"))
        gh = await db.get_companies(platform="greenhouse")
        assert len(gh) == 1 and gh[0]["slug"] == "a"


class TestJobs:
    async def test_upsert_dedupes(self, db, company_id):
        assert await db.upsert_job(company_id, _job()) is True
        assert await db.upsert_job(company_id, _job()) is False

    async def test_deactivate_missing(self, db, company_id):
        await db.upsert_job(company_id, _job("j1"))
        await db.upsert_job(company_id, _job("j2"))
        n = await db.deactivate_missing_jobs(company_id, {"j1"})
        assert n == 1
        rows, total = await db.query_jobs()
        assert total == 1
        rows_all, _ = await db.query_jobs(active_only=False)
        assert len(rows_all) == 2

    async def test_query_filters(self, db, company_id):
        await db.upsert_job(company_id, _job("j1", "Software Engineer Intern"))
        await db.upsert_job(company_id, _job("j2", "Senior Data Scientist", "https://apply/2"))
        from jobagent.classifier import classify
        for ext_id, title in (("j1", "Software Engineer Intern"), ("j2", "Senior Data Scientist")):
            pass
        # classify happens in engine; emulate here:
        rows, _ = await db.query_jobs(search="data")
        # titles searchable
        assert any("Data" in r["title"] for r in rows)

    async def test_status_flow(self, db, company_id):
        await db.upsert_job(company_id, _job("j1"))
        rows, _ = await db.query_jobs()
        job_id = rows[0]["id"]
        assert await db.job_status(job_id) == ""
        await db.set_job_status(job_id, "saved")
        assert await db.job_status(job_id) == "saved"
        saved = await db.jobs_by_status("saved")
        assert len(saved) == 1
        await db.set_job_status(job_id, "")
        assert await db.job_status(job_id) == ""


class TestNotifications:
    async def test_mark_and_check(self, db, company_id):
        await db.upsert_job(company_id, _job("j1"))
        rows, _ = await db.query_jobs()
        job_id = rows[0]["id"]
        assert await db.is_notified(job_id) is False
        await db.mark_notified([job_id], "batch1")
        assert await db.is_notified(job_id) is True


class TestPostedAt:
    async def test_posted_at_stored_and_exported(self, db, company_id):
        from jobagent.models import RawJob

        job = _job("j1")
        job.posted_at = "2026-02-14T12:00:00+00:00"
        await db.upsert_job(company_id, job)
        rows, _ = await db.query_jobs()
        assert rows[0]["posted_at"] == "2026-02-14T12:00:00+00:00"

    async def test_sort_by_posted_date_falls_back_to_first_seen(self, db, company_id):
        from datetime import datetime, timedelta, timezone

        base = datetime.now(timezone.utc)
        old = _job("old")
        old.posted_at = (base - timedelta(days=10)).isoformat(timespec="seconds")
        new = _job("new")
        new.posted_at = (base - timedelta(days=1)).isoformat(timespec="seconds")
        undated = _job("undated")  # no posted_at → first_seen (now) fallback
        await db.upsert_job(company_id, old)
        await db.upsert_job(company_id, undated)
        await db.upsert_job(company_id, new)

        rows, _ = await db.query_jobs(sort="newest")
        assert [r["external_id"] for r in rows] == ["undated", "new", "old"]
        rows, _ = await db.query_jobs(sort="oldest")
        assert [r["external_id"] for r in rows] == ["old", "new", "undated"]

    async def test_migration_adds_posted_at(self, tmp_path, company_id_factory=None):
        """A legacy DB without the column migrates cleanly and keeps data."""
        import aiosqlite

        legacy = tmp_path / "legacy.db"
        async with aiosqlite.connect(legacy) as conn:
            # Full pre-migration schema: everything except posted_at.
            await conn.execute(
                """
                CREATE TABLE jobs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    company_id INTEGER NOT NULL,
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
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            await conn.execute(
                "INSERT INTO jobs (company_id, external_id, title, apply_url) VALUES (1, 'x', 'T', 'u')"
            )
            # query_jobs joins on companies; give the job its parent row.
            await conn.execute(
                """
                CREATE TABLE companies (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    slug TEXT NOT NULL,
                    ats_platform TEXT NOT NULL,
                    is_active INTEGER NOT NULL DEFAULT 1
                )
                """
            )
            await conn.execute(
                "INSERT INTO companies (name, slug, ats_platform) VALUES ('Acme', 'acme', 'greenhouse')"
            )
            await conn.commit()

        db2 = Database(legacy)
        await db2.connect()
        try:
            rows, total = await db2.query_jobs(active_only=False)
            assert total == 1
            assert rows[0]["posted_at"] is None
        finally:
            await db2.close()


class TestStats:
    async def test_stats_shape(self, db, company_id):
        await db.upsert_job(company_id, _job("j1", "Software Engineer"))
        stats = await db.stats()
        assert stats["active_jobs"] == 1
        assert stats["companies"] == 1
        assert stats["by_platform"][0]["label"] == "greenhouse"
        status = await db.scraper_status()
        assert status["active_jobs"] == 1
