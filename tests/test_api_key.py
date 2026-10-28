"""The Claude API key (what the Agent SDK documentation asks for): it is stored in the
keychain, reaches Claude Code through `apiKeyHelper` and is never in the agent sessions'
environment. The startup check and the weekly report accept it. No network, no model and
with the fake keychain from conftest."""

from __future__ import annotations

import json
import logging
import os

import pytest

from aide.common import api_key
from aide.common.config import Settings, read_yaml
from aide.common.environment import clean_environment, present_credentials
from aide.core import authentication
from aide.core.authentication import evaluate_authentication
from aide.core.base_session import sdk_environment, sdk_settings
from aide.main import prepare, request_key

KEY = "sk-ant-api03-EXAMPLE-FOR-TESTS-0000000000"
KEYCHAIN = (api_key.SERVICE, api_key.ACCOUNT)


@pytest.fixture
def config_key(config_tmp):
    config_tmp.settings.claude.credential = "api_key"
    config_tmp.settings.real_browser.enabled = False
    return config_tmp


# --- the configuration --------------------------------------------------------------------


def test_repo_settings_use_the_api_key(tmp_paths):
    settings = Settings.model_validate(read_yaml(tmp_paths.config / "settings.yaml"))
    assert settings.claude.credential == "api_key"
    assert settings.real_browser.enabled is False


def test_real_chrome_needs_the_login(tmp_paths):
    data = read_yaml(tmp_paths.config / "settings.yaml")
    data["real_browser"]["enabled"] = True
    with pytest.raises(ValueError, match="real_browser needs claude.credential: login"):
        Settings.model_validate(data)
    data["claude"] = {"credential": "login"}
    assert Settings.model_validate(data).real_browser.enabled


# --- the sessions ---------------------------------------------------------------------------


def test_helper_is_in_settings_and_key_never_in_environment(config_key, monkeypatch):
    monkeypatch.setattr(api_key.sys, "executable", "/opt/My Agent/.venv/bin/python3")
    settings = json.loads(sdk_settings(config_key, {"apiKeyHelper": "echo otra"}))
    # With -I (without the current folder on the module path) and the path in quotes.
    assert settings["apiKeyHelper"] == (
        "'/opt/My Agent/.venv/bin/python3' -I -m aide.common.api_key"
    )
    from pydantic import SecretStr

    config_key.environment.CLAUDE_CODE_OAUTH_TOKEN = SecretStr("sk-ant-oat01-EXAMPLE")
    config_key.environment.ANTHROPIC_API_KEY = SecretStr(KEY)
    env = sdk_environment(config_key)
    assert not {"ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"} & set(env)
    assert KEY not in json.dumps(env) and KEY not in json.dumps(settings)


def test_with_login_there_is_no_helper(config_tmp):
    assert "apiKeyHelper" not in json.loads(sdk_settings(config_tmp))


# --- the environment at startup -------------------------------------------------------------


def test_clean_environment_collects_the_key_without_leaving_it(monkeypatch):
    env = {
        "ANTHROPIC_API_KEY": f"  {KEY}\n",
        "ANTHROPIC_AUTH_TOKEN": "other",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-EXAMPLE",
        "PATH": "/usr/bin",
    }
    report = clean_environment(real_browser=False, environment=env, api_key=True)
    assert report.collected_key == KEY
    assert set(env) == {"PATH"}
    assert set(report.removed_credentials) == {
        "ANTHROPIC_AUTH_TOKEN",
        "CLAUDE_CODE_OAUTH_TOKEN",
    }
    text = " ".join(report.notices()) + repr(report)
    assert "keychain" in text and KEY not in text
    assert present_credentials(False, {"CLAUDE_CODE_OAUTH_TOKEN": "x"}, api_key=True) == [
        "CLAUDE_CODE_OAUTH_TOKEN"
    ]


def test_prepare_stores_the_environment_key_in_the_keychain(
    tmp_paths, monkeypatch, caplog, fake_keychain
):
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    with caplog.at_level(logging.INFO):
        config, report = prepare(tmp_paths, console=False)
    assert config.uses_api_key
    assert "ANTHROPIC_API_KEY" not in os.environ
    assert fake_keychain[KEYCHAIN] == KEY
    assert "keychain" in caplog.text and KEY not in caplog.text


def test_prepare_stores_the_env_file_key_in_the_keychain(tmp_paths, caplog, fake_keychain):
    tmp_paths.env.write_text(f"ANTHROPIC_API_KEY={KEY}\n", encoding="utf-8")
    with caplog.at_level(logging.WARNING):
        _, report = prepare(tmp_paths, console=False)
    assert fake_keychain[KEYCHAIN] == KEY
    assert report.key_in_env_file and "ANTHROPIC_API_KEY" not in os.environ
    assert "delete it from .env" in caplog.text and KEY not in caplog.text


def test_set_api_key_stores_only_a_well_formed_key(fake_keychain, capsys):
    assert request_key(lambda _: "no es una clave") == 1
    assert KEYCHAIN not in fake_keychain
    assert request_key(lambda _: f" {KEY} ") == 0
    assert fake_keychain[KEYCHAIN] == KEY
    assert KEY not in capsys.readouterr().out


# --- the helper -----------------------------------------------------------------------------


def test_helper_gives_the_key_only_outside_the_sandbox(monkeypatch, capsys, fake_keychain):
    monkeypatch.setattr(api_key, "in_sandbox", lambda pid=None: False)
    assert api_key.main() == 1  # no key in the keychain
    assert capsys.readouterr().out == ""
    api_key.save(KEY)
    assert api_key.main() == 0
    assert capsys.readouterr().out == KEY
    # An agent Bash command runs inside the sandbox: there it does not give it.
    monkeypatch.setattr(api_key, "in_sandbox", lambda pid=None: True)
    assert api_key.main() == 1
    output = capsys.readouterr()
    assert output.out == "" and KEY not in output.err


def test_this_process_is_not_in_a_sandbox():
    if api_key.sys.platform == "darwin":
        assert api_key.in_sandbox() is False


# --- the startup check ----------------------------------------------------------------------

INIT_KEY = {"apiKeySource": "apiKeyHelper", "model": "claude-haiku-5-5"}


def _evaluate(**changes):
    base = dict(
        init=INIT_KEY,
        account={"apiProvider": "firstParty"},
        ok_result=True,
        error=None,
        credentials_in_environment=[],
        real_browser=False,
        chrome=None,
        api_key=True,
        key_in_keychain=True,
    )
    base.update(changes)
    return evaluate_authentication(**base)


def test_helper_key_is_valid_without_a_subscription():
    r = _evaluate()
    assert r.ok and r.problems == [] and r.notices == []
    assert "API key" in r.text() and "apiKeyHelper" in r.text()
    assert _evaluate(account={}).ok  # the account does not have to say anything


@pytest.mark.parametrize(
    "changes",
    [
        {"init": {"apiKeySource": "ANTHROPIC_API_KEY"}},  # the key arrived through the environment
        {"init": {"apiKeySource": "none"}},
        {"credentials_in_environment": ["ANTHROPIC_AUTH_TOKEN"]},
        {"account": {"apiProvider": "bedrock"}},
        {"ok_result": False},
    ],
)
def test_key_must_arrive_through_the_helper(changes):
    assert not _evaluate(**changes).ok


def test_no_key_or_rejected_key_gives_the_steps():
    for r in (
        _evaluate(key_in_keychain=False, init=None, ok_result=False),
        _evaluate(error="authentication_failed: Invalid API key", ok_result=False),
    ):
        assert not r.ok and r.expired_login
        assert any("--set-api-key" in p for p in r.steps)
        assert not any("/login" in p for p in r.steps)


async def test_without_a_key_the_request_is_not_launched(config_key, monkeypatch):
    def never(*a, **k):
        raise AssertionError("no debía abrir una sesión")

    monkeypatch.setattr(authentication, "ClaudeSDKClient", never)
    r = await authentication.check_authentication(config_key, browsers=False)
    assert not r.ok and r.problems == ["There is no Claude API key in the keychain."]


# --- the weekly report ----------------------------------------------------------------------


@pytest.fixture
async def engine(config_key, monkeypatch, tmp_path):
    from aide.bot.channel import FakeChannel
    from aide.core.engine import Engine
    from aide.maintenance import security
    from tests.fake_sdk import FakeClient, Recorder

    FakeClient.registry = Recorder()
    monkeypatch.setattr(security, "PLIST", tmp_path / "no-existe.plist")
    monkeypatch.setattr(security, "audit_dependencies", lambda root: ([], None))
    monkeypatch.setattr(security, "process_listeners", lambda port, pid=None: ["127.0.0.1"])
    m = Engine(config_key, channel=FakeChannel(print_out=False))
    await m.start(workers=False)
    yield m
    await m.stop()


async def test_weekly_report_accepts_the_keychain_key(engine, monkeypatch):
    from aide.maintenance import security

    paths = engine.config.paths
    paths.env.write_text("TELEGRAM_BOT_TOKEN=x\n")
    paths.env.chmod(0o600)

    s = await security.review(engine)
    assert any("no Claude API key in the keychain" in line for line in s.lines)
    assert any("--set-api-key" in q for q in s.that_do)

    api_key.save(KEY)
    s = await security.review(engine)
    assert any("through apiKeyHelper" in line for line in s.lines)
    assert not any("Anthropic" in line and "outside" in line for line in s.lines)

    # Outside the keychain, no: neither in the environment nor in .env.
    monkeypatch.setenv("ANTHROPIC_API_KEY", KEY)
    s = await security.review(engine)
    assert any("outside the keychain" in line for line in s.lines)
    assert KEY not in json.dumps(s.for_model())
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    paths.env.write_text("ANTHROPIC_AUTH_TOKEN=x\n")
    s = await security.review(engine)
    assert any("outside the keychain" in line for line in s.lines)
