"""Lever job postings adapter.

Public endpoint: GET https://api.lever.co/v0/postings/{slug}?mode=json

The list response carries no posting date, so the adapter additionally calls
GET /v0/postings/{slug}/{id}?mode=json (which includes createdAt) — but only
for jobs not in ``known_external_ids``. The engine passes the IDs already in
the database, so each job's date is fetched once, when it first appears.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

from ..models import CompanyInfo, RawJob
from ..sanitize import sanitize_description
from .base import ATSAdapter

logger = logging.getLogger(__name__)


class LeverAdapter(ATSAdapter):
    platform = "lever"

    def board_url(self, company_slug: str) -> str:
        return f"https://jobs.lever.co/{company_slug}"

    def _api_url(self, company_slug: str) -> str:
        return f"https://api.lever.co/v0/postings/{company_slug}?mode=json"

    def _posting_api_url(self, company_slug: str, external_id: str) -> str:
        return f"https://api.lever.co/v0/postings/{company_slug}/{external_id}?mode=json"

    async def fetch_jobs(
        self, company_slug: str, known_external_ids: Optional[set[str]] = None
    ) -> list[RawJob]:
        data = await self.get_json(self._api_url(company_slug))
        if not isinstance(data, list):
            raise ValueError("unexpected Lever response shape")

        known = known_external_ids or set()
        unknown_ids = {
            str(post["id"])
            for post in data
            if isinstance(post, dict) and "id" in post and str(post["id"]) not in known
        }

        jobs: list[RawJob] = []
        for post in data:
            job = self._parse(post, company_slug)
            if job is not None:
                jobs.append(job)

        if unknown_ids:
            await self._fill_posted_dates(company_slug, jobs, unknown_ids)
        return jobs

    async def _fill_posted_dates(
        self, company_slug: str, jobs: list[RawJob], external_ids: set[str]
    ) -> None:
        """Fetch createdAt for the given postings from the per-posting endpoint.

        Best-effort: failures leave posted_at unset and never break a scrape.
        Bounded concurrency to stay polite to the API.
        """
        by_id = {job.external_id: job for job in jobs}
        semaphore = asyncio.Semaphore(3)

        async def fetch_one(external_id: str) -> Optional[str]:
            async with semaphore:
                try:
                    data = await self.get_json(
                        self._posting_api_url(company_slug, external_id)
                    )
                except Exception as exc:  # noqa: BLE001 — dates are best-effort
                    logger.debug(
                        "lever/%s: posted-date fetch failed for %s: %s",
                        company_slug, external_id, exc,
                    )
                    return None
            return self._parse_posted_at(data.get("createdAt")) if isinstance(data, dict) else None

        dates = await asyncio.gather(*(fetch_one(eid) for eid in external_ids))
        for external_id, posted_at in zip(external_ids, dates):
            job = by_id.get(external_id)
            if job is not None and job.posted_at is None and posted_at:
                job.posted_at = posted_at

    def _parse(self, post: dict[str, Any], company_slug: str) -> RawJob | None:
        try:
            external_id = str(post["id"])
            title = str(post["text"]).strip()
            apply_url = str(post.get("hostedUrl") or "").strip()
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("lever/%s: skipping malformed job %s", company_slug, exc)
            return None

        if not title or not apply_url:
            return None

        categories = post.get("categories") or {}
        location = categories.get("location")
        departments = [
            d for d in (categories.get("department"), categories.get("team")) if d
        ]

        description = sanitize_description(post.get("descriptionPlain") or post.get("description"))

        return RawJob(
            external_id=external_id,
            title=title,
            apply_url=apply_url,
            location=location,
            description=description,
            departments=departments,
            posted_at=self._parse_posted_at(post.get("createdAt")),
        )
