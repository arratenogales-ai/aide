"""SDK options for each role (orchestrator, quick reply, review, triage)."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import ClaudeAgentOptions

from aide.common.config import Config, ModelRole
from aide.core.base_session import sdk_environment, sdk_settings
from aide.core.context import SessionContext
from aide.core.hooks import create_hooks
from aide.core.permissions import create_can_use_tool
from aide.core.policy import glob_no_uppercase
from aide.core.prompts import (
    ORCHESTRATOR_SYSTEM,
    QUICK_SYSTEM,
    REVIEW_SYSTEM,
    TRIAGE_SYSTEM,
    user_presentation,
)
from aide.core.subagents import subagents_definitions

log = logging.getLogger(__name__)

ORCHESTRATOR_TOOLS = [
    "Read",
    "Write",
    "Edit",
    "Bash",
    "Glob",
    "Grep",
    "WebSearch",
    "WebFetch",
    "Agent",
    "Skill",
    "TaskCreate",
    "TaskUpdate",
    "TaskGet",
    "TaskList",
    "AskUserQuestion",
    "ToolSearch",
]
QUICK_TOOLS = ["Read", "Glob", "Grep", "WebSearch", "WebFetch", "ToolSearch"]
REVIEW_TOOLS = ["Read", "Glob", "Grep", "WebFetch", "Bash", "ToolSearch"]

SECRET_PATHS = [
    "~/.ssh",
    "~/.aws",
    "~/.gnupg",
    "~/.config/gcloud",
    "~/.kube",
    "~/.netrc",
    "~/.git-credentials",
    "~/Library/Keychains",
    "~/Library/Cookies",
    "~/Library/Mail",
    "~/Library/Messages",
    "~/Library/Application Support/Google/Chrome",
    "~/Library/Safari",
]


# What is forbidden and may be in any folder of agent/ (a project's .env, a key or a token): the
# policy denies it to Read and Grep, but a Bash command that walks the whole folder (grep -r, rg
# -uu, a script) is only stopped by the sandbox. They go as globs, which the CLI converts
# into macOS sandbox expressions, with each letter as [xX]. .pem files are not included: a project's
# scripts may need the cacert.pem of its .venv to use HTTPS.
SECRETS_IN_AGENT = [
    "**/.env",
    "**/.env.*",
    "**/*.key",
    "**/*.p12",
    "**/id_rsa*",
    "**/id_ed25519*",
    "**/id_ecdsa*",
    "**/credentials.json",
    "**/token.json",
]


def sandbox_settings(config: Config) -> dict[str, Any] | None:
    """Bash sandbox. Commands only write to agent/ and to temp files."""
    s = config.settings.sandbox
    if not s.enabled:
        return None
    root = config.paths.root
    denied = [str(Path(p).expanduser()) for p in SECRET_PATHS] + [
        str(root / ".env"),
        str(root / "config"),
        str(root / "data"),
        str(config.paths.logs),
        str(root / "src"),
        # The weekly backups: the database and your documents, also as zip.
        str(config.paths.backups),
        # The documents inbox: only the code reads it.
        str(config.paths.inbox),
    ]
    agent = str(config.paths.agent.resolve())
    denied += [f"{agent}/{glob_no_uppercase(glob)}" for glob in SECRETS_IN_AGENT]
    return {
        "enabled": True,
        "autoAllowBashIfSandboxed": True,
        "allowUnsandboxedCommands": False,
        "network": {"allowLocalBinding": True, "allowedDomains": list(s.allowed_domains)},
        "filesystem": {"denyRead": denied, "denyWrite": denied_write(config)},
    }


def denied_write(config: Config) -> list[str]:
    """What no Bash command writes, even inside agent/ and even if done by a
    script the policy can't analyze. Write and Edit don't go through the
    sandbox: they remain under the policy, which asks for approval for a new skill."""
    agent = config.paths.agent
    return [
        str(agent / "profile.md"),
        str(agent / "memory.md"),
        str(agent / "CLAUDE.md"),
        str(agent / ".claude"),
    ]


def _mcp_servers(ses: SessionContext, only_read: bool = False) -> dict[str, Any]:
    from aide.tools.server import build_server

    servers: dict[str, Any] = {"aide": build_server(ses, only_read)}
    if not only_read:
        for name, data in ses.core.config.settings.mcp_servers.items():
            servers[name] = dict(data.config)
    return servers


def _log_stderr(task_id: int | None) -> Any:
    def record(line: str) -> None:
        log.debug("cli[%s] %s", task_id, line.rstrip())

    return record


def orchestrator_options(
    ses: SessionContext,
    model: ModelRole,
    *,
    resume: str | None,
    max_turns: int,
    max_budget_usd: float,
    opus_fallback: bool = False,
) -> ClaudeAgentOptions:
    """Builds the agent options for the main orchestrator session."""
    config = ses.core.config
    tools = list(ORCHESTRATOR_TOOLS)
    return ClaudeAgentOptions(
        model=model.id,
        effort=model.effort,
        system_prompt=ORCHESTRATOR_SYSTEM + user_presentation(config.user),
        cwd=str(config.paths.agent),
        setting_sources=["project"],
        settings=sdk_settings(config),
        skills=config.paths.available_skills(),
        tools=tools,
        mcp_servers=_mcp_servers(ses),
        agents=subagents_definitions(config, chrome=ses.chrome, model_fallback_opus=opus_fallback),
        hooks=create_hooks(ses),
        can_use_tool=create_can_use_tool(ses),
        permission_mode="default",
        env=sdk_environment(config, {"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"}),
        max_turns=max(1, max_turns),
        max_budget_usd=max(0.01, round(max_budget_usd, 4)),
        resume=resume,
        sandbox=sandbox_settings(config),
        extra_args={"chrome": None} if ses.chrome else {},
        stderr=_log_stderr(ses.task_id),
    )


def quick_options(
    ses: SessionContext,
    model: ModelRole,
    *,
    resume: str | None,
    max_turns: int,
    max_budget_usd: float,
) -> ClaudeAgentOptions:
    """Builds the agent options for a quick session, with the current date and time."""
    config = ses.core.config
    zone = config.time_zone
    today = datetime.now(ZoneInfo(zone)).strftime("%A, %B %-d, %Y, %H:%M")
    return ClaudeAgentOptions(
        model=model.id,
        effort=model.effort,
        system_prompt=QUICK_SYSTEM.format(zone=zone, today=today) + user_presentation(config.user),
        cwd=str(config.paths.agent),
        setting_sources=["project"],
        settings=sdk_settings(config),
        skills=[],
        tools=QUICK_TOOLS,
        mcp_servers=_mcp_servers(ses, only_read=True),
        hooks=create_hooks(ses),
        can_use_tool=create_can_use_tool(ses),
        permission_mode="default",
        env=sdk_environment(config, {"CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1"}),
        max_turns=max(1, max_turns),
        max_budget_usd=max(0.01, max_budget_usd),
        resume=resume,
        stderr=_log_stderr(ses.task_id),
    )


def review_options(
    ses: SessionContext,
    model: ModelRole,
    *,
    schema: dict[str, Any],
    max_turns: int,
    max_budget_usd: float,
) -> ClaudeAgentOptions:
    """Builds the agent options for a review session with structured output."""
    config = ses.core.config
    return ClaudeAgentOptions(
        model=model.id,
        effort=model.effort,
        system_prompt=REVIEW_SYSTEM,
        cwd=str(config.paths.agent),
        setting_sources=["project"],
        settings=sdk_settings(config),
        skills=[],
        tools=REVIEW_TOOLS,
        mcp_servers=_mcp_servers(ses, only_read=True),
        hooks=create_hooks(ses),
        can_use_tool=create_can_use_tool(ses),
        permission_mode="default",
        env=sdk_environment(config),
        max_turns=max(1, max_turns),
        max_budget_usd=max(0.01, max_budget_usd),
        output_format={"type": "json_schema", "schema": schema},
        sandbox=sandbox_settings(config),
        stderr=_log_stderr(ses.task_id),
    )


def triage_options(
    config: Config, model: ModelRole, schema: dict[str, Any], max_budget_usd: float
) -> ClaudeAgentOptions:
    """Builds the agent options for classifying a message, with no tools and few turns."""
    return ClaudeAgentOptions(
        model=model.id,
        effort=model.effort,
        system_prompt=TRIAGE_SYSTEM,
        cwd=str(config.paths.agent),
        setting_sources=[],
        settings=sdk_settings(config),
        tools=[],
        permission_mode="default",
        env=sdk_environment(config),
        max_turns=3,
        max_budget_usd=max(0.01, max_budget_usd),
        output_format={"type": "json_schema", "schema": schema},
    )
