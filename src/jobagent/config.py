"""Configuration loading: config.yaml defaults, overridden by environment variables."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import yaml

# Environment variables that override config.yaml values.
ENV_SLACK_WEBHOOK_URL = "SLACK_WEBHOOK_URL"
ENV_DB_PATH = "JOBAGENT_DB_PATH"
ENV_CONFIG_PATH = "JOBAGENT_CONFIG_PATH"

DEFAULT_CONFIG_PATH = "config.yaml"

_VALID_LEVELS = {"internship", "new_grad", "entry", "mid", "senior"}
_VALID_CATEGORIES = {"swe", "data_science", "pm", "design", "quant", "other"}


class Config:
    """Typed access to configuration values.

    Built from a plain dict (parsed YAML) merged over defaults; environment
    variables take precedence.
    """

    def __init__(self, data: dict[str, Any], base_dir: Path):
        self._data = data
        self._base_dir = base_dir

    # -- scraping ---------------------------------------------------------

    @property
    def interval_minutes(self) -> int:
        return int(self._get("scraping", "interval_minutes", default=60))

    @property
    def max_concurrent_requests(self) -> int:
        return int(self._get("scraping", "max_concurrent_requests", default=5))

    @property
    def request_delay_ms(self) -> int:
        return int(self._get("scraping", "request_delay_ms", default=500))

    # -- ATS platforms ----------------------------------------------------

    def platform_enabled(self, platform: str) -> bool:
        return bool(self._get("ats_platforms", platform, "enabled", default=True))

    @property
    def enabled_platforms(self) -> list[str]:
        return [p for p in ("greenhouse", "lever", "ashby") if self.platform_enabled(p)]

    # -- notifications ----------------------------------------------------

    @property
    def slack_webhook_url(self) -> str:
        env = os.environ.get(ENV_SLACK_WEBHOOK_URL, "").strip()
        if env:
            return env
        return str(self._get("notifications", "slack_webhook_url", default="")).strip()

    @property
    def notifications_enabled(self) -> bool:
        return bool(self._get("notifications", "enabled", default=True))

    @property
    def filter_experience_levels(self) -> list[str]:
        levels = self._get("notifications", "filters", "experience_levels", default=[]) or []
        return [lv for lv in levels if lv in _VALID_LEVELS]

    @property
    def filter_categories(self) -> list[str]:
        cats = self._get("notifications", "filters", "categories", default=[]) or []
        return [c for c in cats if c in _VALID_CATEGORIES]

    # -- dashboard --------------------------------------------------------

    @property
    def dashboard_host(self) -> str:
        return str(self._get("dashboard", "host", default="127.0.0.1"))

    @property
    def dashboard_port(self) -> int:
        return int(self._get("dashboard", "port", default=8000))

    # -- database ---------------------------------------------------------

    @property
    def database_path(self) -> Path:
        env = os.environ.get(ENV_DB_PATH, "").strip()
        if env:
            return Path(env).expanduser()
        raw = Path(str(self._get("database", "path", default="data/jobs.db")))
        if not raw.is_absolute():
            raw = self._base_dir / raw
        return raw

    # -- helpers ----------------------------------------------------------

    def _get(self, *keys: str, default: Any = None) -> Any:
        node: Any = self._data
        for key in keys:
            if not isinstance(node, dict) or key not in node:
                return default
            node = node[key]
        return node if node is not None else default


def load_config(config_path: Optional[str | Path] = None) -> Config:
    """Load configuration from YAML, falling back to defaults for anything missing."""
    path_str = (
        config_path
        or os.environ.get(ENV_CONFIG_PATH)
        or DEFAULT_CONFIG_PATH
    )
    path = Path(path_str).expanduser()
    if path.exists():
        with path.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            data = {}
        # config.yaml lives in the project root, so its directory IS the
        # base for resolving relative paths (e.g. data/jobs.db).
        base_dir = path.resolve().parent
    else:
        data = {}
        base_dir = Path.cwd()

    return Config(data, base_dir)
