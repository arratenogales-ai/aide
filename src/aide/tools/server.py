"""In-process MCP server "aide" with the agent's own tools.

Each tool module provides a factory `(SessionContext) -> list[SdkMcpTool]`, so the tools
know the task and the folder of the session that uses them. Read-only ones carry
`readOnlyHint` so they can run in parallel.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from claude_agent_sdk import SdkMcpTool, create_sdk_mcp_server

from aide.core.context import SessionContext
from aide.core.policy import OWN_READ

log = logging.getLogger(__name__)

Factory = Callable[[SessionContext], list[SdkMcpTool[Any]]]
_FACTORIES: list[Factory] = []


def log_factory(factory: Factory) -> Factory:
    """Registers a tool factory once and returns it."""
    if factory not in _FACTORIES:
        _FACTORIES.append(factory)
    return factory


def text(content: Any) -> dict[str, Any]:
    """Wraps content as a text result for a tool, converting non-text to JSON."""
    if not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False, indent=1, default=str)
    return {"content": [{"type": "text", "text": content}]}


def error(message: str) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": message}], "is_error": True}


def tools_of(ses: SessionContext, only_read: bool = False) -> list[SdkMcpTool[Any]]:
    """Creates the tools for a session from all registered factories."""
    _load_modules()
    tools: list[SdkMcpTool[Any]] = []
    for factory in _FACTORIES:
        try:
            tools += factory(ses)
        except Exception:
            log.exception("could not create the tools from %s", factory.__module__)
    if only_read:
        tools = [h for h in tools if h.name in OWN_READ]
    return tools


def build_server(ses: SessionContext, only_read: bool = False) -> Any:
    return create_sdk_mcp_server("aide", "0.1.0", tools_of(ses, only_read))


def _load_modules() -> None:
    # Importing the modules registers their factories.
    from aide.tools import history, telegram  # noqa: F401

    for name in (
        "google_gmail",
        "google_calendar",
        "memory",
        "browser",
        "scheduler",
        "documents",
        "paperwork",
    ):
        try:
            __import__(f"aide.tools.{name}")
        except ModuleNotFoundError as e:
            if e.name and e.name.startswith("aide"):
                continue
            raise
