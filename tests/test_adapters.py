"""Adapter normalization tests using respx-mocked HTTP."""

import httpx
import pytest
import respx

from jobagent.scraper.ashby import AshbyAdapter
from jobagent.scraper.base import NotFoundError
from jobagent.scraper.greenhouse import GreenhouseAdapter
from jobagent.scraper.lever import LeverAdapter


@pytest.fixture
async def client():
    async with httpx.AsyncClient() as c:
        yield c


@pytest.fixture
def gh_client(client):
    return GreenhouseAdapter(client)


@pytest.fixture
def lever_client(client):
    return LeverAdapter(client)


@pytest.fixture
def ashby_client(client):
    return AshbyAdapter(client)


class TestGreenhouse:
    @respx.mock
    async def test_parse_jobs(self, gh_client):
        respx.get("https://api.greenhouse.io/v1/boards/acme/jobs").respond(
            json={
                "jobs": [
                    {
                        "id": 123,
                        "title": "Software Engineer Intern",
                        "absolute_url": "https://boards.greenhouse.io/acme/jobs/123",
                        "location": {"name": "San Francisco"},
                        "content": "<p>Build things</p><script>alert(1)</script>",
                        "departments": [{"name": "Engineering"}],
                    }
                ]
            }
        )
        jobs = await gh_client.fetch_jobs("acme")
        assert len(jobs) == 1
        job = jobs[0]
        assert job.external_id == "123"
        assert job.title == "Software Engineer Intern"
        assert job.location == "San Francisco"
        assert job.departments == ["Engineering"]
        assert job.description is not None
        assert "script" not in job.description

    @respx.mock
    async def test_404_raises(self, gh_client):
        respx.get("https://api.greenhouse.io/v1/boards/missing/jobs").respond(status_code=404)
        with pytest.raises(NotFoundError):
            await gh_client.fetch_jobs("missing")

    @respx.mock
    async def test_malformed_skipped(self, gh_client):
        respx.get("https://api.greenhouse.io/v1/boards/acme/jobs").respond(
            json={"jobs": [{"id": 1, "title": ""}, {"id": 2, "title": "OK Job", "absolute_url": "https://x"}]}
        )
        jobs = await gh_client.fetch_jobs("acme")
        assert len(jobs) == 1
        assert jobs[0].title == "OK Job"


class TestLever:
    @respx.mock
    async def test_parse_jobs(self, lever_client):
        respx.get("https://api.lever.co/v0/postings/acme").respond(
            json=[
                {
                    "id": "abc-123",
                    "text": "Backend Engineer",
                    "hostedUrl": "https://jobs.lever.co/acme/abc-123",
                    "categories": {"location": "Remote", "department": "Engineering", "team": "Core"},
                    "descriptionPlain": "Do backend things",
                }
            ]
        )
        jobs = await lever_client.fetch_jobs("acme")
        assert len(jobs) == 1
        job = jobs[0]
        assert job.external_id == "abc-123"
        assert job.title == "Backend Engineer"
        assert job.location == "Remote"
        assert job.departments == ["Engineering", "Core"]

    @respx.mock
    async def test_wrong_shape_raises(self, lever_client):
        respx.get("https://api.lever.co/v0/postings/acme").respond(json={"oops": True})
        with pytest.raises(Exception):
            await lever_client.fetch_jobs("acme")


class TestPostedDates:
    @respx.mock
    async def test_greenhouse_first_published(self, gh_client):
        respx.get("https://api.greenhouse.io/v1/boards/acme/jobs").respond(
            json={"jobs": [
                {"id": 1, "title": "Eng", "absolute_url": "https://x/1",
                 "first_published": "2026-02-14T12:30:00-05:00"},
                {"id": 2, "title": "No date", "absolute_url": "https://x/2"},
                {"id": 3, "title": "Bad date", "absolute_url": "https://x/3",
                 "first_published": "not-a-date"},
            ]}
        )
        jobs = await gh_client.fetch_jobs("acme")
        assert jobs[0].posted_at == "2026-02-14T17:30:00+00:00"  # normalized to UTC
        assert jobs[1].posted_at is None
        assert jobs[2].posted_at is None  # unparsable never breaks the scrape

    @respx.mock
    async def test_ashby_published_at(self, ashby_client):
        respx.get("https://api.ashbyhq.com/posting-api/job-board/acme?includeCompensation=true").respond(
            json={"jobs": [
                {"id": "j1", "title": "SWE", "jobUrl": "https://x/1",
                 "publishedAt": "2026-01-02T03:04:05.000Z"},
            ]}
        )
        jobs = await ashby_client.fetch_jobs("acme")
        assert jobs[0].posted_at == "2026-01-02T03:04:05+00:00"

    @respx.mock
    async def test_lever_fetches_dates_for_new_jobs_only(self, lever_client):
        respx.get("https://api.lever.co/v0/postings/acme").respond(
            json=[
                {"id": "known", "text": "Old job", "hostedUrl": "https://lv/known"},
                {"id": "fresh", "text": "New job", "hostedUrl": "https://lv/fresh"},
            ]
        )
        detail = respx.get("https://api.lever.co/v0/postings/acme/fresh").respond(
            json={"id": "fresh", "createdAt": 1767225600000}  # 2026-01-01T00:00:00Z
        )

        jobs = await lever_client.fetch_jobs("acme", known_external_ids={"known"})
        by_id = {j.external_id: j for j in jobs}
        assert by_id["fresh"].posted_at == "2026-01-01T00:00:00+00:00"
        assert by_id["known"].posted_at is None
        assert detail.call_count == 1

        # A second pass with both IDs known skips the per-posting endpoint.
        await lever_client.fetch_jobs("acme", known_external_ids={"known", "fresh"})
        assert detail.call_count == 1

    @respx.mock
    async def test_lever_date_fetch_failure_is_best_effort(self, lever_client):
        respx.get("https://api.lever.co/v0/postings/acme").respond(
            json=[{"id": "fresh", "text": "New job", "hostedUrl": "https://lv/fresh"}]
        )
        respx.get("https://api.lever.co/v0/postings/acme/fresh").respond(status_code=500)
        jobs = await lever_client.fetch_jobs("acme")
        assert len(jobs) == 1
        assert jobs[0].posted_at is None

    def test_parse_posted_at_variants(self):
        from jobagent.scraper.base import ATSAdapter

        parse = ATSAdapter._parse_posted_at
        assert parse(1767225600000) == "2026-01-01T00:00:00+00:00"      # epoch ms (Lever)
        assert parse("2026-02-14T12:30:00Z") == "2026-02-14T12:30:00+00:00"
        assert parse("2026-02-14T12:30:00") == "2026-02-14T12:30:00+00:00"  # naive → UTC
        assert parse("  ") is None
        assert parse("garbage") is None
        assert parse(0) is None
        assert parse(None) is None


class TestAshby:
    @respx.mock
    async def test_parse_jobs(self, ashby_client):
        respx.get("https://api.ashbyhq.com/posting-api/job-board/acme").respond(
            json={
                "jobs": [
                    {
                        "id": "job-9",
                        "title": "ML Engineer, New Grad",
                        "jobUrl": "https://jobs.ashbyhq.com/acme/job-9",
                        "location": "New York",
                        "jobDescription": "<p>Models!</p>",
                        "department": "AI",
                        "compensation": {"compensationIntervalSummary": "$120,000 — $180,000"},
                    }
                ]
            }
        )
        jobs = await ashby_client.fetch_jobs("acme")
        assert len(jobs) == 1
        job = jobs[0]
        assert job.external_id == "job-9"
        assert job.salary_min == 120000
        assert job.salary_max == 180000
        assert job.salary_currency == "USD"
        assert job.departments == ["AI"]
