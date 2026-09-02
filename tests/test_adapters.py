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
