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
            assert result is True
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
        assert result is False
        assert await db.is_notified(seed["j1"]) is True

    async def test_no_notification_when_disabled(self, config, db, seed):
        config._data["notifications"]["enabled"] = False
        notifier = Notifier(config, db)
        assert await notifier.notify_new_jobs([seed["j1"]]) is False

    @respx.mock
    async def test_send_failure_still_records(self, config, db, seed):
        respx.post("https://hooks.slack.com/services/TEST/URL").respond(status_code=500)
        notifier = Notifier(config, db)
        result = await notifier.notify_new_jobs([seed["j1"]])
        assert result is False
        assert await db.is_notified(seed["j1"]) is True

    def test_digest_block_structure(self, config, db, seed):
        notifier = Notifier(config, db)
        jobs = [
            {"experience_level": "internship", "title": "SWE Intern", "company_name": "Acme",
             "location": "Remote", "apply_url": "https://a/1"},
            {"experience_level": "new_grad", "title": "NG SWE", "company_name": "Beta",
             "location": None, "apply_url": "https://a/2"},
        ]
        blocks = notifier._build_digest_blocks(jobs)
        assert blocks[0]["type"] == "header"
        sections = [b for b in blocks if b["type"] == "section"]
        assert len(sections) == 2
        assert "Internships (1)" in sections[0]["text"]["text"]
        assert "New Grad (1)" in sections[1]["text"]["text"]
