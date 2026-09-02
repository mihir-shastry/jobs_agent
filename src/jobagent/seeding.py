"""Shared helpers for locating and loading the seed company list."""

from __future__ import annotations

import json
import logging
from pathlib import Path

from .models import Company

logger = logging.getLogger(__name__)

DATA_DIR = Path(__file__).resolve().parent.parent.parent / "data"
SEED_FILE = DATA_DIR / "seed_companies.json"
CANDIDATE_FILE = DATA_DIR / "candidate_slugs.txt"


def load_seed_companies(path: Path | None = None) -> list[Company]:
    """Load companies from the seed JSON file (empty list when missing)."""
    seed_path = path or SEED_FILE
    if not seed_path.exists():
        logger.warning("seed file not found: %s", seed_path)
        return []
    entries = json.loads(seed_path.read_text(encoding="utf-8"))
    companies: list[Company] = []
    for entry in entries:
        companies.append(
            Company(
                name=entry["name"],
                slug=entry["slug"],
                ats_platform=entry["ats_platform"],
                ats_board_url=entry.get("ats_board_url"),
                career_url=entry.get("career_url"),
            )
        )
    logger.info("loaded %d seed companies from %s", len(companies), seed_path)
    return companies


def load_candidate_slugs(path: Path | None = None) -> list[str]:
    """Load candidate slugs for discovery, skipping blanks and comments."""
    candidate_path = path or CANDIDATE_FILE
    if not candidate_path.exists():
        return []
    slugs: list[str] = []
    for line in candidate_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#"):
            slugs.append(line)
    return slugs
