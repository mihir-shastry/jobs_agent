"""Slack digest notifier.

Builds a batched digest (grouped by experience level) of new jobs that match
the configured filters and posts it via incoming webhook. Jobs are recorded
in notified_jobs regardless of delivery success so we never re-notify.

Every attempt produces a ``NotificationReport`` with a machine-readable
``reason`` explaining exactly why a digest did or did not fire — surfaced in
CI run summaries so silent skips are never a mystery again.
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
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

_RETRY_DELAY_SECONDS = 2.0

# Explanations surfaced in logs and CI step summaries so a missing digest is
# always explainable from the run page.
EXPLICIT_REASONS: dict[str, str] = {
    "ok": "digest sent to Slack",
    "no_jobs": "no new job IDs were provided",
    "notifications_disabled": "notifications are disabled in config",
    "no_webhook": "no Slack webhook URL configured",
    "no_matching_jobs": "no new jobs matched the configured notification filters",
    "slack_rejected": "Slack rejected the digest (bad webhook URL/secret?)",
    "slack_unreachable": "could not deliver to Slack after retries (network/5xx)",
    "forced_test": "forced test digest (bypasses seen-state)",
}


@dataclass
class NotificationReport:
    """Outcome of one notification attempt."""

    sent: bool = False
    reason: str = "ok"
    matching_jobs: int = 0
    batch_id: Optional[str] = field(default=None)

    @property
    def explanation(self) -> str:
        return EXPLICIT_REASONS.get(self.reason, self.reason)


def _format_posted_date(posted_at: Any) -> str:
    """Short "· posted Feb 14" fragment for digest bullets ('' when unknown)."""
    if not posted_at:
        return ""
    try:
        dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
    except ValueError:
        return ""
    return f"· posted {dt.strftime('%b')} {dt.day}"


class Notifier:
    """Sends batched Slack digests for new jobs."""

    def __init__(self, config: Config, database: Database):
        self.config = config
        self.database = database

    # -- public API --------------------------------------------------------

    async def notify_new_jobs(self, job_ids: list[int]) -> NotificationReport:
        """Build and send a digest for the given new job ids.

        Returns a report describing what happened. Always records the jobs as
        notified (even on delivery failure) so the hourly cycle never re-asks.
        """
        report = await self._attempt_digest(job_ids)
        await self._record_notified(job_ids, batch_id=report.batch_id)
        return report

    async def notify_test_digest(
        self, job_ids: list[int], *, apply_filters: bool = True
    ) -> NotificationReport:
        """Force-send a digest for the given jobs, bypassing seen-state.

        Used by the ``test_digest`` run mode and the dashboard's test button
        to verify the webhook end-to-end. Failed sends are deliberately NOT
        recorded as notified so the test can be retried after a fix.
        """
        if not job_ids:
            return NotificationReport(sent=False, reason="no_jobs")
        webhook = self.config.slack_webhook_url
        if not webhook:
            return NotificationReport(sent=False, reason="no_webhook")
        if apply_filters:
            matching = await self._filter_matching(job_ids)
        else:
            matching = await self._load_jobs(job_ids)
        if not matching:
            return NotificationReport(sent=False, reason="no_matching_jobs")
        blocks = self._build_digest_blocks(matching)
        sent, fail_reason = await self._send(webhook, blocks)
        return NotificationReport(
            sent=sent,
            reason="forced_test" if sent else fail_reason,
            matching_jobs=len(matching),
        )

    # -- internals ----------------------------------------------------------

    async def _attempt_digest(self, job_ids: list[int]) -> NotificationReport:
        if not job_ids:
            return NotificationReport(sent=False, reason="no_jobs")
        if not self.config.notifications_enabled:
            return NotificationReport(sent=False, reason="notifications_disabled")
        webhook = self.config.slack_webhook_url
        if not webhook:
            logger.info("no Slack webhook configured; skipping notification")
            return NotificationReport(sent=False, reason="no_webhook")

        # Unseen-in-digest jobs may still be pre-existing (first scrape seeds
        # many). Only include jobs matching user filters.
        matching = await self._filter_matching(job_ids)
        if not matching:
            return NotificationReport(sent=False, reason="no_matching_jobs")

        blocks = self._build_digest_blocks(matching)
        batch_id = uuid.uuid4().hex[:12]
        sent, fail_reason = await self._send(webhook, blocks)
        return NotificationReport(
            sent=sent,
            reason="ok" if sent else fail_reason,
            matching_jobs=len(matching),
            batch_id=batch_id if sent else None,
        )

    async def _load_jobs(self, job_ids: list[int]) -> list[dict[str, Any]]:
        jobs: list[dict[str, Any]] = []
        for job_id in job_ids:
            job = await self.database.get_job(job_id)
            if job is not None:
                jobs.append(job)
        return jobs

    async def _filter_matching(self, job_ids: list[int]) -> list[dict[str, Any]]:
        levels = self.config.filter_experience_levels
        categories = self.config.filter_categories
        matching: list[dict[str, Any]] = []
        for job in await self._load_jobs(job_ids):
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
                posted = _format_posted_date(job.get("posted_at"))
                suffix = f" {posted}" if posted else ""
                lines.append(
                    f"• <{job['apply_url']}|{self._escape(job['title'])}> — "
                    f"{self._escape(job['company_name'])}{self._escape(loc)}{suffix}"
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

    async def _send(self, webhook: str, blocks: list[dict[str, Any]], *, retries: int = 2) -> tuple[bool, str]:
        """POST the digest. Returns (sent, failure_reason).

        4xx responses are deterministic (bad URL/secret) and are not retried;
        network errors and 5xx get one retry with backoff.
        """
        payload = {"text": "New job postings digest", "blocks": blocks}
        reason = "slack_unreachable"
        for attempt in range(1, retries + 1):
            try:
                async with httpx.AsyncClient(timeout=15.0) as client:
                    resp = await client.post(webhook, json=payload)
            except httpx.HTTPError as exc:
                logger.warning("Slack webhook failed (attempt %d/%d): %s", attempt, retries, exc)
            else:
                if resp.status_code < 400:
                    return True, ""
                logger.warning("Slack webhook returned %d: %s", resp.status_code, resp.text[:200])
                if resp.status_code < 500:
                    return False, "slack_rejected"
                reason = "slack_unreachable"
            if attempt < retries:
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
        return False, reason

    async def _record_notified(self, job_ids: list[int], batch_id: Optional[str]) -> None:
        if job_ids:
            await self.database.mark_notified(job_ids, batch_id or "unbatched")

    @staticmethod
    def _escape(text: str) -> str:
        return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
