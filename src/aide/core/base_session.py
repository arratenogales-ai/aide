"""Pieces common to all SDK sessions: environment and inline settings.

Everything here was verified with real sessions.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

from aide.common.api_key import helper_order
from aide.common.config import Config

# Environment variables the program sets in every session (merged with os.environ, which was already
# cleaned at startup). Without the CLI's non-essential traffic, and with it without the hidden call
# that titles each session: it reads the whole first message with Haiku, and in sessions opened in
# batches that is repeated in each one.
NO_TRAFFIC_EXTRA = {"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}

FIXED_ENVIRONMENT = {
    # Every Bash command starts in agent/, as the policy assumes.
    "CLAUDE_BASH_MAINTAIN_PROJECT_WORKING_DIR": "1",
    # Subagents do not launch others and there are at most three at a time.
    "CLAUDE_CODE_MAX_SUBAGENT_SPAWN_DEPTH": "1",
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS": "3",
    # TaskCreate and TaskUpdate do not come by default in 5.x models.
    "CLAUDE_CODE_ENABLE_TODO_TOOLS": "1",
    "CLAUDE_CODE_ENABLE_TASKS": "1",
    # Only the subagents defined by the program.
    "CLAUDE_AGENT_SDK_DISABLE_BUILTIN_AGENTS": "1",
    # The agent's memory is memory.md with the user's approval.
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
    "CLAUDE_CODE_DISABLE_GIT_INSTRUCTIONS": "1",
    # Gmail and Calendar use their own tools, never claude.ai connectors.
    "ENABLE_CLAUDEAI_MCP_SERVERS": "false",
    # Unattended sessions: wait on transient capacity errors.
    "CLAUDE_CODE_RETRY_WATCHDOG": "1",
    "CLAUDE_AGENT_SDK_CLIENT_APP": "aide/0.1",
    # Session state (running/idle) to know when a turn truly ends.
    "CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS": "1",
    # Bash output trimmed to 10,000 characters (the CLI's is 30,000): each command stays in the
    # context and is reread at every step.
    "BASH_MAX_OUTPUT_LENGTH": "10000",
    "DISABLE_AUTOUPDATER": "1",
    **NO_TRAFFIC_EXTRA,
}


def sdk_environment(config: Config, extra: dict[str, str] | None = None) -> dict[str, str]:
    """Builds the environment variables for the SDK, with caches inside agent/."""
    env = dict(FIXED_ENVIRONMENT)
    cache = config.paths.agent_cache
    # The sandbox only allows writing inside agent/: package caches go there.
    env.update(
        {
            "UV_CACHE_DIR": str(cache / "uv"),
            "PIP_CACHE_DIR": str(cache / "pip"),
            "npm_config_cache": str(cache / "npm"),
            "PLAYWRIGHT_BROWSERS_PATH": str(Path.home() / "Library/Caches/ms-playwright"),
            "MPLCONFIGDIR": str(cache / "matplotlib"),
            # `python` in the agent's commands is the project's, with the document
            # libraries (python-docx, openpyxl, python-pptx, reportlab, pypdf)
            # installed.
            "PATH": os.pathsep.join(
                [str(Path(sys.executable).parent), os.environ.get("PATH", "/usr/bin:/bin")]
            ),
        }
    )
    # Alternative credential: only with /login and the real browser disabled. With
    # the API key no credential goes in the environment: `apiKeyHelper` provides it.
    token = config.environment.CLAUDE_CODE_OAUTH_TOKEN
    if not config.uses_api_key and not config.real_browser and token is not None:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = token.get_secret_value()
    if extra:
        env.update(extra)
    return env


# Instructions Claude Code loads only when reading a file in its folder (memory documentation:
# CLAUDE.md, CLAUDE.local.md, AGENTS.md and .claude/rules of subfolders).
SUBFOLDERS_INSTRUCTIONS = (
    "**/CLAUDE.md",
    "**/CLAUDE.local.md",
    "**/AGENTS.md",
    "**/.claude/CLAUDE.md",
    "**/.claude/AGENTS.md",
    "**/.claude/rules/**",
)


def excluded_claude_md(root: Path) -> list[str]:
    """CLAUDE.md of the project root and all parent directories, and the instructions of any
    subfolder of the projects and of the task folders.

    Projects (they may carry an old CLAUDE.md from another tool) and whatever the user sends or the
    agent downloads are data, never instructions: without this exclusion, Claude Code would load its
    CLAUDE.md when reading any file in that folder.
    """
    excluded: list[str] = []
    for folder in (root, *root.parents):
        excluded.append(str(folder / "CLAUDE.md"))
        excluded.append(str(folder / "CLAUDE.local.md"))
        excluded.append(str(folder / ".claude" / "CLAUDE.md"))
    agent = root / "agent"
    for base in (agent / "projects", agent / "tasks", agent / "test_tasks"):
        excluded += [f"{base}/{pattern}" for pattern in SUBFOLDERS_INSTRUCTIONS]
    return excluded


def sdk_settings(config: Config, extra: dict[str, Any] | None = None) -> str:
    """Inline settings (--settings flag) common to all sessions."""
    settings: dict[str, Any] = {
        "claudeMdExcludes": excluded_claude_md(config.paths.root),
        "includeGitInstructions": False,
        "attribution": {"commit": "", "pr": ""},
        "disableClaudeAiConnectors": True,
    }
    if extra:
        settings.update(extra)
    if config.uses_api_key:
        # The key comes out of the keychain through this command, which Claude Code launches
        # itself; this way it is not in the environment of the sessions or of their Bash
        # commands (common/api_key.py). It goes after `extra` so nothing changes it.
        settings["apiKeyHelper"] = helper_order()
    return json.dumps(settings, ensure_ascii=False)
