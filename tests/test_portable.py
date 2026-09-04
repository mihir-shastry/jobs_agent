"""End-to-end portable pipeline tests with all external calls mocked."""

import json

import httpx
import pytest
import respx

from jobagent.config import Config
from jobagent.portable import run_portable_cycle
from jobagent.seeding import DATA_DIR, SEED_FILE


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    return Config(
        {
            "scraping": {"interval_minutes": 60, "max_concurrent_requests": 5, "request_delay_ms": 0},
            "ats_platforms": {"greenhouse": {"enabled": True}, "lever": {"enabled": False}, "ashby": {"enabled": False}},
            "notifications": {
                "slack_webhook_url": "https://hooks.slack.com/services/T/B/X",
                "enabled": True,
                "filters": {"experience_levels": ["internship"], "categories": ["swe"]},
            },
            "database": {"path": str(tmp_path / "unused.db")},
        },
        base_dir=tmp_path,
    )


@pytest.fixture
def seed_file(tmp_path):
    """A two-company seed file pointing at mocked boards."""
    path = tmp_path / "seeds.json"
    path.write_text(json.dumps([
        {"name": "Acme", "slug": "acme", "ats_platform": "greenhouse",
         "ats_board_url": "https://boards.greenhouse.io/acme"},
        {"name": "Beta", "slug": "beta", "ats_platform": "greenhouse",
         "ats_board_url": "https://boards.greenhouse.io/beta"},
    ]))
    return path


def _mock_boards():
    respx.get("https://api.greenhouse.io/v1/boards/acme/jobs").respond(
        json={"jobs": [
            {"id": 1, "title": "Software Engineer Intern", "absolute_url": "https://gh/1",
             "location": {"name": "Remote"}, "first_published": "2026-02-14T12:00:00Z"},
            {"id": 2, "title": "Senior Data Scientist", "absolute_url": "https://gh/2",
             "location": {"name": "NYC"}},
        ]}
    )
    respx.get("https://api.greenhouse.io/v1/boards/beta/jobs").respond(
        json={"jobs": [
            {"id": 10, "title": "SWE Intern (Summer)", "absolute_url": "https://gh/10",
             "location": {"name": "Remote"}},
        ]}
    )


class TestPortableCycle:
    @respx.mock
    async def test_first_run_all_new_and_digest_sent(self, config, tmp_path, seed_file):
        _mock_boards()
        slack_route = respx.post("https://hooks.slack.com/services/T/B/X").respond(status_code=200)

        seen = tmp_path / "seen_jobs.jsonl"
        export = tmp_path / "jobs.json"
        summary = await run_portable_cycle(config, seen, export, seed_file=seed_file)

        assert summary["boards_scraped"] == 2
        assert summary["jobs_found"] == 3
        assert summary["new_jobs"] == 3
        assert summary["digest_sent"] is True
        assert slack_route.called

        payload = json.loads(slack_route.calls.last.request.content)
        text = json.dumps(payload)
        assert "Software Engineer Intern" in text
        assert "Senior Data Scientist" not in text  # filtered out (level+category)

        # Export contains all 3 active jobs, with the ATS publish date.
        data = json.loads(export.read_text())
        assert data["total"] == 3
        assert len(data["jobs"]) == 3
        assert "description" not in data["jobs"][0]
        by_title = {j["title"]: j for j in data["jobs"]}
        assert by_title["Software Engineer Intern"]["posted_at"] == "2026-02-14T12:00:00+00:00"
        assert by_title["Senior Data Scientist"]["posted_at"] is None

        # Seen file has all 3 keys.
        keys = [json.loads(line)["k"] for line in seen.read_text().strip().splitlines()]
        assert sorted(keys) == ["greenhouse:acme:1", "greenhouse:acme:2", "greenhouse:beta:10"]

    @respx.mock
    async def test_second_run_no_new_no_digest(self, config, tmp_path, seed_file):
        _mock_boards()
        slack_route = respx.post("https://hooks.slack.com/services/T/B/X").respond(status_code=200)

        seen = tmp_path / "seen_jobs.jsonl"
        export = tmp_path / "jobs.json"

        await run_portable_cycle(config, seen, export, seed_file=seed_file)
        assert slack_route.call_count == 1

        summary2 = await run_portable_cycle(config, seen, export, seed_file=seed_file)
        assert summary2["new_jobs"] == 0
        assert summary2["digest_sent"] is False
        assert summary2["notification"]["reason"] == "no_jobs"
        assert slack_route.call_count == 1  # no new digest

    @respx.mock
    async def test_job_disappears_next_run_excluded_from_export(self, config, tmp_path, seed_file):
        respx.post("https://hooks.slack.com/services/T/B/X").respond(status_code=200)
        route = respx.get("https://api.greenhouse.io/v1/boards/acme/jobs")
        route.respond(json={"jobs": [
            {"id": 1, "title": "Software Engineer Intern", "absolute_url": "https://gh/1",
             "location": {"name": "Remote"}},
        ]})
        respx.get("https://api.greenhouse.io/v1/boards/beta/jobs").respond(json={"jobs": []})

        seen = tmp_path / "seen_jobs.jsonl"
        export = tmp_path / "jobs.json"
        await run_portable_cycle(config, seen, export, seed_file=seed_file)
        assert json.loads(export.read_text())["total"] == 1

        # Job 1 vanishes from the board.
        route.respond(json={"jobs": []})
        summary2 = await run_portable_cycle(config, seen, export, seed_file=seed_file)
        assert summary2["jobs_found"] == 0
        data = json.loads(export.read_text())
        assert data["total"] == 0
        # Seen key is retained (prunes only after retention window).
        assert "greenhouse:acme:1" in seen.read_text()

    @respx.mock
    async def test_slack_failure_still_exports(self, config, tmp_path, seed_file, monkeypatch):
        monkeypatch.setattr("jobagent.notifier._RETRY_DELAY_SECONDS", 0)
        _mock_boards()
        respx.post("https://hooks.slack.com/services/T/B/X").respond(status_code=500)

        seen = tmp_path / "seen_jobs.jsonl"
        export = tmp_path / "jobs.json"
        summary = await run_portable_cycle(config, seen, export, seed_file=seed_file)

        assert summary["digest_sent"] is False
        assert summary["notification"]["reason"] == "slack_unreachable"
        assert summary["notification"]["attempted"] is True
        assert summary["new_jobs"] == 3
        assert json.loads(export.read_text())["total"] == 3
        assert (tmp_path / "seen_jobs.jsonl").exists()

    @respx.mock
    async def test_notification_report_fields_on_success(self, config, tmp_path, seed_file):
        _mock_boards()
        respx.post("https://hooks.slack.com/services/T/B/X").respond(status_code=200)
        seen = tmp_path / "seen_jobs.jsonl"
        export = tmp_path / "jobs.json"
        summary = await run_portable_cycle(config, seen, export, seed_file=seed_file)
        report = summary["notification"]
        assert report["attempted"] is True
        assert report["sent"] is True
        assert report["reason"] == "ok"
        assert report["matching_jobs"] == 2  # intern jobs match; senior doesn't

    @respx.mock
    async def test_test_digest_mode_sends_without_new_jobs(self, config, tmp_path, seed_file, monkeypatch):
        monkeypatch.setattr("jobagent.notifier._RETRY_DELAY_SECONDS", 0)
        _mock_boards()
        slack_route = respx.post("https://hooks.slack.com/services/T/B/X").respond(status_code=200)
        seen = tmp_path / "seen_jobs.jsonl"
        export = tmp_path / "jobs.json"

        summary = await run_portable_cycle(config, seen, export, seed_file=seed_file, test_digest=True)
        assert summary["digest_sent"] is True
        assert summary["notification"]["reason"] == "forced_test"
        payload = json.loads(slack_route.calls.last.request.content)
        text = json.dumps(payload)
        # Test digest shows the filter-matching jobs (what the user would get).
        assert "Software Engineer Intern" in text
        assert "Senior Data Scientist" not in text

        # test_digest still records seen keys and exports normally.
        keys = [json.loads(line)["k"] for line in seen.read_text().strip().splitlines()]
        assert len(keys) == 3
        assert json.loads(export.read_text())["total"] == 3

        # A subsequent regular run finds nothing new (state stayed clean).
        summary2 = await run_portable_cycle(config, seen, export, seed_file=seed_file)
        assert summary2["new_jobs"] == 0
