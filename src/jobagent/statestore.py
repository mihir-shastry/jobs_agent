"""File-based seen-state store for portable (GitHub Actions) deployments.

Replaces the SQLite ``notified_jobs`` table with a single JSONL file that is
committed back to the repo between runs: one JSON object per line, e.g.
``{"k": "greenhouse:acme:12345", "t": "2026-09-02T16:00:00+00:00"}``.

Old entries are pruned on save so the file stays small even after months of
hourly runs (a job that is still posted will simply be re-added on the next
scrape; it is only *new* postings that trigger a digest).
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30


class SeenStore:
    """JSONL-backed set of seen job keys with time-based pruning."""

    def __init__(self, path: Path, retention_days: int = DEFAULT_RETENTION_DAYS):
        self.path = Path(path)
        self.retention_days = retention_days
        self._seen: dict[str, float] = {}

    def load(self) -> None:
        """Read the store from disk; missing file starts empty."""
        self._seen = {}
        if not self.path.exists():
            return
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
                self._seen[entry["k"]] = float(entry.get("t") or 0.0)
            except (json.JSONDecodeError, KeyError, TypeError, ValueError):
                logger.debug("skipping malformed state line: %.80s", line)

    def __contains__(self, key: str) -> bool:
        return key in self._seen

    def add(self, key: str, when: datetime | None = None) -> None:
        timestamp = (when or datetime.now(timezone.utc)).timestamp()
        self._seen[key] = timestamp

    def save(self) -> None:
        """Prune stale entries and write the file atomically."""
        cutoff = time.time() - self.retention_days * 86400
        pruned = {k: t for k, t in self._seen.items() if t >= cutoff}
        removed = len(self._seen) - len(pruned)
        if removed:
            logger.info("pruned %d stale seen-keys (>%d days)", removed, self.retention_days)

        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = self.path.with_suffix(self.path.suffix + ".tmp")
        lines = [
            json.dumps({"k": k, "t": t}, separators=(",", ":"))
            for k, t in sorted(pruned.items())
        ]
        tmp_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
        tmp_path.replace(self.path)
        self._seen = pruned

    def __len__(self) -> int:
        return len(self._seen)
