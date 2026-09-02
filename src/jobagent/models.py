"""Shared Pydantic models for jobs, companies, and scraper results."""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# Valid values for classification columns.
EXPERIENCE_LEVELS = ("internship", "new_grad", "entry", "mid", "senior")
CATEGORIES = ("swe", "data_science", "pm", "design", "quant", "other")
REMOTE_TYPES = ("remote", "hybrid", "onsite", "unknown")
ATS_PLATFORMS = ("greenhouse", "lever", "ashby")


class Company(BaseModel):
    """A company board on a specific ATS platform."""

    name: str
    slug: str
    ats_platform: str
    ats_board_url: Optional[str] = None
    career_url: Optional[str] = None


class CompanyInfo(Company):
    """Company data returned by discovery probes."""


class RawJob(BaseModel):
    """Normalized job posting produced by an ATS adapter."""

    external_id: str
    title: str
    apply_url: str
    location: Optional[str] = None
    remote_type: str = "unknown"
    description: Optional[str] = None
    salary_min: Optional[int] = None
    salary_max: Optional[int] = None
    salary_currency: Optional[str] = None
    departments: list[str] = Field(default_factory=list)

    # Filled in by the classifier after normalization.
    experience_level: str = "entry"
    category: str = "other"


class ScrapeResult(BaseModel):
    """Outcome of fetching jobs for a single company board."""

    company_slug: str
    ats_platform: str
    jobs_found: int = 0
    jobs_new: int = 0
    job_ids: list[int] = Field(default_factory=list)
    error: Optional[str] = None
