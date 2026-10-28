"""Validated configuration (.env and YAML)."""

from __future__ import annotations

import pytest
import yaml
from pydantic import BaseModel

from aide.common.config import Config, ConfigError, Models, Permissions, Settings
from aide.common.paths import Paths


def validate_yaml_text(name: str, text: str) -> BaseModel:
    """Validates the contents of a config/ YAML without saving it, as at startup."""
    kinds = {"models.yaml": Models, "settings.yaml": Settings, "permissions.yaml": Permissions}
    if name not in kinds:
        raise ConfigError(f"unknown configuration file: {name}")
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError as e:
        raise ConfigError(f"invalid YAML: {e}") from e
    if not isinstance(data, dict):
        raise ConfigError("the file must contain a mapping")
    try:
        return kinds[name].model_validate(data)
    except Exception as e:  # pydantic.ValidationError
        raise ConfigError(str(e)) from e


def test_real_configuration_is_valid():
    config = Config.load(Paths(root=__import__("pathlib").Path(__file__).resolve().parents[1]))
    assert config.role("orchestrator").alias == "opus"
    assert config.role("triage").id == "claude-haiku-5-5"
    # Small tasks with Sonnet and a short cap; the review, with its own cap.
    assert config.role("orchestrator_small").alias == "sonnet"
    assert config.role("light_reviewer").alias == "haiku"
    assert config.settings.caps.small.turns == 12
    assert config.settings.caps.review.turns == 15
    assert config.settings.caps.light_review.turns == 5
    assert config.settings.caps.large.usd == 12.0
    assert config.time_zone == "UTC" or config.environment.TIME_ZONE
    assert config.permissions.own_tools["gmail_send_draft"] == 2
    # Limits for tasks with Chrome.
    assert config.settings.real_browser.max_actions == 60
    assert config.settings.real_browser.max_tokens == 3_000_000
    assert config.settings.real_browser.token_notice == 0.85
    # The example configuration brings nothing from anyone.
    assert config.settings.allowed_folders == []
    assert config.settings.real_browser.browser is None
    assert config.settings.real_browser.device_id is None
    assert not config.settings.no_wifi.active and not config.settings.email.active
    assert "*/checkout*" in config.permissions.chrome.forbidden_pages
    from aide.core.base_session import FIXED_ENVIRONMENT

    assert FIXED_ENVIRONMENT["BASH_MAX_OUTPUT_LENGTH"] == "10000"  # checked with the CLI
    # The maintenance day, in English; the voice, with no fixed language (Whisper detects it).
    assert config.settings.maintenance.day == "sunday"
    assert config.settings.voice.language == ""


def test_invalid_token_warning(tmp_paths: Paths):
    path = tmp_paths.config / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["real_browser"]["token_notice"] = 0
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        Config.load(tmp_paths)


def test_missing_role(tmp_paths: Paths):
    path = tmp_paths.config / "models.yaml"
    data = yaml.safe_load(path.read_text())
    del data["roles"]["reviewer"]
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="reviewer"):
        Config.load(tmp_paths)


def test_invalid_regex_in_permissions():
    text = """
own_tools: {}
paths: {forbidden: [], forbidden_write: [], write_approval: [], free_read: []}
bash: {forbidden: ['(not closed'], approval: [], notice: []}
chrome: {forbidden_domains: [], domains_only_with_approval: [], domains_with_account: [], send_words: []}
"""
    with pytest.raises(ConfigError):
        validate_yaml_text("permissions.yaml", text)


def test_unknown_time_zone(tmp_paths: Paths):
    path = tmp_paths.config / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["time_zone"] = "Mars/Olympus"
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="unknown time zone"):
        Config.load(tmp_paths)


def test_heavy_tasks_at_most_two(tmp_paths: Paths):
    path = tmp_paths.config / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["simultaneous_heavy_tasks"] = 5
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError):
        Config.load(tmp_paths)


def test_env_takes_priority_and_empty_values_are_none(tmp_paths: Paths, monkeypatch):
    for name in ("TIME_ZONE", "PANEL_PORT", "TELEGRAM_ALLOWED_USER_ID"):
        monkeypatch.delenv(name, raising=False)
    tmp_paths.env.write_text(
        "TELEGRAM_BOT_TOKEN=\nTELEGRAM_ALLOWED_USER_ID=\nPANEL_PORT=9999\nTIME_ZONE=Europe/Madrid\n"
    )
    config = Config.load(tmp_paths)
    assert config.environment.TELEGRAM_BOT_TOKEN is None
    assert config.environment.TELEGRAM_ALLOWED_USER_ID is None
    assert config.panel_port == 9999
    assert config.time_zone == "Europe/Madrid"
    assert config.panel_host == "127.0.0.1"
    assert config.user == ""  # without USER_NAME, neutral phrases


def test_user_name_comes_from_env(tmp_paths: Paths, monkeypatch):
    from aide.bot.commands import presentation
    from aide.core.prompts import user_presentation

    monkeypatch.delenv("USER_NAME", raising=False)
    tmp_paths.env.write_text("USER_NAME=  Alex   Doe \n")
    config = Config.load(tmp_paths)
    assert config.user == "Alex Doe"
    assert presentation(config.user).startswith("<b>Hi, Alex Doe. I'm")
    assert presentation("").startswith("<b>Hi. I'm")
    assert user_presentation("Alex Doe") == "\n\nThe user's name is Alex Doe."
    assert user_presentation("") == ""


def test_mcp_server_with_reserved_name(tmp_paths: Paths):
    path = tmp_paths.config / "settings.yaml"
    data = yaml.safe_load(path.read_text())
    data["mcp_servers"] = {"aide": {"config": {"type": "http", "url": "https://x"}}}
    path.write_text(yaml.safe_dump(data))
    with pytest.raises(ConfigError, match="reserved"):
        Config.load(tmp_paths)
