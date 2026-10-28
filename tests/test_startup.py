"""Startup cleans up the credentials and warns.

System checks without a model.

The authentication check confirms the claude.ai login."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import socket
import tempfile
from pathlib import Path

from aide.common.environment import clean_environment, present_credentials
from aide.core.authentication import (  # noqa: E402
    evaluate_authentication,
    evaluate_browsers,
    reply_list,
)
from aide.core.base_session import excluded_claude_md, sdk_environment, sdk_settings
from aide.core.system import ChromeState, KeepAwake, _socket_replies, chrome_state
from aide.main import prepare

# --- from test_entorno -----------------------------------------------------


def _dirty_environment() -> dict[str, str]:
    return {
        "PATH": "/usr/bin",
        "HOME": "/nonexistent/home",
        "ANTHROPIC_API_KEY": "sk-ant-api03-SECRET",
        "ANTHROPIC_AUTH_TOKEN": "secret-token",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-SECRET",
        "CLAUDE_CODE_USE_BEDROCK": "1",
        "CLAUDECODE": "1",
        "CLAUDE_CODE_SESSION_ID": "abc",
        "CLAUDE_CODE_ENABLE_TASKS": "0",
        "CLAUDE_AGENT_SDK_VERSION": "0.0.1",
    }


def test_with_real_browser_removes_the_three_credentials():
    env = _dirty_environment()
    report = clean_environment(real_browser=True, environment=env)
    assert set(report.removed_credentials) == {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        assert name not in env
    assert "CLAUDE_CODE_USE_BEDROCK" in report.other_removed_sources
    assert {"CLAUDECODE", "CLAUDE_CODE_SESSION_ID", "CLAUDE_CODE_ENABLE_TASKS"} <= set(
        report.inherited_removed
    )
    assert env == {"PATH": "/usr/bin", "HOME": "/nonexistent/home"}
    assert present_credentials(True, env) == []


def test_without_real_browser_keeps_the_long_token():
    env = _dirty_environment()
    report = clean_environment(real_browser=False, environment=env)
    assert "CLAUDE_CODE_OAUTH_TOKEN" in env
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in report.removed_credentials
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env


def test_warnings_name_variables_and_never_values():
    env = _dirty_environment()
    report = clean_environment(real_browser=True, long_token_in_env_file=True, environment=env)
    notices = " ".join(report.notices())
    assert "ANTHROPIC_API_KEY" in notices and "ANTHROPIC_AUTH_TOKEN" in notices
    assert "SECRET" not in notices and "secret-token" not in notices
    assert "CLAUDE_CODE_OAUTH_TOKEN" in notices  # warning that it is in .env and not used


def test_clean_environment_generates_no_warnings():
    env = {"PATH": "/usr/bin"}
    report = clean_environment(real_browser=True, environment=env)
    assert report.notices() == []
    assert not report.had_credentials


def test_prepare_cleans_os_environ_and_warns(tmp_paths, monkeypatch, caplog):
    """With /login and the real Chrome (the API key is in test_clave_api.py)."""
    settings = tmp_paths.config / "settings.yaml"
    text = settings.read_text(encoding="utf-8")
    text = text.replace("credential: api_key", "credential: login")
    text = text.replace("real_browser:\n  enabled: false", "real_browser:\n  enabled: true")
    settings.write_text(text, encoding="utf-8")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-api03-TESTING")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "another-secret")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-TESTING")
    with caplog.at_level(logging.WARNING):
        config, report = prepare(tmp_paths, console=False)
    assert config.real_browser is True
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in os.environ
    assert set(report.removed_credentials) == {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
    text = caplog.text
    assert "ANTHROPIC_API_KEY" in text
    assert "TESTING" not in text and "another-secret" not in text


# --- from test_sistema -----------------------------------------------------


def test_without_bridge_the_extension_is_not_connected(tmp_path: Path):
    state = chrome_state(folder=tmp_path / "no-existe")
    assert state.connected_extension is False
    assert not state.ready


def test_socket_that_is_not_the_bridges_does_not_count():
    # Unix socket paths have a maximum of 104 characters on macOS.
    folder = Path(tempfile.mkdtemp(prefix="md-", dir="/tmp"))
    path = folder / "1.sock"  # pid 1 (launchd) is not the bridge process
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(path))
    server.listen(1)
    try:
        assert _socket_replies(path)
        assert chrome_state(folder=folder).connected_extension is False
    finally:
        server.close()
        shutil.rmtree(folder, ignore_errors=True)


async def test_caffeinate_is_activated_and_released():
    awake = KeepAwake()
    await awake.acquire()
    await awake.acquire()
    assert awake.active
    await awake.release()
    assert awake.active  # there is still a task
    await awake.release()
    await asyncio.sleep(0.05)
    assert not awake.active


def test_development_claude_md_excluded(config_tmp):
    excluded = excluded_claude_md(config_tmp.paths.root)
    assert str(config_tmp.paths.root / "CLAUDE.md") in excluded
    assert str(config_tmp.paths.root.parent / "CLAUDE.md") in excluded
    assert str(config_tmp.paths.agent / "CLAUDE.md") not in excluded
    assert '"disableClaudeAiConnectors": true' in sdk_settings(config_tmp)


def test_sdk_environment_sets_limits_and_does_not_pass_the_token_with_browser(config_tmp):
    env = sdk_environment(config_tmp)
    assert env["CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH"] == "1"
    assert env["CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS"] == "3"
    assert env["CLAUDE_CODE_ENABLE_TODO_TOOLS"] == "1"
    assert env["ENABLE_CLAUDEAI_MCP_SERVERS"] == "false"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
    assert "ANTHROPIC_API_KEY" not in env


# --- from test_autenticacion -----------------------------------------------

INIT_OK = {"apiKeySource": "none", "model": "claude-haiku-5-5"}


MAX_ACCOUNT = {"subscriptionType": "Claude Max", "apiProvider": "firstParty", "organization": "x"}


CHROME_OK = ChromeState(True, True, "bridge active")


def _evaluate(**changes):
    base = dict(
        init=INIT_OK,
        account=MAX_ACCOUNT,
        ok_result=True,
        error=None,
        credentials_in_environment=[],
        real_browser=True,
        chrome=CHROME_OK,
    )
    base.update(changes)
    return evaluate_authentication(**base)


def test_claude_ai_login_with_max():
    r = _evaluate()
    assert r.ok
    assert r.notices == [] and r.problems == []
    assert "claude.ai login" in r.text()


def test_pro_plan_works_but_warns():
    r = _evaluate(account={**MAX_ACCOUNT, "subscriptionType": "Claude Pro"})
    assert r.ok
    assert any("Claude Pro" in a for a in r.notices)


def test_api_key_is_not_valid():
    r = _evaluate(init={"apiKeySource": "ANTHROPIC_API_KEY"})
    assert not r.ok
    assert any("API key" in p for p in r.problems)


def test_credentials_in_the_environment_is_not_valid():
    r = _evaluate(credentials_in_environment=["ANTHROPIC_AUTH_TOKEN"])
    assert not r.ok


def test_cloud_provider_is_not_valid():
    r = _evaluate(account={**MAX_ACCOUNT, "apiProvider": "bedrock"})
    assert not r.ok


def test_expired_login_gives_steps():
    r = _evaluate(
        init=None,
        account={},
        ok_result=False,
        error="authentication_failed: Failed to authenticate: OAuth session expired and could not be refreshed",
    )
    assert not r.ok and r.expired_login
    assert any("/login" in p for p in r.steps)


def test_chrome_disconnected_warns_with_steps():
    r = _evaluate(
        chrome=ChromeState(True, False, "the Claude in Chrome extension is not connected")
    )
    assert r.ok  # authentication is correct; Chrome is a separate warning
    assert any("extension" in a for a in r.notices)
    assert any("Chrome" in p for p in r.steps)


REAL_REPLY = [
    {
        "type": "text",
        "text": '[{"deviceId":"00000000-0000-4000-8000-000000000001","name":"Browser 1","osPlatform":"macOS",'
        '"connectedAt":1792425585692,"isLocal":true,"inUse":true},{"deviceId":"04b54d95-40a2-4fe5-ae79-00968630a3be",'
        '"name":"Browser 2","osPlatform":"macOS","connectedAt":1792423385318,"isLocal":true}]'
        '2 browsers are connected; "Browser 1" is the one in use for this session.',
    }
]


EXPECTED = "00000000-0000-4000-8000-000000000001"


def test_browser_list_from_the_response():
    items = reply_list(REAL_REPLY)
    assert [n["name"] for n in items] == ["Browser 1", "Browser 2"]


def test_expected_browser_in_use():
    state = evaluate_browsers(reply_list(REAL_REPLY), EXPECTED, "Browser 1")
    assert state.correct
    assert "Browser 1 connected and in use" in state.text()


def test_expected_browser_connected_but_not_in_use():
    items = reply_list(REAL_REPLY)
    items[0]["inUse"] = False
    state = evaluate_browsers(items, EXPECTED, "Browser 1")
    assert state.connected and not state.in_usage and not state.correct


def test_expected_browser_not_connected():
    items = [n for n in reply_list(REAL_REPLY) if n["name"] != "Browser 1"]
    state = evaluate_browsers(items, EXPECTED, "Browser 1")
    assert not state.connected
    assert "Browser 2" in state.text()


async def test_the_browser_query_does_not_block_startup(config_tmp, monkeypatch):
    """If the Chrome extension does not answer, it waits at most BROWSERS_TIME and
    continues with an error, instead of leaving startup stuck (on 28-10 it took 3 minutes)."""
    import asyncio
    import time

    from aide.core import authentication

    closed = asyncio.Event()

    class HungClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            closed.set()

        async def query(self, text):
            await asyncio.sleep(3600)

    monkeypatch.setattr(authentication, "ClaudeSDKClient", HungClient)
    monkeypatch.setattr(authentication, "BROWSERS_TIME", 0.2)
    start = time.monotonic()
    state = await authentication.query_browsers(config_tmp)
    assert time.monotonic() - start < 2
    assert not state.correct and "did not respond" in state.error
    assert closed.is_set()  # the session is closed, not left open
