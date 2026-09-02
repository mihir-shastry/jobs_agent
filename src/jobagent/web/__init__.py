"""FastAPI application factory."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from functools import lru_cache
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from fastapi import FastAPI
from fastapi.responses import RedirectResponse

from ..config import Config, load_config
from ..database import Database
from ..engine import ScrapeEngine

logger = logging.getLogger(__name__)


class AppState:
    """Holds shared singletons for the app lifetime."""

    def __init__(self, config: Config):
        self.config = config
        self.database = Database(config.database_path)
        self.engine = ScrapeEngine(config, self.database)
        self.scheduler: Optional[AsyncIOScheduler] = None


@lru_cache(maxsize=1)
def get_state() -> AppState:
    return AppState(load_config())


def create_app(config: Optional[Config] = None) -> FastAPI:
    """Build the FastAPI app. `config` override is for tests."""
    from .routes import router

    state = get_state() if config is None else AppState(config)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await state.database.connect()
        state.scheduler = AsyncIOScheduler(timezone="UTC")
        state.scheduler.add_job(
            run_scrape_cycle,
            "interval",
            minutes=state.config.interval_minutes,
            id="scrape_cycle",
            max_instances=1,
            coalesce=True,
        )
        state.scheduler.start()
        logger.info(
            "scheduler started: scraping every %d minutes (platforms: %s)",
            state.config.interval_minutes,
            ", ".join(state.config.enabled_platforms),
        )
        try:
            yield
        finally:
            if state.scheduler is not None:
                state.scheduler.shutdown(wait=False)
            await state.database.close()

    async def run_scrape_cycle() -> None:
        logger.info("scheduled scrape cycle starting")
        results = await state.engine.run_cycle()
        total_new = sum(r.jobs_new for r in results)
        errors = sum(1 for r in results if r.error)
        logger.info(
            "scrape cycle done: %d boards, %d new jobs, %d errors",
            len(results), total_new, errors,
        )

    app = FastAPI(title="Job Agent", lifespan=lifespan)
    app.state.jobagent = state
    app.include_router(router)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/")
    async def index_redirect():
        return RedirectResponse(url="/jobs")

    return app
