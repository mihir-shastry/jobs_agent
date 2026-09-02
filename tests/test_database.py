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


class TestStats:
    async def test_stats_shape(self, db, company_id):
        await db.upsert_job(company_id, _job("j1", "Software Engineer"))
        stats = await db.stats()
        assert stats["active_jobs"] == 1
        assert stats["companies"] == 1
        assert stats["by_platform"][0]["label"] == "greenhouse"
        status = await db.scraper_status()
        assert status["active_jobs"] == 1
