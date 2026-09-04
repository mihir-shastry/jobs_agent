"""Greenhouse job board adapter.

Public endpoint: GET https://api.greenhouse.io/v1/boards/{slug}/jobs?content=true
"""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from ..models import CompanyInfo, RawJob
from ..sanitize import sanitize_description
from .base import ATSAdapter

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"<[^>]+>")


class GreenhouseAdapter(ATSAdapter):
    platform = "greenhouse"

    def board_url(self, company_slug: str) -> str:
        return f"https://boards.greenhouse.io/{company_slug}"

    def _api_url(self, company_slug: str) -> str:
        return f"https://api.greenhouse.io/v1/boards/{company_slug}/jobs?content=true"

    async def fetch_jobs(
        self, company_slug: str, known_external_ids: Optional[set[str]] = None
    ) -> list[RawJob]:
        data = await self.get_json(self._api_url(company_slug))
        posts = data.get("jobs") or []
        jobs: list[RawJob] = []
        for post in posts:
            job = self._parse(post, company_slug)
            if job is not None:
                jobs.append(job)
        return jobs

    def _parse(self, post: dict[str, Any], company_slug: str) -> RawJob | None:
        try:
            external_id = str(post["id"])
            title = str(post["title"]).strip()
            apply_url = str(post.get("absolute_url") or "").strip()
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("greenhouse/%s: skipping malformed job %s", company_slug, exc)
            return None

        if not title or not apply_url:
            return None

        location = None
        loc = post.get("location")
        if isinstance(loc, dict):
            location = loc.get("name")
        elif isinstance(loc, str):
            location = loc

        # `content` is HTML-encoded when ?content=true.
        description = sanitize_description(post.get("content"))

        departments = [
            d.get("name") for d in (post.get("departments") or []) if isinstance(d, dict) and d.get("name")
        ]

        return RawJob(
            external_id=external_id,
            title=title,
            apply_url=apply_url,
            location=location,
            description=description,
            departments=departments,
            posted_at=self._parse_posted_at(post.get("first_published")),
        )
