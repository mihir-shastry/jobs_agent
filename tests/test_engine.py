"""Engine scrape-cycle test with all three ATS endpoints mocked."""

import httpx
import pytest
import respx

from jobagent.config import Config
from jobagent.database import Database
from jobagent.engine import ScrapeEngine
from jobagent.models import Company


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    return Config(
        {
            "scraping": {"interval_minutes": 60, "max_concurrent_requests": 5, "request_delay_ms": 0},
            "ats_platforms": {"greenhouse": {"enabled": True}, "lever": {"enabled": True}, "ashby": {"enabled": False}},
            "notifications": {"slack_webhook_url": "", "enabled": True},
            "database": {"path": str(tmp_path / "e.db")},
        },
        base_dir=tmp_path,
    )


@pytest.fixture
async def db(config):
    database = Database(config.database_path)
    await database.connect()
    yield database
    await database.close()


async def _seed(db):
    await db.upsert_company(Company(name="Acme", slug="acme", ats_platform="greenhouse"))
    await db.upsert_company(Company(name="Beta", slug="beta", ats_platform="lever"))
    await db.upsert_company(Company(name="Gone", slug="gone", ats_platform="greenhouse"))


class TestRunCycle:
    @respx.mock
    async def test_full_cycle(self, config, db):
        await _seed(db)

        respx.get("https://api.greenhouse.io/v1/boards/acme/jobs").respond(
            json={"jobs": [
                {"id": 1, "title": "Software Engineer Intern", "absolute_url": "https://gh/1",
                 "location": {"name": "Remote"}},
                {"id": 2, "title": "Senior Backend Engineer", "absolute_url": "https://gh/2",
                 "location": {"name": "NYC"}},
            ]}
        )
        # Board disappeared → company deactivated.
        respx.get("https://api.greenhouse.io/v1/boards/gone/jobs").respond(status_code=404)
        respx.get("https://api.lever.co/v0/postings/beta").respond(
            json=[
                {"id": "l1", "text": "Data Science Intern", "hostedUrl": "https://lv/1",
                 "categories": {"location": "San Francisco"}},
            ]
        )

        engine = ScrapeEngine(config, db)
        results = await engine.run_cycle()

        by_slug = {r.company_slug: r for r in results}
        assert by_slug["acme"].jobs_new == 2
        assert by_slug["beta"].jobs_new == 1
        assert by_slug["gone"].error == "board not found"

        rows, total = await db.query_jobs()
        assert total == 3
        acme_rows = [r for r in rows if r["company_slug"] == "acme"]
        intern = next(r for r in acme_rows if r["external_id"] == "1")
        assert intern["experience_level"] == "internship"
        assert intern["remote_type"] == "remote"
        senior = next(r for r in acme_rows if r["external_id"] == "2")
        assert senior["experience_level"] == "senior"
        assert senior["category"] == "swe"

        companies = {c["slug"]: c for c in await db.get_companies()}
        assert "gone" not in companies  # deactivated

        # Second run: no new jobs, no errors on active boards.
        results2 = await engine.run_cycle()
        by_slug2 = {r.company_slug: r for r in results2}
        assert by_slug2["acme"].jobs_new == 0
        assert by_slug2["beta"].jobs_new == 0
