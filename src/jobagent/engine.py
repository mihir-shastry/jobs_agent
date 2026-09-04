"""Scraper orchestrator: runs one scrape cycle across all enabled platforms."""

from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx

from .config import Config
from .classifier import classify
from .database import Database
from .models import RawJob, ScrapeResult
from .notifier import Notifier
from .scraper.ashby import AshbyAdapter
from .scraper.base import ATSAdapter, ATSError, NotFoundError
from .scraper.greenhouse import GreenhouseAdapter
from .scraper.lever import LeverAdapter

logger = logging.getLogger(__name__)

_ADAPTERS: dict[str, type[ATSAdapter]] = {
    GreenhouseAdapter.platform: GreenhouseAdapter,
    LeverAdapter.platform: LeverAdapter,
    AshbyAdapter.platform: AshbyAdapter,
}


def _remote_type(location: str | None) -> str:
    if not location:
        return "unknown"
    loc = location.lower()
    if "remote" in loc and "hybrid" not in loc:
        return "remote"
    if "hybrid" in loc:
        return "hybrid"
    return "onsite"


class ScrapeEngine:
    """Coordinates adapters, database writes, and notifications."""

    def __init__(self, config: Config, database: Database):
        self.config = config
        self.database = database
        self.notifier = Notifier(config, database)

    async def run_cycle(self) -> list[ScrapeResult]:
        """Scrape all active companies on enabled platforms; notify about new jobs."""
        new_job_ids: list[int] = []
        results: list[ScrapeResult] = []

        async with httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            limits=httpx.Limits(max_connections=self.config.max_concurrent_requests * 2),
        ) as client:
            for platform in self.config.enabled_platforms:
                adapter_cls = _ADAPTERS.get(platform)
                if adapter_cls is None:
                    logger.warning("no adapter for platform %s; skipping", platform)
                    continue
                adapter = adapter_cls(client)
                platform_results, platform_new_ids = await self._scrape_platform(
                    adapter, client
                )
                results.extend(platform_results)
                new_job_ids.extend(platform_new_ids)

        if new_job_ids:
            await self._notify(new_job_ids)

        return results

    async def _scrape_platform(
        self, adapter: ATSAdapter, client: httpx.AsyncClient
    ) -> tuple[list[ScrapeResult], list[int]]:
        companies = await self.database.get_companies(platform=adapter.platform)
        semaphore = asyncio.Semaphore(self.config.max_concurrent_requests)
        delay = self.config.request_delay_ms / 1000.0

        async def scrape_one(company: dict) -> ScrapeResult:
            async with semaphore:
                result = await self._scrape_company(adapter, company)
                await asyncio.sleep(delay)
                return result

        results = await asyncio.gather(*(scrape_one(c) for c in companies))
        new_ids: list[int] = []
        for result in results:
            new_ids.extend(result.job_ids)
        return results, new_ids

    async def _scrape_company(self, adapter: ATSAdapter, company: dict) -> ScrapeResult:
        slug = company["slug"]
        result = ScrapeResult(company_slug=slug, ats_platform=adapter.platform)
        # Adapters may skip extra per-job work (e.g. Lever's posted-date
        # fetch) for postings already in the database.
        known_ids = await self.database.get_external_ids(company["id"])
        try:
            raw_jobs = await adapter.fetch_jobs(slug, known_ids)
        except NotFoundError:
            logger.info("board gone: %s/%s — deactivating", adapter.platform, slug)
            await self.database.set_company_active(company["id"], False)
            result.error = "board not found"
            return result
        except ATSError as exc:
            logger.warning("scrape failed %s/%s: %s", adapter.platform, slug, exc)
            result.error = str(exc)
            return result
        except Exception as exc:  # noqa: BLE001 — never crash the cycle
            logger.exception("unexpected error scraping %s/%s", adapter.platform, slug)
            result.error = str(exc)
            return result

        keep_ids: set[str] = set()
        for raw in raw_jobs:
            self._classify(raw)
            keep_ids.add(raw.external_id)
            is_new = await self.database.upsert_job(company["id"], raw)
            result.jobs_found += 1
            if is_new:
                result.jobs_new += 1
                job_id = await self._lookup_job_id(company["id"], raw.external_id)
                if job_id is not None:
                    result.job_ids.append(job_id)

        deactivated = await self.database.deactivate_missing_jobs(
            company["id"], keep_ids
        )
        if deactivated:
            logger.info("deactivated %d jobs for %s/%s", deactivated, adapter.platform, slug)

        await self.database.mark_company_scraped(
            company["id"], datetime.now(timezone.utc).isoformat()
        )
        return result

    def _classify(self, raw: RawJob) -> None:
        level, category = classify(raw.title)
        raw.experience_level = level
        raw.category = category
        raw.remote_type = _remote_type(raw.location)

    async def _lookup_job_id(self, company_id: int, external_id: str) -> Optional[int]:
        cursor = await self.database.db.execute(
            "SELECT id FROM jobs WHERE company_id = ? AND external_id = ?",
            (company_id, external_id),
        )
        row = await cursor.fetchone()
        return int(row["id"]) if row else None

    async def _notify(self, new_job_ids: list[int]) -> None:
        try:
            await self.notifier.notify_new_jobs(new_job_ids)
        except Exception:  # noqa: BLE001 — notifications are non-critical
            logger.exception("notification step failed (jobs are still saved)")
