"""Startup check.

A minimal request with Haiku confirms that authentication works and that the active credential is
the one `claude.credential` says in settings.yaml:
- api_key (what the Agent SDK documentation asks for): `apiKeySource == "apiKeyHelper"` in the init
  message, provider `firstParty` if the account says so, the key in the keychain and no credential
  in the environment.
- login: `apiKeySource == "none"`, the account from `get_server_info()` with provider
  `firstParty` and a claude.ai subscription, and no credential in the environment.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    HookMatcher,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
)

from aide.common import api_key
from aide.common.config import Config
from aide.common.environment import present_credentials
from aide.core.base_session import sdk_environment, sdk_settings
from aide.core.system import ChromeState, chrome_state

log = logging.getLogger(__name__)

# The longest to wait for the Chrome extension's browser list: at startup this check is
# awaited before looking at the scheduled tasks, and on 10-28 it took 3 minutes.
BROWSERS_TIME = 60

LOGIN_STEPS = [
    "Open the Terminal app on your Mac.",
    "Type claude and press Return.",
    "Inside Claude Code, type /login and follow the steps with your claude.ai account.",
    "When it's done, send me any message and I'll check again.",
]
KEY_STEPS = [
    "Create an API key in the Claude Console (platform.claude.com, API keys).",
    "In the Terminal, in the Aide folder, run: uv run aide --set-api-key",
    "Paste the key when it asks (it's saved in the macOS keychain).",
    "When it's done, send me any message and I'll check again.",
]
CHROME_STEPS = [
    "Open Google Chrome on your Mac.",
    "Check that the Claude in Chrome extension is turned on and signed in to your account.",
    "If it still doesn't connect, open claude in the Terminal, type /chrome and choose Reconnect extension.",
]
LOGIN_SIGNALS = ("failed to authenticate", "login expired", "oauth", "please run /login", "401")
KEY_SIGNALS = (
    "invalid api key",
    "invalid x-api-key",
    "apikeyhelper",
    "not logged in",
    "401",
    "403",
)


def credential_steps(config: Config) -> list[str]:
    """The steps to fix the credential the agent uses."""
    return KEY_STEPS if config.uses_api_key else LOGIN_STEPS


def credential_name(config: Config) -> str:
    return "Claude API key" if config.uses_api_key else "Claude Code login"


LIST_TOOL = "mcp__claude-in-chrome__list_connected_browsers"


@dataclass
class RealBrowserState:
    """Which of the account's browsers is connected and in use (checked by its deviceId)."""

    expected: str | None
    device_id: str | None
    connected: bool = False
    in_usage: bool = False
    browsers: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None

    @property
    def correct(self) -> bool:
        return self.connected and self.in_usage

    def text(self) -> str:
        """Describes in words whether the expected browser is connected and in use."""
        name = self.expected or self.device_id or "the configured browser"
        if self.error:
            return f"could not check the connected browsers ({self.error})"
        if self.correct:
            return f"{name} connected and in use"
        if self.connected:
            return f"{name} is connected but is not the one in use"
        other = ", ".join(n.get("name", "?") for n in self.browsers) or "none"
        return f"{name} is not connected (connected: {other})"


def evaluate_browsers(
    items: list[dict[str, Any]], device_id: str | None, name: str | None
) -> RealBrowserState:
    """Returns a RealBrowserState showing whether the expected browser is connected."""
    state = RealBrowserState(expected=name, device_id=device_id, browsers=items)
    for browser in items:
        matches = (device_id and browser.get("deviceId") == device_id) or (
            not device_id and name and browser.get("name") == name
        )
        if matches:
            state.connected = True
            state.in_usage = bool(browser.get("inUse"))
    return state


def reply_list(reply: Any) -> list[dict[str, Any]] | None:
    """Extracts the JSON list from the list_connected_browsers response."""
    from aide.core.hooks import reply_text

    text = reply_text(reply)
    start = text.find("[")
    if start < 0:
        return None
    try:
        data, _ = json.JSONDecoder().raw_decode(text[start:])
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, list) else None


async def query_browsers(config: Config) -> RealBrowserState:
    """Minimal session with --chrome that can only call list_connected_browsers.

    The model only triggers the call; the list is read from the PostToolUse hook and the code
    decides.
    """
    settings = config.settings.real_browser
    captured: dict[str, Any] = {}

    async def screenshot(entry: dict[str, Any], _id: str | None, _ctx: Any) -> dict[str, Any]:
        captured["items"] = reply_list(entry.get("tool_response"))
        return {}

    options = ClaudeAgentOptions(
        model=config.role("triage").id,
        effort="low",
        tools=[],
        allowed_tools=[LIST_TOOL],
        setting_sources=[],
        system_prompt="Call the list_connected_browsers tool once and reply: done.",
        max_turns=3,
        cwd=str(config.paths.agent),
        permission_mode="default",
        env=sdk_environment(config),
        settings=sdk_settings(config),
        extra_args={"chrome": None},
        hooks={"PostToolUse": [HookMatcher(matcher=LIST_TOOL, hooks=[screenshot], timeout=30)]},
    )
    error = None

    async def query() -> None:
        async with ClaudeSDKClient(options) as client:
            await client.query("List the connected browsers.")
            async for _ in client.receive_response():
                pass

    try:
        await asyncio.wait_for(query(), BROWSERS_TIME)
    except TimeoutError:
        error = f"the Chrome extension did not respond within {BROWSERS_TIME} s"
        log.warning("could not check the connected browsers: %s", error)
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        log.exception("could not check the connected browsers")
    items = captured.get("items")
    if items is None:
        return RealBrowserState(settings.browser, settings.device_id, error=error or "no response")
    state = evaluate_browsers(items, settings.device_id, settings.browser)
    log.info("connected browsers: %s", [(n.get("name"), n.get("inUse")) for n in items])
    return state


class _NoKey(Exception):
    """There is no API key in the keychain."""


@dataclass
class AuthenticationResult:
    """The result of a sign-in check: status, plan, browser state and problems."""

    ok: bool
    api_key_source: str | None = None
    provider: str | None = None
    plan: str | None = None
    organization: str | None = None
    model: str | None = None
    expired_login: bool = False
    chrome: ChromeState | None = None
    browser: RealBrowserState | None = None
    api_key: bool = False
    problems: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    steps: list[str] = field(default_factory=list)

    @property
    def credential(self) -> str:
        if self.ok and self.api_key:
            return "Claude Console API key (from the keychain, through apiKeyHelper)"
        if self.ok:
            return "claude.ai login (/login)"
        return "not confirmed"

    def text(self) -> str:
        """Returns a readable summary of the sign-in check result."""
        lines = []
        if self.ok:
            lines.append(f"Signed in correctly with the {self.credential}.")
        else:
            lines.append("The sign-in check failed.")
        if self.plan:
            lines.append(f"Plan detected: {self.plan}.")
        if self.browser is not None:
            lines.append(f"Real Chrome: {self.browser.text()}.")
        elif self.chrome is not None:
            state = "connected" if self.chrome.ready else self.chrome.detail
            lines.append(f"Real Chrome: {state}.")
        lines += self.problems + self.notices
        if self.steps:
            lines.append("To fix it:")
            lines += [f"{i}. {p}" for i, p in enumerate(self.steps, 1)]
        return "\n".join(lines)


def evaluate_authentication(
    *,
    init: dict[str, Any] | None,
    account: dict[str, Any] | None,
    ok_result: bool,
    error: str | None,
    credentials_in_environment: list[str],
    real_browser: bool,
    chrome: ChromeState | None,
    expected_plan: str = "Max",
    api_key: bool = False,
    key_in_keychain: bool = True,
) -> AuthenticationResult:
    """Decides by code whether the active credential is the configured one: the keychain API key
    (`api_key`) or the claude.ai login."""
    r = AuthenticationResult(ok=False, chrome=chrome, api_key=api_key)
    account = account or {}
    r.api_key_source = (init or {}).get("apiKeySource")
    r.model = (init or {}).get("model")
    r.provider = account.get("apiProvider")
    r.plan = account.get("subscriptionType")
    r.organization = account.get("organization")

    if api_key:
        return _evaluate_key(
            r, init, account, ok_result, error, credentials_in_environment, key_in_keychain
        )

    error_text = (error or "").lower()
    if error and any(s in error_text for s in LOGIN_SIGNALS):
        r.expired_login = True
        r.problems.append("The Claude Code login is not valid or has expired.")
        r.steps += LOGIN_STEPS
    elif error:
        r.problems.append(f"The test request failed: {error}")

    if credentials_in_environment:
        r.problems.append(
            "Some credentials are still set and take priority over your subscription: "
            + ", ".join(credentials_in_environment)
        )
    if init is not None and r.api_key_source != "none":
        r.problems.append(
            f"The session uses an API key ({r.api_key_source}) instead of your subscription."
        )
    if account and r.provider not in (None, "firstParty"):
        r.problems.append(f"The session does not use the Anthropic API directly ({r.provider}).")
    if not account and not r.expired_login:
        r.problems.append("Could not read the Claude Code account.")
    if account and not r.plan and not r.expired_login:
        r.problems.append("The account does not report any claude.ai subscription.")
        r.steps += [p for p in LOGIN_STEPS if p not in r.steps]

    if r.plan and expected_plan.lower() not in r.plan.lower():
        r.notices.append(
            f"The active plan is {r.plan}; this setup expects {expected_plan}. "
            "Usage limits will be those of that plan."
        )

    if real_browser and chrome is not None and not chrome.ready:
        r.notices.append(f"The real browser is turned on, but {chrome.detail}.")
        r.steps += [p for p in CHROME_STEPS if p not in r.steps]

    r.ok = (
        ok_result
        and not r.problems
        and r.api_key_source == "none"
        and r.provider == "firstParty"
        and bool(r.plan)
    )
    return r


def _evaluate_key(
    r: AuthenticationResult,
    init: dict[str, Any] | None,
    account: dict[str, Any],
    ok_result: bool,
    error: str | None,
    credentials_in_environment: list[str],
    key_in_keychain: bool,
) -> AuthenticationResult:
    """The API key: it must arrive through `apiKeyHelper` and not through the environment."""
    if not key_in_keychain:
        r.expired_login = True
        r.problems.append("There is no Claude API key in the keychain.")
        r.steps += KEY_STEPS
    error_text = (error or "").lower()
    if error and any(s in error_text for s in KEY_SIGNALS):
        r.expired_login = True
        r.problems.append("Claude didn't accept the API key (it may be wrong or revoked).")
        r.steps += [p for p in KEY_STEPS if p not in r.steps]
    elif error:
        r.problems.append(f"The test request failed: {error}")
    if credentials_in_environment:
        r.problems.append(
            "Some credentials are still set in the environment, where the agent's commands "
            "could see them: " + ", ".join(credentials_in_environment)
        )
    if init is not None and r.api_key_source != "apiKeyHelper":
        r.problems.append(
            f"The session doesn't take the key from the keychain helper ({r.api_key_source})."
        )
    if r.provider not in (None, "firstParty"):
        r.problems.append(f"The session does not use the Anthropic API directly ({r.provider}).")
    r.ok = ok_result and not r.problems and r.api_key_source == "apiKeyHelper"
    return r


async def check_authentication(
    config: Config,
    at_limit: Callable[[Any], Awaitable[None]] | None = None,
    browsers: bool = True,
) -> AuthenticationResult:
    """Launches the minimal request with Haiku and evaluates the result.

    `at_limit` receives the plan limits event that comes with the response (this is how limits data
    is saved at startup and when requested). With `browsers=False` the list of connected browsers
    is not queried (faster when only the limits matter).
    """
    browser = config.real_browser
    chrome = chrome_state() if browser else None
    uses_key = config.uses_api_key
    key_in_keychain = bool(await asyncio.to_thread(api_key.read)) if uses_key else True
    init: dict[str, Any] | None = None
    account: dict[str, Any] | None = None
    ok_result = False
    error: str | None = None

    options = ClaudeAgentOptions(
        model=config.role("triage").id,
        effort="low",
        tools=[],
        setting_sources=[],
        system_prompt="Reply exactly: ok",
        max_turns=1,
        cwd=str(config.paths.agent),
        permission_mode="default",
        env=sdk_environment(config),
        settings=sdk_settings(config),
    )
    try:
        if not key_in_keychain:
            # Without a key the request is not launched: it would fail anyway.
            raise _NoKey
        async with ClaudeSDKClient(options) as client:
            info = await client.get_server_info() or {}
            account = info.get("account") or {}
            await client.query("ok")
            async for message in client.receive_response():
                if isinstance(message, SystemMessage) and message.subtype == "init":
                    init = message.data
                elif isinstance(message, AssistantMessage) and message.error:
                    texts = [b.text for b in message.content if isinstance(b, TextBlock)]
                    error = f"{message.error}: {' '.join(texts)}".strip()
                elif isinstance(message, RateLimitEvent) and at_limit is not None:
                    try:
                        await at_limit(message.rate_limit_info)
                    except Exception:
                        log.exception("could not save the limits event")
                elif isinstance(message, ResultMessage):
                    ok_result = message.subtype == "success" and not message.is_error
                    if message.is_error and not error:
                        error = message.result or ", ".join(message.errors or []) or "error"
    except _NoKey:
        log.warning("sign-in: there is no Claude API key in the keychain")
    except Exception as e:  # CLINotFoundError, ProcessError, CLIConnectionError...
        error = f"{type(e).__name__}: {e}"
        log.exception("the sign-in check failed")

    result = evaluate_authentication(
        init=init,
        account=account,
        ok_result=ok_result,
        error=error,
        credentials_in_environment=present_credentials(browser, api_key=uses_key),
        real_browser=browser,
        chrome=chrome,
        api_key=uses_key,
        key_in_keychain=key_in_keychain,
    )
    if browsers and browser and result.ok and chrome is not None and chrome.ready:
        result.browser = await query_browsers(config)
        if not result.browser.correct:
            expected = config.settings.real_browser.browser or "this Mac's browser"
            result.notices.append(f"The real Chrome is not ready: {result.browser.text()}.")
            result.steps.append(
                f"In the Terminal, open claude, type /chrome and select {expected}."
            )
    log.info(
        "sign-in: ok=%s apiKeySource=%s provider=%s plan=%s browser=%s",
        result.ok,
        result.api_key_source,
        result.provider,
        result.plan,
        result.browser.text() if result.browser else "-",
    )
    return result
