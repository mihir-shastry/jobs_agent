"""Dashboard routes."""

from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pathlib import Path

from ..models import CATEGORIES, EXPERIENCE_LEVELS
from . import get_state

router = APIRouter()

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

PER_PAGE = 50


def _state(request: Request):
    return get_state()


@router.get("/jobs", response_class=HTMLResponse)
async def jobs(
    request: Request,
    q: Optional[str] = Query(None),
    level: list[str] = Query([]),
    category: list[str] = Query([]),
    platform: list[str] = Query([]),
    remote_only: bool = Query(False),
    page: int = Query(1, ge=1),
    sort: str = Query("newest"),
):
    state = _state(request)
    rows, total = await state.database.query_jobs(
        search=q,
        levels=level or None,
        categories=category or None,
        platforms=platform or None,
        remote_only=remote_only,
        page=page,
        per_page=PER_PAGE,
        sort=sort,
    )
    total_pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    return templates.TemplateResponse(
        request,
        "jobs.html",
        {
            "jobs": rows,
            "total": total,
            "page": page,
            "total_pages": total_pages,
            "q": q or "",
            "selected_levels": set(level),
            "selected_categories": set(category),
            "selected_platforms": set(platform),
            "remote_only": remote_only,
            "sort": sort,
            "experience_levels": EXPERIENCE_LEVELS,
            "categories": CATEGORIES,
            "platforms": ("greenhouse", "lever", "ashby"),
        },
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: int):
    state = _state(request)
    job = await state.database.get_job(job_id)
    if job is None:
        return templates.TemplateResponse(request, "404.html", {}, status_code=404)
    others = await state.database.other_jobs_at_company(job["company_id"], job_id)
    status = await state.database.job_status(job_id)
    return templates.TemplateResponse(
        request,
        "job_detail.html",
        {"job": job, "others": others, "status": status},
    )


@router.post("/jobs/{job_id}/status")
async def set_status(request: Request, job_id: int, status: str = Form(...)):
    state = _state(request)
    if status not in {"saved", "applied", "ignored", ""}:
        status = ""
    await state.database.set_job_status(job_id, status)
    return RedirectResponse(url=f"/jobs/{job_id}", status_code=303)


@router.get("/stats", response_class=HTMLResponse)
async def stats(request: Request):
    state = _state(request)
    data = await state.database.stats()
    return templates.TemplateResponse(request, "stats.html", {"stats": data})


@router.get("/settings", response_class=HTMLResponse)
async def settings(request: Request):
    state = _state(request)
    status = await state.database.scraper_status()
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "scraper_status": status,
            "webhook_configured": bool(state.config.slack_webhook_url),
            "notifications_enabled": state.config.notifications_enabled,
            "interval_minutes": state.config.interval_minutes,
            "filters": {
                "levels": state.config.filter_experience_levels,
                "categories": state.config.filter_categories,
            },
            "experience_levels": EXPERIENCE_LEVELS,
            "categories": CATEGORIES,
        },
    )


@router.post("/scrape")
async def trigger_scrape(request: Request):
    """Run one scrape cycle in the background; redirect to settings."""
    import asyncio

    state = _state(request)
    asyncio.create_task(state.engine.run_cycle())
    return RedirectResponse(url="/settings?scrape=triggered", status_code=303)


@router.post("/notify-test")
async def notify_test(request: Request):
    """Send a test digest with up to 5 recent jobs (bypasses filters)."""
    state = _state(request)
    rows, _ = await state.database.query_jobs(active_only=True, per_page=5)
    if not rows:
        return RedirectResponse(url="/settings?test=no_jobs", status_code=303)
    batch_id = uuid.uuid4().hex[:12]
    await state.database.mark_notified([r["id"] for r in rows], batch_id)
    # Build and send digest directly.
    blocks = state.engine.notifier._build_digest_blocks(rows)  # noqa: SLF001
    sent = await state.engine.notifier._send(  # noqa: SLF001
        state.config.slack_webhook_url, blocks
    ) if state.config.slack_webhook_url else False
    return RedirectResponse(
        url=f"/settings?test={'sent' if sent else 'failed'}", status_code=303
    )
