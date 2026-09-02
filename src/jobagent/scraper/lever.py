"""Lever job postings adapter.

Public endpoint: GET https://api.lever.co/v0/postings/{slug}?mode=json
"""

from __future__ import annotations

import logging
from typing import Any

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

    async def fetch_jobs(self, company_slug: str) -> list[RawJob]:
        data = await self.get_json(self._api_url(company_slug))
        if not isinstance(data, list):
            raise ValueError("unexpected Lever response shape")
        jobs: list[RawJob] = []
        for post in data:
            job = self._parse(post, company_slug)
            if job is not None:
                jobs.append(job)
        return jobs

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
        )
