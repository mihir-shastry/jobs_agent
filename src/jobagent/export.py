"""Export the active job set to a compact JSON file for the static dashboard.

Descriptions are dropped and titles deduplicated per company to keep the
payload small (GitHub Pages soft-limits sites to well under 100 MB, but a
lean file also loads fast on phones).
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .database import Database

# Fields kept per job; everything else (description, salary, …) is omitted.
_KEEP_FIELDS = (
    "id", "title", "company_name", "ats_platform", "location",
    "experience_level", "category", "apply_url", "posted_at", "first_seen_at",
)


async def export_jobs_json(db: Database, out_path: Path) -> dict[str, Any]:
    """Write active jobs to out_path; returns summary stats."""
    rows, total = await db.query_jobs(active_only=True, per_page=100_000, sort="newest")
    jobs = [{field: row[field] for field in _KEEP_FIELDS} for row in rows]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "total": total,
        "jobs": jobs,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(
        json.dumps(payload, separators=(",", ":"), ensure_ascii=False),
        encoding="utf-8",
    )
    size_kb = out_path.stat().st_size / 1024
    return {"exported": len(jobs), "total": total, "size_kb": round(size_kb, 1)}
