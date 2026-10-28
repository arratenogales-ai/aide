"""Simulated SDK to test the core without calling the model.

`FakeClient` imitates ClaudeSDKClient: each query runs a script of steps. Tool steps go
through the PreToolUse and PostToolUse hooks and through can_use_tool exactly as Claude
Code would, so the policy, the approvals and the timeline are tested.
"""

from __future__ import annotations

import asyncio
import inspect
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    PermissionResultAllow,
    RateLimitEvent,
    RateLimitInfo,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolPermissionContext,
    ToolUseBlock,
)


@dataclass
class Tool:
    name: str
    entry: dict[str, Any]
    # Effect if allowed (for example, writing the file). Returns the result text (or a coroutine
    # that returns it, like the own tools of `own`).
    effect: Callable[[dict[str, Any]], Any] | None = None
    agent: str | None = None
    # Identifier of the call, if the test needs it (that of a delegation, so that the subagent's
    # replies carry it as `parent_tool_use_id`).
    id: str | None = None


@dataclass
class Text:
    text: str


@dataclass
class End:
    """Result of the turn."""

    text: str = ""
    subtype: str = "success"
    is_error: bool = False
    cost: float = 0.01
    turns: int = 2
    model: str = "claude-haiku-5-5"
    error_assistant: str | None = None
    limit: dict[str, Any] | None = None
    structured: Any = None


@dataclass
class Wait:
    seconds: float


@dataclass
class Background:
    """Background work (Bash command or subagent). It notifies at the start and, `seconds`
    after the turn ends, notifies that it finished; if there is a `continuation`, the agent
    goes back to work with those steps, as Claude Code does on receiving the notification."""

    id: str
    kind: str = "local_bash"
    seconds: float = 0.2
    continuation: list[Any] = field(default_factory=list)
    notifies: bool = True


@dataclass
class Usage:
    """A model response with its usage, like those the session sends during the turn: repeated
    for each block with the same identifier and, if from a subagent, with
    `parent_tool_use_id` (verified with real sessions)."""

    tokens: int
    parent: str | None = None
    blocks: int = 2


Step = Tool | Text | End | Wait | Background | Usage


@dataclass
class Recorder:
    queries: list[str] = field(default_factory=list)
    blocks: list[list[dict[str, Any]]] = field(default_factory=list)
    options: list[Any] = field(default_factory=list)
    tools_results: list[tuple[str, str]] = field(default_factory=list)
    models: list[str] = field(default_factory=list)
    # What a script raised (a failing assert, for example): the turn ends with an error instead
    # of waiting forever, and conftest fails the test.
    script_errors: list[str] = field(default_factory=list)


class FakeClient:
    """Configured with `FakeClient.guion = funcion(prompt, opciones, registro) -> list[Paso]`."""

    script: Callable[[str, Any, Recorder], list[Step]] = staticmethod(
        lambda p, o, r: [End("<delivery>ok</delivery>")]
    )
    registry = Recorder()
    # Accumulated per session: when resuming it, the real CLI starts from it.
    accumulated: dict[str, tuple[float, int, int, int]] = {}

    def __init__(self, options: Any) -> None:
        self.options = options
        self.queue: asyncio.Queue[Any] = asyncio.Queue()
        self.session_id = options.resume or str(uuid.uuid4())
        self.accumulated_cost = 0.0
        self.entry = 0  # accumulated input and output tokens (100 and 50 per result)
        self.output = 0
        self.model = options.model
        self._tasks: list[asyncio.Task] = []
        self._pending: list[str] = []
        self._backgrounds: list[Background] = []
        self._working = False
        self.tokens_usage = (
            0  # what the Usage steps add up to, in the accumulated usage of the results
        )
        if options.resume in type(self).accumulated:
            (
                self.accumulated_cost,
                self.tokens_usage,
                self.entry,
                self.output,
            ) = type(self).accumulated[options.resume]
        type(self).registry.options.append(options)

    async def connect(self) -> None:
        await self.queue.put(
            SystemMessage("init", {"session_id": self.session_id, "model": self.model, "tools": []})
        )

    async def query(self, prompt: Any) -> None:
        if not isinstance(prompt, str):
            # A message with blocks (text and images): the script sees the text and one
            # marker per image, and the log keeps the blocks.
            blocks = [b async for m in prompt for b in m["message"]["content"]]
            type(self).registry.blocks.append(blocks)
            prompt = "\n".join(
                b["text"] if b["type"] == "text" else f"[imagen {b['source']['type_mean']}]"
                for b in blocks
            )
        type(self).registry.queries.append(prompt)
        if self._working:
            # Like the real SDK: a message in the middle of a turn is incorporated into
            # the turn in progress.
            self._pending.append(prompt)
            return
        self._tasks.append(asyncio.create_task(self._run(prompt)))

    async def _run(self, prompt: str) -> None:
        self._working = True
        await self.queue.put(SystemMessage("session_state_changed", {"state": "running"}))
        try:
            steps = type(self).script(prompt, self.options, type(self).registry)
        except Exception as e:
            type(self).registry.script_errors.append(f"{type(e).__name__}: {e}")
            steps = [
                End(f"the script failed: {e!r}", subtype="error_during_execution", is_error=True)
            ]
        await self._steps(steps)
        self._working = False
        await self.queue.put(SystemMessage("session_state_changed", {"state": "idle"}))
        for background in self._backgrounds:
            self._tasks.append(asyncio.create_task(self._notify(background)))
        self._backgrounds = []

    async def _notify(self, background: Background) -> None:
        await asyncio.sleep(background.seconds)
        if not background.notifies:
            return
        await self.queue.put(
            SystemMessage(
                "task_notification",
                {"task_id": background.id, "status": "completed", "summary": "terminado"},
            )
        )
        if background.continuation:
            # Like Claude Code: resumes the turn with the notification without emitting
            # `running` or `idle`.
            self._working = True
            await self._steps(background.continuation)
            self._working = False

    async def _steps(self, steps: list[Any]) -> None:
        for step in steps:
            if isinstance(step, Background):
                self._backgrounds.append(step)
                await self.queue.put(
                    SystemMessage(
                        "task_started",
                        {
                            "task_id": step.id,
                            "task_type": step.kind,
                            "description": "in the background",
                        },
                    )
                )
            elif isinstance(step, Wait):
                await asyncio.sleep(step.seconds)
            elif isinstance(step, Text):
                await self.queue.put(AssistantMessage([TextBlock(step.text)], self.model))
            elif isinstance(step, Usage):
                ident = f"msg_{uuid.uuid4().hex[:12]}"
                usage = {
                    "input_tokens": 10,
                    "cache_creation_input_tokens": 0,
                    "cache_read_input_tokens": step.tokens - 11,
                    "output_tokens": 1,
                }
                self.tokens_usage += step.tokens
                for _ in range(step.blocks):
                    await self.queue.put(
                        AssistantMessage(
                            [TextBlock("…")],
                            self.model,
                            parent_tool_use_id=step.parent,
                            usage=usage,
                            message_id=ident,
                        )
                    )
            elif isinstance(step, Tool):
                await self._tool(step)
            elif isinstance(step, End):
                await self._end(step)

    async def _tool(self, step: Tool) -> None:
        usage_id = step.id or f"toolu_{uuid.uuid4().hex[:10]}"
        await self.queue.put(
            AssistantMessage([ToolUseBlock(usage_id, step.name, step.entry)], self.model)
        )
        hook_entry = {
            "hook_event_name": "PreToolUse",
            "tool_name": step.name,
            "tool_input": step.entry,
            "tool_use_id": usage_id,
            "session_id": self.session_id,
            "cwd": self.options.cwd,
        }
        if step.agent:
            hook_entry["agent_type"] = step.agent
            hook_entry["agent_id"] = "agente-1"
        decision = None
        reason = ""
        for matcher in (self.options.hooks or {}).get("PreToolUse", []):
            for callback in matcher.hooks:
                output = await callback(hook_entry, usage_id, {"signal": None})
                specific = (output or {}).get("hookSpecificOutput") or {}
                if specific.get("permissionDecision"):
                    decision = specific["permissionDecision"]
                    reason = specific.get("permissionDecisionReason", "")
        entry = step.entry
        if decision == "deny":
            type(self).registry.tools_results.append((step.name, f"DENEGADA: {reason}"))
            return
        if decision in ("ask", None) or step.name == "AskUserQuestion":
            if self.options.can_use_tool is None:
                type(self).registry.tools_results.append((step.name, "DENEGADA: sin callback"))
                return
            ctx = ToolPermissionContext(
                tool_use_id=usage_id,
                agent_id="agente-1" if step.agent else None,
                decision_reason=reason,
            )
            result = await self.options.can_use_tool(step.name, entry, ctx)
            if not isinstance(result, PermissionResultAllow):
                type(self).registry.tools_results.append(
                    (step.name, f"DENEGADA: {getattr(result, 'message', '')}")
                )
                return
            entry = result.updated_input or entry
        output = step.effect(entry) if step.effect else "ok"
        if inspect.isawaitable(output):
            output = await output
        type(self).registry.tools_results.append((step.name, output))
        post = {
            "hook_event_name": "PostToolUse",
            "tool_name": step.name,
            "tool_input": entry,
            "tool_response": output,
            "tool_use_id": usage_id,
            "session_id": self.session_id,
            "cwd": self.options.cwd,
        }
        if step.agent:
            post["agent_type"] = step.agent
        for matcher in (self.options.hooks or {}).get("PostToolUse", []):
            for callback in matcher.hooks:
                returned = await callback(post, usage_id, {"signal": None})
                specific = (returned or {}).get("hookSpecificOutput") or {}
                if step.name.startswith("mcp__") and "updatedMCPToolOutput" in specific:
                    # Like Claude Code (verified on 10-22-2026): the model sees
                    # the changed output.
                    type(self).registry.tools_results[-1] = (
                        step.name,
                        specific["updatedMCPToolOutput"],
                    )

    async def _end(self, step: End) -> None:
        # Messages injected in the middle of a turn end up incorporated into this turn.
        text = step.text
        if self._pending:
            text = text + "\n[merged in: " + " | ".join(self._pending) + "]"
            self._pending.clear()
        if step.limit:
            info = RateLimitInfo(**step.limit)
            await self.queue.put(RateLimitEvent(info, uuid.uuid4().hex, self.session_id))
        if step.error_assistant:
            await self.queue.put(
                AssistantMessage([TextBlock(text)], self.model, error=step.error_assistant)
            )
        elif text:
            await self.queue.put(AssistantMessage([TextBlock(text)], self.model))
        self.accumulated_cost += step.cost
        self.entry += 100
        self.output += 50
        self._save_accumulated()
        type(self).registry.models.append(self.model)
        await self.queue.put(
            ResultMessage(
                subtype=step.subtype,
                duration_ms=10,
                duration_api_ms=10,
                is_error=step.is_error,
                num_turns=step.turns,
                session_id=self.session_id,
                total_cost_usd=self.accumulated_cost,
                result=text if step.subtype == "success" else None,
                structured_output=step.structured,
                model_usage=self._accumulated_usage(),
            )
        )

    def _save_accumulated(self) -> None:
        type(self).accumulated[self.session_id] = (
            self.accumulated_cost,
            self.tokens_usage,
            self.entry,
            self.output,
        )

    def _accumulated_usage(self) -> dict[str, Any]:
        """Accumulated usage of the session, like the CLI's `model_usage`."""
        return {
            self.model: {
                "inputTokens": self.entry,
                "outputTokens": self.output,
                "cacheReadInputTokens": self.tokens_usage,
                "cacheCreationInputTokens": 0,
                "webSearchRequests": 0,
                "costUSD": self.accumulated_cost,
            }
        }

    async def receive_messages(self):
        while True:
            yield await self.queue.get()

    async def interrupt(self) -> None:
        if not self._working:
            return  # like the CLI: with no turn in progress there is nothing to interrupt
        for t in self._tasks:
            t.cancel()
        self._working = False
        self._save_accumulated()
        await self.queue.put(
            ResultMessage(
                "success",
                1,
                1,
                False,
                0,
                self.session_id,
                terminal_reason="aborted_tools",
                total_cost_usd=self.accumulated_cost,
                model_usage=self._accumulated_usage() if self.tokens_usage else None,
            )
        )
        await self.queue.put(SystemMessage("session_state_changed", {"state": "idle"}))

    async def set_model(self, model: str | None = None) -> None:
        self.model = model or self.model

    async def disconnect(self) -> None:
        for t in self._tasks:
            t.cancel()


def write(path: Path, content: str = "hello") -> Callable[[dict[str, Any]], str]:
    def effect(entry: dict[str, Any]) -> str:
        destination = Path(entry.get("file_path", path))
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(entry.get("content", content), encoding="utf-8")
        return f"File created successfully at: {destination}"

    return effect


# --- real own tools ------------------------------------------------------------------------

# Tools of the "aide" server created by the last session with full permissions.
OWN: dict[str, Any] = {}


def install_own(monkeypatch: Any) -> None:
    """Keeps the own tools each session creates so they can be actually run."""
    from aide.tools import server

    original = server.tools_of

    def wrapper(ses: Any, only_read: bool = False) -> list[Any]:
        tools = original(ses, only_read)
        if not only_read:
            OWN.update({h.name: h for h in tools})
        return tools

    OWN.clear()
    monkeypatch.setattr(server, "tools_of", wrapper)


def own(name: str, entry: dict[str, Any]) -> Tool:
    """Step that runs the own tool `name` (without the mcp__aide__ prefix)."""

    async def effect(final_entry: dict[str, Any]) -> str:
        result = await OWN[name].handler(final_entry)
        text = "\n".join(c.get("text", "") for c in result.get("content", []))
        return ("ERROR: " if result.get("is_error") else "") + text

    return Tool(f"mcp__aide__{name}", entry, effect)
