"""Notifier tests: filter matching, digest blocks, send behavior."""

import json

import httpx
import pytest
import respx

from jobagent.config import Config
from jobagent.database import Database
from jobagent.models import Company, RawJob
from jobagent.notifier import Notifier


@pytest.fixture
def config(tmp_path, monkeypatch):
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    return Config(
        {
            "notifications": {
                "slack_webhook_url": "https://hooks.slack.com/services/TEST/URL",
                "enabled": True,
                "filters": {"experience_levels": ["internship"], "categories": ["swe"]},
            },
            "database": {"path": str(tmp_path / "n.db")},
        },
        base_dir=tmp_path,
    )


@pytest.fixture
async def db(config):
    database = Database(config.database_path)
    await database.connect()
    yield database
    await database.close()


@pytest.fixture
async def seed(db):
    company_id = await db.upsert_company(Company(name="Acme", slug="acme", ats_platform="greenhouse"))
    ids = {}
    for ext, title in (
        ("j1", "Software Engineer Intern"),       # matches
        ("j2", "Senior Data Scientist"),          # wrong level + category
        ("j3", "Product Design Intern"),          # wrong category
        ("j4", "SWE Intern (Summer)"),            # matches
    ):
        raw = RawJob(external_id=ext, title=title, apply_url=f"https://apply/{ext}", location="Remote")
        from jobagent.classifier import classify
        raw.experience_level, raw.category = classify(title)
        await db.upsert_job(company_id, raw)
        rows, _ = await db.query_jobs(active_only=False)
        ids[ext] = [r["id"] for r in rows if r["external_id"] == ext][0]
    return ids


class TestNotify:
    async def test_filters_and_sends(self, config, db, seed):
        notifier = Notifier(config, db)
        sent_payloads = []

        @respx.mock
        async def _run():
            route = respx.post("https://hooks.slack.com/services/TEST/URL").respond(status_code=200)
            result = await notifier.notify_new_jobs([seed["j1"], seed["j2"], seed["j3"], seed["j4"]])
            assert result.sent is True
            assert result.reason == "ok"
            assert result.matching_jobs == 2
            assert route.called
            sent_payloads.append(route.calls.last.request.content)

        await _run()

        payload = json.loads(sent_payloads[0])
        text = json.dumps(payload)
        assert "Software Engineer Intern" in text
        assert "SWE Intern" in text
        assert "Data Scientist" not in text
        assert "Design Intern" not in text

    async def test_jobs_marked_notified_even_without_send(self, config, db, seed):
        config._data["notifications"]["slack_webhook_url"] = ""  # no webhook
        notifier = Notifier(config, db)
        result = await notifier.notify_new_jobs([seed["j1"]])
        assert result.sent is False
        assert result.reason == "no_webhook"
        assert await db.is_notified(seed["j1"]) is True

    async def test_no_notification_when_disabled(self, config, db, seed):
        config._data["notifications"]["enabled"] = False
        notifier = Notifier(config, db)
        report = await notifier.notify_new_jobs([seed["j1"]])
        assert report.sent is False
        assert report.reason == "notifications_disabled"

    @respx.mock
    async def test_send_failure_still_records(self, config, db, seed, monkeypatch):
        monkeypatch.setattr("jobagent.notifier._RETRY_DELAY_SECONDS", 0)
        respx.post("https://hooks.slack.com/services/TEST/URL").respond(status_code=500)
        notifier = Notifier(config, db)
        result = await notifier.notify_new_jobs([seed["j1"]])
        assert result.sent is False
        assert result.reason == "slack_unreachable"
        assert await db.is_notified(seed["j1"]) is True

    @respx.mock
    async def test_retries_on_5xx_then_succeeds(self, config, db, seed, monkeypatch):
        monkeypatch.setattr("jobagent.notifier._RETRY_DELAY_SECONDS", 0)
        route = respx.post("https://hooks.slack.com/services/TEST/URL")
        route.side_effect = [httpx.Response(500), httpx.Response(200)]
        notifier = Notifier(config, db)
        result = await notifier.notify_new_jobs([seed["j1"]])
        assert result.sent is True
        assert result.reason == "ok"
        assert route.call_count == 2

    async def test_no_matching_jobs_reason(self, config, db, seed):
        notifier = Notifier(config, db)
        report = await notifier.notify_new_jobs([seed["j2"]])  # senior, wrong category
        assert report.sent is False
        assert report.reason == "no_matching_jobs"
        assert report.matching_jobs == 0

    @respx.mock
    async def test_test_digest_forces_send_without_recording(self, config, db, seed):
        respx.post("https://hooks.slack.com/services/TEST/URL").respond(status_code=200)
        notifier = Notifier(config, db)
        report = await notifier.notify_test_digest([seed["j2"]], apply_filters=False)
        assert report.sent is True
        assert report.reason == "forced_test"
        assert report.matching_jobs == 1
        # Failed/forced sends are deliberately not recorded as notified.
        assert await db.is_notified(seed["j2"]) is False

    def test_digest_block_structure(self, config, db, seed):
        notifier = Notifier(config, db)
        jobs = [
            {"experience_level": "internship", "title": "SWE Intern", "company_name": "Acme",
             "location": "Remote", "apply_url": "https://a/1",
             "posted_at": "2026-02-14T12:00:00+00:00"},
            {"experience_level": "new_grad", "title": "NG SWE", "company_name": "Beta",
             "location": None, "apply_url": "https://a/2"},
        ]
        blocks = notifier._build_digest_blocks(jobs)
        assert blocks[0]["type"] == "header"
        sections = [b for b in blocks if b["type"] == "section"]
        assert len(sections) == 2
        assert "Internships (1)" in sections[0]["text"]["text"]
        assert "New Grad (1)" in sections[1]["text"]["text"]
        assert "· posted Feb 14" in sections[0]["text"]["text"]
        assert "posted" not in sections[1]["text"]["text"]
