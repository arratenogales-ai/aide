"""Environment cleanup of the process before launching any SDK session.

The SDK builds the Claude Code subprocess environment from os.environ plus
ClaudeAgentOptions.env, so not passing the credentials is not enough: they must be removed
from the process itself. Values are never logged, only names.

With `claude.credential: api_key`, ANTHROPIC_API_KEY (the Agent SDK quickstart's form) is also
removed from the environment, but not lost: it is collected to be saved in the keychain, and from
there Claude Code requests it with `apiKeyHelper` (see common/api_key.py).
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# Credentials that take priority over the /login login (authentication documentation).
CREDENTIALS_ALWAYS = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
CREDENTIAL_LONG_TOKEN = "CLAUDE_CODE_OAUTH_TOKEN"
API_KEY = "ANTHROPIC_API_KEY"

# Other sources that divert authentication away from the subscription.
OTHER_SOURCES = (
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_PROFILE",
    "ANTHROPIC_FEDERATION_RULE_ID",
    "ANTHROPIC_ORGANIZATION_ID",
    "ANTHROPIC_IDENTITY_TOKEN_FILE",
    "ANTHROPIC_IDENTITY_TOKEN",
)

# Variables Claude Code leaves in its child processes and in IDE terminals.
INHERITED_EXACT = ("CLAUDECODE", "CLAUDE_AGENT_SDK_VERSION", "CLAUDE_EFFORT", "CLAUDE_PID")
INHERITED_PREFIX = "CLAUDE_CODE_"


@dataclass
class EnvironmentReport:
    """A report of the environment check: removed credentials, key and browser flags."""

    removed_credentials: list[str] = field(default_factory=list)
    other_removed_sources: list[str] = field(default_factory=list)
    inherited_removed: list[str] = field(default_factory=list)
    long_token_in_env_file: bool = False
    real_browser: bool = True
    api_key: bool = False
    # The ANTHROPIC_API_KEY key, collected for the keychain (never logged or shown).
    collected_key: str | None = field(default=None, repr=False)
    # The key was in .env: it is already in the keychain, but it should be deleted from there.
    key_in_env_file: bool = False

    @property
    def had_credentials(self) -> bool:
        return bool(self.removed_credentials or self.other_removed_sources)

    def notices(self) -> list[str]:
        """Messages for the user. Only variable names, never values."""
        notices: list[str] = []
        if self.collected_key is not None:
            notices.append(
                "ANTHROPIC_API_KEY was set in the environment: I saved it in the keychain and "
                "removed it from the environment, so the agent's sessions never see it. You can "
                "delete it from your terminal profile or from launchd."
            )
        if self.key_in_env_file:
            notices.append(
                "ANTHROPIC_API_KEY is in .env: I saved it in the keychain, which is where the "
                "agent takes it from. You can delete it from .env."
            )
        if self.removed_credentials:
            notices.append(
                "There were credentials in the environment that would have taken priority over "
                f"your subscription, and I removed them: {', '.join(self.removed_credentials)}. "
                "It's best to delete them from your terminal profile or from launchd."
            )
        if self.other_removed_sources:
            notices.append(
                "I removed variables that sent authentication away from your subscription: "
                f"{', '.join(self.other_removed_sources)}."
            )
        if self.long_token_in_env_file and self.api_key:
            notices.append(
                "There is a CLAUDE_CODE_OAUTH_TOKEN in .env. With the API key it is not used; "
                "you can delete it."
            )
        elif self.long_token_in_env_file and self.real_browser:
            notices.append(
                "There is a CLAUDE_CODE_OAUTH_TOKEN in .env. With the real browser turned on it is "
                "not used (it would break the Chrome integration); you can delete it."
            )
        return notices


def clean_environment(
    real_browser: bool,
    long_token_in_env_file: bool = False,
    environment: dict[str, str] | None = None,
    api_key: bool = False,
) -> EnvironmentReport:
    """Removes inherited credentials and variables from the environment.

    With `api_key`, ANTHROPIC_API_KEY is removed too, but its value stays in the report to be
    saved in the keychain. `environment` allows testing the function without touching os.environ.
    """
    env = os.environ if environment is None else environment
    report = EnvironmentReport(
        long_token_in_env_file=long_token_in_env_file,
        real_browser=real_browser,
        api_key=api_key,
    )

    if api_key and (env.get(API_KEY) or "").strip():
        report.collected_key = env.pop(API_KEY).strip()

    remove_credentials = list(CREDENTIALS_ALWAYS)
    if real_browser or api_key:
        remove_credentials.append(CREDENTIAL_LONG_TOKEN)

    for name in remove_credentials:
        if name in env:
            del env[name]
            report.removed_credentials.append(name)

    for name in OTHER_SOURCES:
        if name in env:
            del env[name]
            report.other_removed_sources.append(name)

    for name in list(env):
        if name == CREDENTIAL_LONG_TOKEN:
            continue  # only reaches here with /login and the real browser disabled
        if name in INHERITED_EXACT or name.startswith(INHERITED_PREFIX):
            del env[name]
            report.inherited_removed.append(name)

    return report


def present_credentials(
    real_browser: bool, environment: dict[str, str] | None = None, api_key: bool = False
) -> list[str]:
    """Follow-up check: lists the credentials still in the environment. With the API key there
    can be none either: the key goes through `apiKeyHelper`, not the environment."""
    env = os.environ if environment is None else environment
    names = list(CREDENTIALS_ALWAYS) + list(OTHER_SOURCES)
    if real_browser or api_key:
        names.append(CREDENTIAL_LONG_TOKEN)
    return [n for n in names if n in env]
