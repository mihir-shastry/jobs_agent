"""CLI entry points for jobagent."""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import load_config
from .database import Database
from .models import Company

logger = logging.getLogger("jobagent")

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SEED_FILE = DATA_DIR / "seed_companies.json"
CANDIDATE_FILE = DATA_DIR / "candidate_slugs.txt"


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )


async def _seed_companies() -> int:
    config = load_config()
    db = Database(config.database_path)
    await db.connect()
    try:
        if not SEED_FILE.exists():
            logger.error("seed file not found: %s", SEED_FILE)
            return 1
        companies = json.loads(SEED_FILE.read_text(encoding="utf-8"))
        count = 0
        for entry in companies:
            company = Company(
                name=entry["name"],
                slug=entry["slug"],
                ats_platform=entry["ats_platform"],
                ats_board_url=entry.get("ats_board_url"),
                career_url=entry.get("career_url"),
            )
            await db.upsert_company(company)
            count += 1
        logger.info("seeded %d companies into %s", count, config.database_path)
        return 0
    finally:
        await db.close()


async def _run_scrape() -> int:
    from .engine import ScrapeEngine

    config = load_config()
    db = Database(config.database_path)
    await db.connect()
    try:
        engine = ScrapeEngine(config, db)
        results = await engine.run_cycle()
        total_new = sum(r.jobs_new for r in results)
        errors = [r for r in results if r.error]
        logger.info(
            "scrape complete: %d boards, %d new jobs, %d errors",
            len(results), total_new, len(errors),
        )
        for r in errors[:20]:
            logger.warning("  error: %s/%s: %s", r.ats_platform, r.company_slug, r.error)
        return 0
    finally:
        await db.close()


async def _discover() -> int:
    """Probe candidate slugs and persist boards that respond."""
    import httpx

    from .scraper.ashby import AshbyAdapter
    from .scraper.base import NotFoundError
    from .scraper.greenhouse import GreenhouseAdapter
    from .scraper.lever import LeverAdapter

    config = load_config()
    if not CANDIDATE_FILE.exists():
        logger.error("candidate slugs file not found: %s", CANDIDATE_FILE)
        return 1

    candidates = [
        line.strip()
        for line in CANDIDATE_FILE.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    logger.info("probing %d candidate slugs", len(candidates))

    db = Database(config.database_path)
    await db.connect()
    added = 0
    try:
        async with httpx.AsyncClient(timeout=20.0, follow_redirects=True) as client:
            for adapter_cls in (GreenhouseAdapter, LeverAdapter, AshbyAdapter):
                adapter = adapter_cls(client)
                for slug in candidates:
                    try:
                        jobs = await adapter.fetch_jobs(slug)
                    except NotFoundError:
                        continue
                    except Exception as exc:  # noqa: BLE001
                        logger.debug("%s/%s probe failed: %s", adapter.platform, slug, exc)
                        continue
                    if jobs:
                        name = slug.replace("-", " ").replace("_", " ").title()
                        await db.upsert_company(
                            Company(name=name, slug=slug, ats_platform=adapter.platform,
                                    ats_board_url=adapter.board_url(slug))
                        )
                        added += 1
                        logger.info("✓ %s/%s (%d jobs)", adapter.platform, slug, len(jobs))
        logger.info("discovery done: %d new boards added", added)
        return 0
    finally:
        await db.close()


async def _print_stats() -> int:
    config = load_config()
    db = Database(config.database_path)
    await db.connect()
    try:
        stats = await db.stats()
        print(f"Active jobs:  {stats['active_jobs']}")
        print(f"Companies:    {stats['companies']}")
        print("\nBy platform:")
        for row in stats["by_platform"]:
            print(f"  {row['label']:<15} {row['n']}")
        print("\nBy category:")
        for row in stats["by_category"]:
            print(f"  {row['label']:<15} {row['n']}")
        print("\nBy level:")
        for row in stats["by_level"]:
            print(f"  {row['label']:<15} {row['n']}")
        return 0
    finally:
        await db.close()


def _serve() -> int:
    import uvicorn

    config = load_config()
    uvicorn.run(
        "jobagent.web:create_app",
        host=config.dashboard_host,
        port=config.dashboard_port,
        log_level="info",
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="jobagent", description="ATS job scraper, dashboard, and Slack notifier"
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("serve", help="start dashboard + hourly scraper")
    sub.add_parser("scrape", help="run one scrape cycle now")
    sub.add_parser("seed-companies", help="load data/seed_companies.json into the DB")
    sub.add_parser("discover", help="probe candidate slugs for new boards")
    sub.add_parser("stats", help="print job/company counts")

    args = parser.parse_args(argv)
    _setup_logging(args.verbose)

    if args.command == "serve":
        return _serve()
    if args.command == "scrape":
        return asyncio.run(_run_scrape())
    if args.command == "seed-companies":
        return asyncio.run(_seed_companies())
    if args.command == "discover":
        return asyncio.run(_discover())
    if args.command == "stats":
        return asyncio.run(_print_stats())
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
