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
) -> dict:
    """Execute one portable scrape cycle. Returns a summary dict."""
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
        "errors": 0,
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

            if new_rows:
                digest_config = config.with_notifications(True)
                if digest_config.notifications_enabled and digest_config.slack_webhook_url:
                    from .notifier import Notifier

                    notifier = Notifier(digest_config, db)
                    sent = await notifier.notify_new_jobs([row["id"] for row in new_rows])
                    summary["digest_sent"] = sent
                    logger.info("digest sent=%s for %d new jobs", sent, len(new_rows))
                else:
                    logger.info("%d new jobs but no webhook configured", len(new_rows))

            stats = await export_jobs_json(db, export_path)
            summary["export"] = stats
        finally:
            await db.close()

    seen.save()
    logger.info(
        "portable cycle done: %d boards, %d jobs, %d new, digest=%s",
        summary["boards_scraped"], summary["jobs_found"],
        summary["new_jobs"], summary["digest_sent"],
    )
    return summary
