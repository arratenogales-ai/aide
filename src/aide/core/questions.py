"""Agent questions to the user (AskUserQuestion converted into Telegram buttons)."""

from __future__ import annotations

import asyncio
import itertools
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from aide.bot.channel import Button, Channel
from aide.bot.formatting import bold, esc
from aide.core.tasks import TaskRepository

log = logging.getLogger(__name__)


@dataclass
class PendingQuestion:
    """A question awaiting the user's answer, with its future and selection."""

    id: int
    task_id: int | None
    question: dict[str, Any]
    future: asyncio.Future[str]
    selection: set[int] = field(default_factory=set)
    message_id: int | None = None
    waiting_text: bool = False


class QuestionManager:
    """Manages the questions asked to the user and their pending answers."""

    def __init__(self, tasks: TaskRepository, channel: Callable[[], Channel | None]) -> None:
        self.tasks = tasks
        self._channel = channel
        self._ids = itertools.count(1)
        self._pending: dict[int, PendingQuestion] = {}

    def waiting_text(self) -> PendingQuestion | None:
        """Returns the pending question waiting for a text answer, or None."""
        for p in self._pending.values():
            if p.waiting_text and not p.future.done():
                return p
        return None

    async def ask(self, task_id: int | None, entry: dict[str, Any]) -> dict[str, Any]:
        """Asks the questions one by one and returns the input updated with `answers`."""
        replies: dict[str, str] = {}
        for question in entry.get("questions") or []:
            replies[question.get("question", "")] = await self._one(task_id, question)
        await self.tasks.event(task_id, "answered_question", "", {"replies": replies})
        return {"questions": entry.get("questions") or [], "answers": replies}

    async def _one(self, task_id: int | None, question: dict[str, Any]) -> str:
        channel = self._channel()
        pid = next(self._ids)
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        pending = PendingQuestion(pid, task_id, question, future)
        self._pending[pid] = pending
        try:
            if channel is None:
                raise RuntimeError("no channel to ask the question")
            pending.message_id = await channel.send(
                self._text(pending), buttons=self._buttons(pending)
            )
            await self.tasks.link_message(pending.message_id, task_id, "question")
            await self.tasks.event(task_id, "question", question.get("question", ""), question)
            return await future
        finally:
            self._pending.pop(pid, None)

    def _text(self, p: PendingQuestion) -> str:
        q = p.question
        lines = [bold(q.get("header") or "Question"), esc(q.get("question", ""))]
        for n, option in enumerate(q.get("options") or [], 1):
            description = option.get("description") or ""
            lines.append(
                esc(
                    f"{n}. {option.get('label', '')}" + (f" ({description})" if description else "")
                )
            )
        if q.get("multiSelect"):
            lines.append(esc("You can pick several options and tap Done."))
        return "\n".join(lines)

    def _buttons(self, p: PendingQuestion) -> list[list[Button]]:
        rows = []
        for n, option in enumerate(p.question.get("options") or []):
            mark = "✓ " if n in p.selection else ""
            rows.append([Button(f"{mark}{option.get('label', '')}"[:60], f"sched:{p.id}:{n}")])
        if p.question.get("multiSelect"):
            rows.append([Button("Done", f"sched:{p.id}:ready")])
        rows.append([Button("Other answer", f"sched:{p.id}:other")])
        return rows

    async def resolve_button(self, pid: int, value: str) -> bool:
        """Handles a button answer to a pending question; returns False if none is open."""
        p = self._pending.get(pid)
        if p is None or p.future.done():
            return False
        options = p.question.get("options") or []
        channel = self._channel()
        if value == "other":
            p.waiting_text = True
            if channel:
                await channel.send(esc("Type your answer in your next message."))
            return True
        if value == "ready":
            labels = [options[i].get("label", "") for i in sorted(p.selection)]
            p.future.set_result(", ".join(labels))
            return True
        try:
            index = int(value)
            label = options[index].get("label", "")
        except (ValueError, IndexError):
            return False
        if p.question.get("multiSelect"):
            p.selection ^= {index}
            if channel and p.message_id:
                await channel.edit(p.message_id, self._text(p), buttons=self._buttons(p))
            return True
        p.future.set_result(label)
        return True

    async def receive_text(self, text: str) -> bool:
        """Gives a text answer to the question waiting for one; returns whether it did."""
        p = self.waiting_text()
        if p is None:
            return False
        p.future.set_result(text)
        return True

    def cancel_from_task(self, task_id: int) -> None:
        """Cancels the unanswered questions of a task."""
        for p in list(self._pending.values()):
            if p.task_id == task_id and not p.future.done():
                p.future.cancel()
