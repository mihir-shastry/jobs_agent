"""ATS adapter base class and shared HTTP helpers."""

from __future__ import annotations

import asyncio
import logging
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any, Optional

import httpx

from ..models import CompanyInfo, RawJob

logger = logging.getLogger(__name__)

USER_AGENT = "jobagent/0.1 (personal job tracker)"


class ATSError(Exception):
    """Raised when an ATS API call fails after retries."""


class RateLimitError(ATSError):
    """Raised on repeated 429 responses."""


class NotFoundError(ATSError):
    """Raised on 404 — board doesn't exist for this slug."""


class ATSAdapter(ABC):
    """One adapter per ATS platform. Subclasses implement endpoint parsing."""

    platform: str = "base"
    board_host: str = ""

    def __init__(self, client: httpx.AsyncClient):
        self.client = client

    # -- helpers ----------------------------------------------------------

    async def get_json(self, url: str, *, retries: int = 3) -> Any:
        """GET a URL expecting JSON, with exponential backoff on 429/5xx."""
        delay = 1.0
        last_exc: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                resp = await self.client.get(
                    url,
                    headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                )
            except httpx.HTTPError as exc:
                last_exc = exc
                logger.warning("HTTP error fetching %s (attempt %d): %s", url, attempt, exc)
                await asyncio.sleep(delay)
                delay *= 2
                continue

            if resp.status_code == 404:
                raise NotFoundError(f"board not found: {url}")
            if resp.status_code == 429:
                logger.warning("Rate limited on %s (attempt %d)", url, attempt)
                last_exc = RateLimitError(f"429 from {url}")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code >= 500:
                last_exc = ATSError(f"{resp.status_code} from {url}")
                await asyncio.sleep(delay)
                delay *= 2
                continue
            if resp.status_code >= 400:
                raise ATSError(f"{resp.status_code} from {url}: {resp.text[:200]}")

            try:
                return resp.json()
            except ValueError as exc:
                raise ATSError(f"non-JSON response from {url}") from exc

        raise last_exc or ATSError(f"failed to fetch {url}")

    @staticmethod
    def _parse_posted_at(value: Any) -> Optional[str]:
        """Normalize an ATS publish-date field to ISO-8601 UTC.

        Accepts ISO strings (Greenhouse/Ashby) and epoch milliseconds
        (Lever's createdAt). Returns None for anything unparsable so a
        missing date never breaks a scrape.
        """
        if not value:
            return None
        if isinstance(value, (int, float)):
            try:
                # Lever reports createdAt in epoch milliseconds.
                dt = datetime.fromtimestamp(value / 1000, tz=timezone.utc)
            except (ValueError, OSError, OverflowError):
                return None
            return dt.isoformat(timespec="seconds")
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.astimezone(timezone.utc).isoformat(timespec="seconds")
        return None

    @abstractmethod
    def board_url(self, company_slug: str) -> str:
        """Human-facing job board URL for a company slug."""

    @abstractmethod
    async def fetch_jobs(
        self, company_slug: str, known_external_ids: Optional[set[str]] = None
    ) -> list[RawJob]:
        """Fetch and normalize all postings for one company board.

        ``known_external_ids`` are IDs already persisted by the caller; adapters
        may use them to skip extra work (e.g. Lever fetches publish dates only
        for postings not yet known).
        """

    async def probe_board(self, company_slug: str) -> Optional[CompanyInfo]:
        """Return CompanyInfo when a board exists for the slug, else None.

        Default implementation probes the jobs endpoint; adapters override
        parsing of the response.
        """
        try:
            await self.fetch_jobs(company_slug)
        except NotFoundError:
            return None
        except ATSError as exc:
            logger.debug("probe failed for %s/%s: %s", self.platform, company_slug, exc)
            return None
        return CompanyInfo(name=company_slug, slug=company_slug, ats_platform=self.platform,
                           ats_board_url=self.board_url(company_slug))

    async def discover_companies(self, candidate_slugs: list[str]) -> list[CompanyInfo]:
        """Probe candidate slugs; return those that resolve to real boards."""
        found: list[CompanyInfo] = []
        for slug in candidate_slugs:
            info = await self.probe_board(slug)
            if info is not None:
                found.append(info)
                logger.info("discovered %s board: %s", self.platform, slug)
        return found
