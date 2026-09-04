"""Portable pipeline for GitHub Actions deployments.

One run = one cron invocation:
  1. Scrape all seed companies into a temp SQLite DB (fresh each run — no
     database needs to persist in git).
  2. Diff against the committed seen-keys file to find genuinely new jobs.
  3. Send the batched Slack digest for new jobs (existing notifier reused).
  4. Record seen keys + export compact jobs.json for the Pages dashboard.

The temp database is discarded at the end; only the seen-keys JSONL and
jobs.json are written back to the repo.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx

from .config import Config
from .database import Database
from .engine import ScrapeEngine
from .export import export_jobs_json
from .models import Company
from .notifier import EXPLICIT_REASONS, Notifier
from .seeding import load_seed_companies
from .statestore import SeenStore

logger = logging.getLogger(__name__)


def job_key(ats_platform: str, slug: str, external_id: str) -> str:
    """Stable identity for a posting across runs."""
    return f"{ats_platform}:{slug}:{external_id}"


async def run_portable_cycle(
    config: Config,
    seen_path: Path,
    export_path: Path,
    *,
    seed_file: Path | None = None,
    test_digest: bool = False,
) -> dict:
    """Execute one portable scrape cycle. Returns a summary dict.

    With ``test_digest=True`` the normal digest is replaced by a forced test
    digest of up to 5 recent jobs (verifies the webhook end-to-end); new jobs
    are still recorded as seen so state stays clean.
    """
    if config.notifications_enabled:
        # The engine's own notifier must stay off; this pipeline delivers the
        # digest itself after diffing against the seen-keys file.
        config = config.with_notifications(False)

    seen = SeenStore(seen_path)
    seen.load()
    logger.info("loaded %d seen keys from %s", len(seen), seen_path)

    summary: dict = {
        "boards_scraped": 0,
        "jobs_found": 0,
        "new_jobs": 0,
        "digest_sent": False,
        "test_digest": test_digest,
        "errors": 0,
        "notification": {
            "attempted": False,
            "sent": False,
            "reason": "no_jobs",
            "explanation": EXPLICIT_REASONS["no_jobs"],
            "matching_jobs": 0,
        },
    }

    with TemporaryDirectory(prefix="jobagent-") as tmp_dir:
        db = Database(Path(tmp_dir) / "run.db")
        await db.connect()
        try:
            for company in load_seed_companies(seed_file):
                await db.upsert_company(company)

            engine = ScrapeEngine(config, db)
            results = await engine.run_cycle()
            summary["boards_scraped"] = len(results)
            summary["errors"] = sum(1 for r in results if r.error)

            # New = present in this scrape but never seen before. (Jobs missing
            # from the API response are marked inactive inside the temp DB and
            # therefore excluded from the export automatically.)
            new_rows: list[dict] = []
            rows, total = await db.query_jobs(active_only=True, per_page=100_000)
            summary["jobs_found"] = total
            now = datetime.now(timezone.utc)
            for row in rows:
                key = job_key(row["ats_platform"], row["company_slug"], row["external_id"] or row["title"])
                if key in seen:
                    continue
                seen.add(key, now)
                new_rows.append(row)
            summary["new_jobs"] = len(new_rows)

            digest_config = config.with_notifications(True)
            notifier = Notifier(digest_config, db)
            if test_digest:
                summary["notification"] = await _run_test_digest(
                    notifier, digest_config, db
                )
                summary["digest_sent"] = summary["notification"]["sent"]
            elif new_rows:
                summary["notification"] = await _notify_new_rows(
                    notifier, digest_config, [row["id"] for row in new_rows]
                )
                summary["digest_sent"] = summary["notification"]["sent"]

            stats = await export_jobs_json(db, export_path)
            summary["export"] = stats
        finally:
            await db.close()

    seen.save()
    logger.info(
        "portable cycle done: %d boards, %d jobs, %d new, digest=%s (%s)",
        summary["boards_scraped"], summary["jobs_found"],
        summary["new_jobs"], summary["digest_sent"],
        summary["notification"]["reason"],
    )
    return summary


async def _notify_new_rows(
    notifier: Notifier, digest_config: Config, new_job_ids: list[int]
) -> dict:
    """Send the regular digest for genuinely new jobs; return a report dict."""
    if not digest_config.notifications_enabled:
        return {"attempted": False, "sent": False, "reason": "notifications_disabled",
                "explanation": EXPLICIT_REASONS["notifications_disabled"], "matching_jobs": 0}
    if not digest_config.slack_webhook_url:
        return {"attempted": False, "sent": False, "reason": "no_webhook",
                "explanation": EXPLICIT_REASONS["no_webhook"], "matching_jobs": 0}
    report = await notifier.notify_new_jobs(new_job_ids)
    logger.info(
        "digest sent=%s (%s) for %d new jobs, %d matched filters",
        report.sent, report.reason, len(new_job_ids), report.matching_jobs,
    )
    return {
        "attempted": True,
        "sent": report.sent,
        "reason": report.reason,
        "explanation": report.explanation,
        "matching_jobs": report.matching_jobs,
    }


async def _run_test_digest(notifier: Notifier, digest_config: Config, db: Database) -> dict:
    """Force-send a digest of up to 5 recent filter-matching jobs."""
    if not digest_config.slack_webhook_url:
        return {"attempted": False, "sent": False, "reason": "no_webhook",
                "explanation": EXPLICIT_REASONS["no_webhook"], "matching_jobs": 0}
    rows, _total = await db.query_jobs(active_only=True, per_page=100_000)
    levels = digest_config.filter_experience_levels
    categories = digest_config.filter_categories
    matching = [
        row for row in rows
        if (not levels or row["experience_level"] in levels)
        and (not categories or row["category"] in categories)
    ]
    if not matching:
        # The point of a test digest is verifying the webhook end-to-end, so
        # fall back to any recent jobs when the filters match nothing.
        logger.info("test digest: no filter-matching jobs; falling back to any recent jobs")
        matching = rows
    candidates = matching[:5]
    report = await notifier.notify_test_digest(
        [row["id"] for row in candidates], apply_filters=False
    )
    logger.info("test digest sent=%s (%s)", report.sent, report.reason)
    return {
        "attempted": True,
        "sent": report.sent,
        "reason": report.reason,
        "explanation": report.explanation,
        "matching_jobs": report.matching_jobs,
    }
