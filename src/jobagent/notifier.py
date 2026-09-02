"""Slack digest notifier.

Builds a batched digest (grouped by experience level) of new jobs that match
the configured filters and posts it via incoming webhook. Jobs are recorded
in notified_jobs regardless of delivery success so we never re-notify.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any, Optional

import httpx

from .config import Config
from .database import Database
from .models import EXPERIENCE_LEVELS

logger = logging.getLogger(__name__)

LEVEL_LABELS: dict[str, str] = {
    "internship": "Internships",
    "new_grad": "New Grad",
    "entry": "Entry Level",
    "mid": "Mid Level",
    "senior": "Senior",
}

# Keep digests within Slack's ~50 block / message-size limits.
_MAX_JOBS_PER_LEVEL = 20
_MAX_LEVELS_IN_DIGEST = 5


class Notifier:
    """Sends batched Slack digests for new jobs."""

    def __init__(self, config: Config, database: Database):
        self.config = config
        self.database = database

    # -- public API --------------------------------------------------------

    async def notify_new_jobs(self, job_ids: list[int]) -> bool:
        """Build and send a digest for the given new job ids.

        Returns True when a digest was sent. Always records jobs as notified.
        """
        if not job_ids or not self.config.notifications_enabled:
            await self._record_notified(job_ids, batch_id=None)
            return False

        webhook = self.config.slack_webhook_url
        if not webhook:
            logger.info("no Slack webhook configured; skipping notification")
            await self._record_notified(job_ids, batch_id=None)
            return False

        # Unseen-in-digest jobs may still be pre-existing (first scrape seeds
        # many). Only include jobs matching user filters.
        matching = await self._filter_matching(job_ids)
        if not matching:
            await self._record_notified(job_ids, batch_id=None)
            return False

        blocks = self._build_digest_blocks(matching)
        batch_id = uuid.uuid4().hex[:12]
        sent = await self._send(webhook, blocks)
        await self._record_notified(job_ids, batch_id=batch_id if sent else None)
        return sent

    # -- internals ----------------------------------------------------------

    async def _filter_matching(self, job_ids: list[int]) -> list[dict[str, Any]]:
        levels = self.config.filter_experience_levels
        categories = self.config.filter_categories
        matching: list[dict[str, Any]] = []
        for job_id in job_ids:
            job = await self.database.get_job(job_id)
            if job is None:
                continue
            if levels and job["experience_level"] not in levels:
                continue
            if categories and job["category"] not in categories:
                continue
            matching.append(job)
        return matching

    def _build_digest_blocks(self, jobs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        blocks: list[dict[str, Any]] = [
            {
                "type": "header",
                "text": {"type": "plain_text", "text": f"🆕 {len(jobs)} New Job Postings", "emoji": True},
            }
        ]

        by_level: dict[str, list[dict[str, Any]]] = {}
        for job in jobs:
            by_level.setdefault(job["experience_level"], []).append(job)

        shown_levels = 0
        for level in EXPERIENCE_LEVELS:
            level_jobs = by_level.get(level)
            if not level_jobs or shown_levels >= _MAX_LEVELS_IN_DIGEST:
                continue
            shown_levels += 1
            label = LEVEL_LABELS.get(level, level)
            lines = []
            for job in level_jobs[:_MAX_JOBS_PER_LEVEL]:
                loc = f" ({job['location']})" if job.get("location") else ""
                lines.append(
                    f"• <{job['apply_url']}|{self._escape(job['title'])}> — "
                    f"{self._escape(job['company_name'])}{self._escape(loc)}"
                )
            omitted = len(level_jobs) - len(level_jobs[:_MAX_JOBS_PER_LEVEL])
            if omitted > 0:
                lines.append(f"_…and {omitted} more (see dashboard)_")
            blocks.append(
                {
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": f"*{label} ({len(level_jobs)})*\n" + "\n".join(lines)},
                }
            )

        blocks.append(
            {
                "type": "context",
                "elements": [{"type": "mrkdwn", "text": "Filtered by your preferences • browse all: open the dashboard"}],
            }
        )
        return blocks

    async def _send(self, webhook: str, blocks: list[dict[str, Any]]) -> bool:
        payload = {"text": "New job postings digest", "blocks": blocks}
        try:
            async with httpx.AsyncClient(timeout=15.0) as client:
                resp = await client.post(webhook, json=payload)
            if resp.status_code >= 400:
                logger.warning("Slack webhook returned %d: %s", resp.status_code, resp.text[:200])
                return False
            return True
        except httpx.HTTPError as exc:
            logger.warning("Slack webhook failed: %s", exc)
            return False

    async def _record_notified(self, job_ids: list[int], batch_id: Optional[str]) -> None:
        if job_ids:
            await self.database.mark_notified(job_ids, batch_id or "unbatched")

    @staticmethod
    def _escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
