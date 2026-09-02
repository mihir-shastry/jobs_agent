"""Config loading tests."""

from jobagent.config import load_config


def test_defaults_when_missing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("SLACK_WEBHOOK_URL", raising=False)
    config = load_config(tmp_path / "nonexistent.yaml")
    assert config.interval_minutes == 60
    assert config.dashboard_port == 8000
    assert config.enabled_platforms == ["greenhouse", "lever", "ashby"]
    assert config.slack_webhook_url == ""


def test_yaml_overrides(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "scraping:\n  interval_minutes: 30\n"
        "notifications:\n  slack_webhook_url: 'https://hooks.slack.com/x'\n"
        "  filters:\n    experience_levels: [internship, bogus]\n"
        "database:\n  path: data/custom.db\n",
        encoding="utf-8",
    )
    config = load_config(cfg)
    assert config.interval_minutes == 30
    assert config.slack_webhook_url == "https://hooks.slack.com/x"
    # Invalid level filtered out.
    assert config.filter_experience_levels == ["internship"]
    # Relative DB path resolves against the config file's directory.
    assert str(config.database_path).startswith(str(tmp_path))


def test_env_overrides_yaml(tmp_path, monkeypatch):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("notifications:\n  slack_webhook_url: 'yaml-url'\n", encoding="utf-8")
    monkeypatch.setenv("SLACK_WEBHOOK_URL", "env-url")
    config = load_config(cfg)
    assert config.slack_webhook_url == "env-url"


def test_platform_toggle(tmp_path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text(
        "ats_platforms:\n  greenhouse:\n    enabled: false\n", encoding="utf-8"
    )
    config = load_config(cfg)
    assert config.enabled_platforms == ["lever", "ashby"]
