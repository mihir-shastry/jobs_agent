"""Ashby job board adapter.

Public endpoint: GET https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true
"""

from __future__ import annotations

import logging
import re
from typing import Any

from ..models import CompanyInfo, RawJob
from ..sanitize import sanitize_description
from .base import ATSAdapter

logger = logging.getLogger(__name__)

_RANGE_RE = re.compile(r"(\$?)([\d,]+)\s*[-–—]\s*(\$?)([\d,]+)")


class AshbyAdapter(ATSAdapter):
    platform = "ashby"

    def board_url(self, company_slug: str) -> str:
        return f"https://jobs.ashbyhq.com/{company_slug}"

    def _api_url(self, company_slug: str) -> str:
        return (
            f"https://api.ashbyhq.com/posting-api/job-board/{company_slug}"
            "?includeCompensation=true"
        )

    async def fetch_jobs(self, company_slug: str) -> list[RawJob]:
        data = await self.get_json(self._api_url(company_slug))
        posts = (data or {}).get("jobs") or []
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
            apply_url = str(post.get("jobUrl") or "").strip()
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug("ashby/%s: skipping malformed job %s", company_slug, exc)
            return None

        if not title or not apply_url:
            return None

        location = post.get("location")
        description = sanitize_description(post.get("jobDescription"))

        salary_min = salary_max = None
        currency = None
        comp = post.get("compensation") or {}
        if isinstance(comp, dict):
            comp_summary = comp.get("compensationIntervalSummary") or ""
            m = _RANGE_RE.search(str(comp_summary))
            if m:
                lo = m.group(2).replace(",", "")
                hi = m.group(4).replace(",", "")
                try:
                    salary_min, salary_max = int(lo), int(hi)
                except ValueError:
                    pass
                if "$" in (m.group(1) + m.group(3)) or "USD" in str(comp_summary):
                    currency = "USD"

        departments = [
            d for d in (post.get("department"), post.get("team")) if d
        ]

        return RawJob(
            external_id=external_id,
            title=title,
            apply_url=apply_url,
            location=location,
            description=description,
            salary_min=salary_min,
            salary_max=salary_max,
            salary_currency=currency,
            departments=departments,
        )
