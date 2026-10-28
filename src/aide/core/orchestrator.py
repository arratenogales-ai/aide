"""SDK session per task: turns, mid-turn messages, usage, caps and limits.

A turn ends when the session reports `idle` after a result (with
CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS=1). This covers messages the user injects
mid-turn (the SDK incorporates them into the current turn) and background subagents,
which keep the session in `running` until they finish. Verified with real sessions.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ClaudeSDKClient,
    RateLimitEvent,
    ResultMessage,
    SystemMessage,
    TextBlock,
    ToolUseBlock,
)

from aide.core import navigation
from aide.core.context import SessionContext
from aide.core.db import now
from aide.core.usage import Snapshot, format_tokens

log = logging.getLogger(__name__)

ResultKind = Literal[
    "ok",
    "turn_cap",
    "usd_cap",
    "token_cap",
    "limit",
    "opus_limit",
    "login",
    "browser",
    "error",
    "interrupted",
]

LOGIN_SIGNALS = (
    "failed to authenticate",
    "login expired",
    "please run /login",
    "oauth session expired",
    "oauth token",
)
LIMIT_SIGNALS = ("hit your", "usage limit", "rate limit", "limit reached", "rate_limit")
# Reasons an interrupted turn ends with (ClaudeSDKClient.interrupt).
CUTOFFS = ("aborted_streaming", "aborted_tools")
# Fields of each model response's usage that add up the tokens consumed.
USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


@dataclass
class TurnResult:
    """The result of an agent turn: kind, text, session id, cost and turns."""

    kind: ResultKind
    text: str = ""
    session_id: str | None = None
    detail: str = ""
    reset: datetime | None = None
    cost: float = 0.0
    turns: int = 0
    structured: Any = None


_END = object()  # the session stopped sending messages

# Waits for background work at the end of a turn (seconds).
BASH_BACKGROUND_WAIT = 300
AGENT_BACKGROUND_WAIT = 1800
RESUMPTION_WAIT = 30
IDLE_WAIT = 5
CLOSING_WAIT = 25


async def _message(content: list[dict[str, Any]]) -> AsyncIterator[dict[str, Any]]:
    """A user message with blocks, as the SDK sends them in streaming mode."""
    yield {
        "type": "user",
        "message": {"role": "user", "content": content},
        "parent_tool_use_id": None,
    }


class AgentSession:
    """Wraps a ClaudeSDKClient with a single message reader."""

    # Tests replace the client class with a simulated SDK.
    client_factory: Any = ClaudeSDKClient

    def __init__(self, ses: SessionContext, options: ClaudeAgentOptions, origin: str) -> None:
        self.ses = ses
        self.options = options
        self.origin = origin
        self.client: ClaudeSDKClient | None = None
        self._iterator: Any = None
        self.snapshot = Snapshot()
        self.session_id: str | None = options.resume
        self.in_turn = False
        self._there_is_states = False
        self._limit: Any = None
        self.current_model: str | None = options.model
        self._cutoff: asyncio.Future[None] | None = None  # interruption due to the budget

    async def __aenter__(self) -> AgentSession:
        await self.open_()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def open_(self) -> None:
        """Opens the client connection, restoring earlier usage when resuming a session."""
        if self.options.resume:
            # The CLI resumes with the total the session already had: it starts from
            # that.
            try:
                self.snapshot = await self.ses.core.usage.snapshot_of(self.options.resume)
            except Exception:
                log.exception("could not read the session's earlier usage")
        self.client = type(self).client_factory(self.options)
        await self.client.connect()
        self._iterator = self.client.receive_messages().__aiter__()
        # A single reader dumps messages into a queue: waiting with a cap on the queue can
        # be cancelled without breaking the SDK generator (cancelling its __anext__ would
        # close it).
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._reader = asyncio.create_task(self._read())

    async def _read(self) -> None:
        try:
            async for message in self._iterator:
                await self._queue.put(message)
        except Exception as e:
            log.warning("reading the session ended with an error: %s", e)
        finally:
            await self._queue.put(_END)

    async def close(self) -> None:
        """Closes the session once and without blocking.

        It can be called at the same time from engine shutdown and from the exit of the task's
        `async with`. The SDK's close protects its cleanup against cancellation, so a
        `wait_for` isn't enough to bound it: it is launched in its own task and, if it takes too long,
        it is left to finish on its own instead of waiting for it.
        """
        client, self.client = self.client, None
        if client is not None:
            closing = asyncio.ensure_future(client.disconnect())
            done, _ = await asyncio.wait({closing}, timeout=CLOSING_WAIT)
            if done:
                with contextlib.suppress(BaseException):
                    closing.result()
            else:
                log.warning(
                    "the session did not finish closing in %s s; moving on without waiting",
                    CLOSING_WAIT,
                )
        reader = getattr(self, "_reader", None)
        if reader is not None and not reader.done():
            reader.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.wait({reader}, timeout=5)
        self._iterator = None

    async def turn(self, prompt: str | list[dict[str, Any]]) -> TurnResult:
        """One turn. The prompt is text or, to show images to the model without tools, a
        list of blocks (text and base64 images) sent as a streaming message."""
        if self.client is None:
            raise RuntimeError("the session is not open")
        self.in_turn = True
        try:
            await self.client.query(prompt if isinstance(prompt, str) else _message(prompt))
            return await self._consume()
        finally:
            self.in_turn = False

    async def inject(self, text: str) -> None:
        """User message during execution: the SDK incorporates it into the current turn."""
        if self.client is None:
            raise RuntimeError("the session is not open")
        await self.client.query(text)

    async def interrupt(self) -> None:
        """Interrupts the client, ignoring any error."""
        if self.client is not None:
            with contextlib.suppress(Exception):
                await self.client.interrupt()

    async def change_model(self, model: str) -> None:
        """Switches the connected client to another model."""
        if self.client is not None:
            await self.client.set_model(model)
            self.current_model = model

    async def _next_step(self, wait: float | None) -> Any:
        if wait is None:
            message = await self._queue.get()
        else:
            message = await asyncio.wait_for(self._queue.get(), timeout=wait)
        if message is _END:
            await self._queue.put(_END)  # later turns also see the end
            raise StopAsyncIteration
        return message

    async def _consume(self) -> TurnResult:
        tasks = self.ses.core.tasks
        task_id = self.ses.task_id
        last: ResultMessage | None = None
        errors: list[str] = []
        cost = 0.0
        turns = 0
        running = False
        pending_idle = False  # `idle` can arrive before the result
        # Background work (Bash commands or subagents): while it is still pending, the turn
        # has not finished, because its notification wakes the agent again.
        background: dict[str, str] = {}
        limit_background: float | None = None
        aborted = False
        loop = asyncio.get_running_loop()
        await self._prepare_budget()

        def pending() -> bool:
            """Is there background work the turn must wait for?

            If the agent already closed with <delivery>, the Bash commands still running are
            processes it leaves running on purpose (for example, a local server); only
            subagents are waited for."""
            if not background:
                return False
            if last is not None and "<delivery>" in (last.result or ""):
                return any(t == "local_agent" for t in background.values())
            return True

        def cap_background() -> float:
            # Subagents: they finish on their own. Bash commands: with a cap, because a
            # local server in the background never finishes.
            agents = any(t == "local_agent" for t in background.values())
            return loop.time() + (AGENT_BACKGROUND_WAIT if agents else BASH_BACKGROUND_WAIT)

        while True:
            # With state events it waits for `idle`; without them, a few seconds after
            # the result.
            if limit_background is not None:
                wait: float | None = max(0.1, limit_background - loop.time())
            else:
                wait = None if (last is None or self._there_is_states) else 3.0
            try:
                message = await self._next_step(wait)
            except (TimeoutError, StopAsyncIteration):
                if background:
                    await tasks.event(
                        task_id, "background_unfinished", ", ".join(background.values()), None
                    )
                break
            if isinstance(message, SystemMessage):
                data = message.data or {}
                if message.subtype == "init":
                    self.session_id = data.get("session_id") or self.session_id
                    self.ses.session_id = self.session_id
                    if self.origin == "orchestrator" and task_id and self.session_id:
                        # Saved right away: an interruption in the middle of
                        # the first turn is also resumed.
                        await tasks.update(task_id, session_id=self.session_id, session_used=now())
                    await tasks.event(
                        task_id,
                        "session",
                        f"model {data.get('model')}",
                        {
                            "session_id": self.session_id,
                            "tools": len(data.get("tools") or []),
                        },
                    )
                elif message.subtype == "session_state_changed":
                    self._there_is_states = True
                    state = data.get("state")
                    if state in ("running", "requires_action"):
                        running, pending_idle = True, False
                        limit_background = None
                    elif state == "idle":
                        if last is not None:
                            if aborted or not pending():
                                break
                            await tasks.event(
                                task_id,
                                "waiting_plain_second",
                                ", ".join(background.values()),
                                None,
                            )
                            limit_background = cap_background()
                            continue
                        if running:
                            pending_idle = True
                elif message.subtype in ("task_started", "task_notification"):
                    background_id = str(data.get("task_id") or "")
                    if message.subtype == "task_started" and background_id:
                        background[background_id] = str(data.get("task_type") or "local_bash")
                        if limit_background is not None:
                            limit_background = cap_background()
                    elif background_id in background:
                        del background[background_id]
                        if last is not None:
                            # Claude Code resumes the turn with the
                            # notification (without signaling with the
                            # `running` state); if it doesn't within a
                            # while, the turn is closed.
                            limit_background = (
                                cap_background() if background else loop.time() + RESUMPTION_WAIT
                            )
                    await tasks.event(
                        task_id,
                        f"background_{message.subtype}",
                        str(data.get("description") or data.get("summary") or "")[:300],
                        {k: data.get(k) for k in ("task_id", "task_type", "status")},
                    )
                elif message.subtype == "compact_boundary":
                    await tasks.event(task_id, "compaction", "conversation compacted", None)
            elif isinstance(message, AssistantMessage):
                limit_background = None  # the agent is working: the turn will end with a result
                await self._count_tokens(message)
                if message.error:
                    texts = " ".join(b.text for b in message.content if isinstance(b, TextBlock))
                    errors.append(f"{message.error}: {texts}".strip())
                if message.parent_tool_use_id is None:
                    for block in message.content:
                        if isinstance(block, TextBlock) and block.text.strip():
                            await tasks.event(task_id, "agent_text", block.text[:1500])
                        elif isinstance(block, ToolUseBlock) and block.name in ("Agent", "Task"):
                            await tasks.event(
                                task_id,
                                "delegation",
                                f"{block.input.get('subagent_type')}: {block.input.get('description', '')}",
                                {
                                    "subagent_type": block.input.get("subagent_type"),
                                    "prompt": str(block.input.get("prompt", ""))[:3000],
                                },
                            )
            elif isinstance(message, RateLimitEvent):
                info = message.rate_limit_info
                await self.ses.core.usage.log_limit(info)
                if info.status == "rejected":
                    self._limit = info
            elif isinstance(message, ResultMessage):
                last = message
                if message.terminal_reason in ("aborted_streaming", "aborted_tools"):
                    aborted = True
                self.session_id = message.session_id or self.session_id
                self.ses.session_id = self.session_id
                delta = await self.ses.core.usage.record(
                    task_id=task_id,
                    origin=self.origin,
                    model_usage=message.model_usage,
                    previous=self.snapshot,
                    turns=message.num_turns or 0,
                    main_model=self.current_model,
                )
                cost += delta
                turns += message.num_turns or 0
                try:
                    await self.ses.core.usage.save_snapshot(self.session_id, self.snapshot)
                except Exception:
                    log.exception("could not save the session's accumulated usage")
                if task_id is not None:
                    await tasks.add_consumption(task_id, delta, message.num_turns or 0)
                    # The exact usage is already in the database: the turn's
                    # goes back to zero.
                    await self._prepare_budget(new_turn=False)
                if pending_idle and (aborted or not pending()):
                    break
                # It waits a few seconds for `idle` (a turn resumed by a
                # notification doesn't always send it) or, if background work
                # remains, until its cap.
                limit_background = (
                    cap_background() if pending() and not aborted else loop.time() + IDLE_WAIT
                )
        return self._classify(last, errors, cost, turns)

    async def _prepare_budget(self, new_turn: bool = True) -> None:
        """Task usage before what arrives now, for the budget of tasks with
        Chrome and the task's own."""
        ses = self.ses
        if ses.role != "orchestrator" or ses.task_id is None:
            return
        if new_turn:
            ses.cap_exceeded_tokens = False
            await navigation.prepare(ses)
            try:
                task = await ses.core.tasks.get(ses.task_id)
                ses.cap_tokens_task = task.token_cap if task is not None else None
            except Exception:
                log.exception("could not read the budget of task %s", ses.task_id)
        ses.tokens_messages.clear()
        try:
            ses.previous_tokens = (await ses.core.usage.task_consumption(ses.task_id)).total
        except Exception:
            log.exception("could not read the usage of task %s", ses.task_id)

    async def _count_tokens(self, message: AssistantMessage) -> None:
        """Budget of tasks with Chrome and of those with their
        own.

        Adds up the usage of each model response, including those of subagents (they arrive with their
        `parent_tool_use_id`, verified with real sessions). The session repeats the message for
        each block with the same identifier, so each one counts once. If the task exceeds
        its budget (its own or, if it used real Chrome, the Chrome one), the turn is cut
        and the task asks the user, as with the turn cap.
        """
        ses = self.ses
        usage = message.usage or {}
        key = message.message_id or message.uuid
        if not usage or not key or ses.role != "orchestrator" or ses.task_id is None:
            return
        total = sum(int(usage.get(k) or 0) for k in USAGE_FIELDS)
        ses.tokens_messages[key] = max(ses.tokens_messages.get(key, 0), total)
        await self.check_budget()

    async def check_budget(self) -> None:
        """Cuts the turn if the task has exceeded its token budget."""
        ses = self.ses
        if ses.cap_exceeded_tokens or ses.done_delivery or ses.task_id is None:
            return
        current = ses.current_token_cap()
        if current is None:
            return
        cap, _of = current
        spent = ses.spent_tokens()
        if spent <= cap:
            return
        ses.cap_exceeded_tokens = True
        await ses.core.tasks.event(
            ses.task_id,
            "token_cap",
            f"{format_tokens(spent, False)} of {format_tokens(cap)}",
            {"tokens": spent, "cap": cap},
        )
        self._cutoff = asyncio.ensure_future(self.interrupt())

    def _classify(
        self, result: ResultMessage | None, errors: list[str], cost: float, turns: int
    ) -> TurnResult:
        base = dict(session_id=self.session_id, cost=cost, turns=turns)
        errors_text = " | ".join(errors)
        cut = result is None or result.is_error or result.terminal_reason in CUTOFFS
        if self.ses.cap_exceeded_tokens and cut:
            # If the turn finished before the cut arrived, its work stands and
            # continues.
            cap, of = self.ses.current_token_cap() or (
                self.ses.core.config.settings.real_browser.max_tokens,
                "chrome",
            )
            if of == "chrome":
                detail = f"it went over the {format_tokens(cap)} budget for a task that uses Chrome"
            else:
                detail = f"it went over its budget of {format_tokens(cap)}"
            return TurnResult("token_cap", detail=detail, **base)
        if result is None:
            return TurnResult(
                "error", detail=errors_text or "the session ended without a result", **base
            )
        low = (
            errors_text + " " + (result.result or "") + " " + " ".join(result.errors or [])
        ).lower()
        if result.subtype == "error_max_turns":
            return TurnResult("turn_cap", detail="it hit its turn limit", **base)
        if result.subtype == "error_max_budget_usd":
            return TurnResult("usd_cap", detail="it hit its spending limit", **base)
        if "authentication_failed" in low or any(s in low for s in LOGIN_SIGNALS):
            return TurnResult("login", detail=errors_text or result.result or "", **base)
        limit = self._limit
        if self._is_limit(low, result) or (limit is not None and result.is_error):
            limit_kind = getattr(limit, "rate_limit_type", None) or ""
            reset = None
            if limit is not None and getattr(limit, "resets_at", None):
                reset = datetime.fromtimestamp(limit.resets_at, UTC)
            reset = reset or _hour_from_text(low) or datetime.now(UTC) + timedelta(hours=1)
            self._limit = None
            kind: ResultKind = (
                "opus_limit" if ("opus" in limit_kind or "opus limit" in low) else "limit"
            )
            return TurnResult(kind, detail=errors_text or limit_kind, reset=reset, **base)
        if self.ses.chrome and self.ses.browser_errors > 0:
            # The last action in Chrome failed due to connection (also if the turn was
            # cut because of it).
            return TurnResult("browser", text=result.result or "", **base)
        if result.terminal_reason in ("aborted_streaming", "aborted_tools"):
            return TurnResult("interrupted", text=result.result or "", **base)
        if result.subtype != "success" or result.is_error:
            detail = (
                errors_text or result.result or ", ".join(result.errors or []) or result.subtype
            )
            return TurnResult("error", detail=detail, **base)
        return TurnResult(
            "ok", text=result.result or "", structured=result.structured_output, **base
        )

    @staticmethod
    def _is_limit(low: str, result: ResultMessage) -> bool:
        if result.api_error_status == 429 and "temporarily limiting" not in low:
            return True
        return result.is_error and any(s in low for s in LIMIT_SIGNALS)


def _hour_from_text(text: str) -> datetime | None:
    """Reset time from messages like 'resets 3:45pm' (Mac local time)."""
    m = re.search(r"resets\s+(\d{1,2})(?::(\d{2}))?\s*(am|pm)?", text)
    if not m:
        return None
    hour, minute, ampm = int(m.group(1)), int(m.group(2) or 0), m.group(3)
    if ampm == "pm" and hour < 12:
        hour += 12
    if ampm == "am" and hour == 12:
        hour = 0
    local_now = datetime.now().astimezone()
    candidate = local_now.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)
