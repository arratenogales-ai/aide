"""Quick replies and conversation.

Sonnet with read-only access to the calendar, email, memory, history and the web.
The session is reused while there is recent activity and up to a number of exchanges,
so chained questions have context without creating an endless conversation.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aide.core import prompts
from aide.core.context import Core, SessionContext
from aide.core.injection import search_instructions
from aide.core.orchestrator import AgentSession
from aide.core.session import quick_options
from aide.core.tasks import Task

log = logging.getLogger(__name__)


class QuickReplyError(Exception):
    """An exception type for quick reply errors."""

    pass


class QuickConversation:
    """A quick-answer conversation that keeps its session id and exchange count."""

    def __init__(self, core: Core) -> None:
        self.core = core
        self.session_id: str | None = None
        self.last: datetime | None = None
        self.exchanges = 0
        self._lock = asyncio.Lock()

    def _reusable(self) -> bool:
        settings = self.core.config.settings.quick_conversation
        if not self.session_id or self.last is None:
            return False
        recent = datetime.now(UTC) - self.last < timedelta(minutes=settings.inactivity_minutes)
        return recent and self.exchanges < settings.max_exchanges

    async def reply(
        self, task: Task, images: list[Path] | None = None, context: str | None = None
    ) -> str:
        """Replies to a quick task, resuming the earlier session if it can be reused."""
        async with self._lock:
            config = self.core.config
            role = config.role("quick_reply")
            cap = config.settings.caps.quick
            resume = self.session_id if self._reusable() else None
            if resume is None:
                self.exchanges = 0
            ses = SessionContext(
                core=self.core,
                task_id=task.id,
                folder=None,
                role="quick",
                phase="quick",
                user_texts=[task.request],
            )
            options = quick_options(
                ses, role, resume=resume, max_turns=cap.turns, max_budget_usd=cap.usd
            )
            prompt = task.request
            if context:
                # Replies to an agent message: it goes as data, not as the user's
                # words, and is checked for instructions.
                suspicion = search_instructions(context)
                if suspicion:
                    ses.suspicious_injection = f'the message the user is replying to: "{suspicion}"'
                prompt = (
                    prompts.block_answered_message(context).strip()
                    + f"\n\nWhat the user writes:\n{prompt}"
                )
            if images:
                items = "\n".join(f"- {path}" for path in images)
                prompt = (
                    f"{prompt}\n\nThe user sent this photo with the question. Look at it with "
                    f"Read before answering:\n{items}"
                )
            async with AgentSession(ses, options, "quick") as session:
                result = await session.turn(prompt)
            if result.kind != "ok":
                self.session_id = None
                raise QuickReplyError(f"{result.kind}: {result.detail}")
            self.session_id = result.session_id
            self.last = datetime.now(UTC)
            self.exchanges += 1
            await self.core.tasks.update(task.id, session_id=result.session_id, model=role.id)
            return result.text.strip() or "I don't have an answer for that."
