"""Short model calls with validated JSON output and no tools.

Like triage: a separate session, without tools or project settings and without extended reasoning
(with Sonnet, this uses far less output). They are used by email classification and document
reading. What they spend stays in the usage log with its origin (and with the task, if there is
one).

If the model needs to see an image (a document's photo), it goes inside the message, in
base64: this way it needs no tool to read it.

With `raises=True`, instead of None the call raises NoReply with the reason: the image could not be
shown, the plan limit was reached (with its reset) or the model failed.

With `max_output`, the model's response does not exceed those tokens
(`CLAUDE_CODE_MAX_OUTPUT_TOKENS`, checked in the CLI bundled with the SDK): a meal photo has a small
cap.
"""

from __future__ import annotations

import base64
import logging
from datetime import datetime
from pathlib import Path
from typing import Any

from claude_agent_sdk import ClaudeAgentOptions

from aide.core.base_session import sdk_environment, sdk_settings
from aide.core.context import SessionContext

log = logging.getLogger(__name__)

IMAGE_KINDS = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".webp": "image/webp",
    ".gif": "image/gif",
}
MAX_IMAGE = 5 * 1024 * 1024  # the API limit per image


class NoReply(Exception):
    """Why a call gave no response: “image” (could not be shown to the model), “limit” (the plan's;
    `kind` is the window and `reset`, when it returns) or “model” (failed or gave no valid output)."""

    def __init__(
        self, reason: str, detail: str = "", kind: str = "", reset: datetime | None = None
    ) -> None:
        super().__init__(detail or reason)
        self.reason = reason
        self.detail = detail
        self.kind = kind
        self.reset = reset


def image_block(path: Path) -> dict[str, Any]:
    """An image as a message block. ValueError if the format or size is not valid."""
    kind = IMAGE_KINDS.get(path.suffix.lower())
    if kind is None:
        raise ValueError(f"image format not supported: {path.suffix or path.name}")
    data = path.read_bytes()
    if len(data) > MAX_IMAGE:
        raise ValueError("the image is larger than 5 MB")
    return {
        "type": "image",
        "source": {"type": "base64", "type_mean": kind, "data": base64.b64encode(data).decode()},
    }


def options(
    core: Any,
    *,
    role: str,
    system: str,
    schema: dict[str, Any],
    max_turns: int = 3,
    max_budget_usd: float = 1.0,
    save_session: bool = True,
    max_output: int | None = None,
) -> ClaudeAgentOptions:
    """Builds the ClaudeAgentOptions for a role call with its limits and schema."""
    config = core.config
    model = config.role(role)
    extra = {"CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output)} if max_output else None
    return ClaudeAgentOptions(
        model=model.id,
        effort=model.effort,
        thinking={"type": "disabled"},
        system_prompt=system,
        cwd=str(config.paths.agent),
        setting_sources=[],
        settings=sdk_settings(config),
        tools=[],
        permission_mode="default",
        env=sdk_environment(config, extra),
        max_turns=max_turns,
        max_budget_usd=max_budget_usd,
        output_format={"type": "json_schema", "schema": schema},
        # Without saving the conversation in ~/.claude/projects: what it reads does not stay
        # on disk (the user's documents). The CLI respects this with the SDK too
        # (checked).
        extra_args={} if save_session else {"no-session-persistence": None},
    )


async def structured(
    core: Any,
    *,
    role: str,
    origin: str,
    system: str,
    schema: dict[str, Any],
    prompt: str,
    task_id: int | None = None,
    max_budget_usd: float = 1.0,
    images: list[Path] | None = None,
    save_session: bool = True,
    raises: bool = False,
    max_output: int | None = None,
) -> dict[str, Any] | None:
    """The JSON the model returns, or None if the call fails or gives no valid output (with
    `raises`, NoReply with the reason)."""
    from aide.core.orchestrator import AgentSession

    content: str | list[dict[str, Any]] = prompt
    if images:
        try:
            content = [*(image_block(r) for r in images), {"type": "text", "text": prompt}]
        except (OSError, ValueError) as e:
            log.warning("cannot show the image to the model (%s): %s", origin, e)
            if raises:
                raise NoReply("image", str(e)) from e
            return None
    sub = SessionContext(core=core, task_id=task_id, folder=None, role=role, phase=origin)
    try:
        async with AgentSession(
            sub,
            options(
                core,
                role=role,
                system=system,
                schema=schema,
                max_budget_usd=max_budget_usd,
                save_session=save_session,
                max_output=max_output,
            ),
            origin,
        ) as session:
            result = await session.turn(content)
    except Exception as e:
        log.exception("a structured call failed (%s)", origin)
        if raises:
            raise await _no_reply(core, type(e).__name__) from e
        return None
    if result.kind == "ok" and isinstance(result.structured, dict):
        return result.structured
    log.warning("the %s call did not return valid output (%s)", origin, result.kind)
    if not raises:
        return None
    if result.kind in ("limit", "opus_limit"):
        raise NoReply("limit", result.detail, result.detail, result.reset)
    raise await _no_reply(core, result.kind)


async def _no_reply(core: Any, detail: str) -> NoReply:
    """The reason for a failed call: the plan limit if the last limits data is a standing
    rejection (the session may drop without saying so), and otherwise, the model."""
    try:
        limit = await core.usage.reached_limit()
    except Exception:
        log.exception("could not check the plan limits")
        limit = None
    if limit is not None:
        return NoReply("limit", detail, limit[0], limit[1])
    return NoReply("model", detail)
