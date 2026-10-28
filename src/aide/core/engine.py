"""Engine: single entry point of the core.

Receives inputs (Telegram or simulator), routes them by code, manages the queue of heavy tasks and
takes each task through its phases (plan, execution, review, delivery) with one SDK session per
task.

Here are the state (`__init__`), the lifecycle and the warnings; the rest of the methods, by topic,
are in the `engine_*.py` files, mixin classes with no state of their own.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from aide.bot.channel import Button, Channel, Entry
from aide.bot.formatting import esc, truncate
from aide.common.config import Config
from aide.core.approvals import ApprovalManager
from aide.core.connection import Connection
from aide.core.context import Core
from aide.core.db import Database
from aide.core.engine_checks import ChecksMixin
from aide.core.engine_common import (
    NOT_NOTIFIED_KEY,
    OFFLINE_REASON,
    PENDING_MAINTENANCE_KEY,
    PendingPhoto,
    TaskRun,
    TextWait,
    Triage,
    log,
)
from aide.core.engine_control import ControlMixin, requested_chrome
from aide.core.engine_entry import EntryMixin
from aide.core.engine_execution import ExecutionMixin
from aide.core.engine_google import GoogleMixin
from aide.core.engine_scheduled import ScheduledMixin
from aide.core.idempotency import ActionLog
from aide.core.policy import Policy
from aide.core.questions import QuestionManager
from aide.core.states import IN_PROGRESS, TaskState
from aide.core.system import KeepAwake, chrome_state
from aide.core.tasks import Task, TaskRepository
from aide.core.usage import UsageLog
from aide.memory.memory import Memory

# What other modules and tests import from here.
__all__ = [
    "PENDING_MAINTENANCE_KEY",
    "NOT_NOTIFIED_KEY",
    "OFFLINE_REASON",
    "TaskRun",
    "TextWait",
    "PendingPhoto",
    "Engine",
    "Triage",
    "requested_chrome",
]


class Engine(
    EntryMixin,
    ExecutionMixin,
    ControlMixin,
    ChecksMixin,
    ScheduledMixin,
    GoogleMixin,
):
    """The agent's core engine, combining entry, execution, control and check mixins."""

    def __init__(self, config: Config, channel: Channel | None = None) -> None:
        self.config = config
        self.channel: Channel | None = channel
        self.db = Database(config.paths.db)
        self.tasks = TaskRepository(self.db, config.paths)
        # Tasks created or moved in this startup: recovery after an outage does not touch
        # them.
        self._live: set[int] = set()
        self.tasks.observer = self._live.add
        self.usage = UsageLog(self.db)
        self.policy = Policy(config)
        self.approvals = ApprovalManager(
            self.db,
            self.tasks,
            lambda: self.channel,
            reminder_hours=config.settings.approval_reminder_hours,
            text_limit=config.settings.telegram.text_limit,
        )
        self.questions = QuestionManager(self.tasks, lambda: self.channel)
        self.core = Core(
            config=config,
            db=self.db,
            tasks=self.tasks,
            usage=self.usage,
            policy=self.policy,
            approvals=self.approvals,
            questions=self.questions,
            ref_channel=lambda: self.channel,
            actions=ActionLog(self.db),
            notify=self.notify,
        )
        self.memory = Memory(config.paths.memory, self.db)
        self.core.services["memory"] = self.memory
        self.core.services["down_browser"] = self.down_browser
        self.queue: asyncio.Queue[int] = asyncio.Queue()
        self._in_queue: set[int] = set()
        self.in_progress: dict[int, TaskRun] = {}
        self._planning: set[int] = set()
        self._quick: set[int] = set()
        self._workers: list[asyncio.Task] = []
        self._background: set[asyncio.Task] = set()
        self.awake = KeepAwake()
        self.text_wait: TextWait | None = None
        self.pending_photo: PendingPhoto | None = None
        self._requesting_script: set[int] = set()
        # Weekly maintenance (connected in application.py): (engine, test) -> None.
        self.maintenance: Callable[[Engine, bool], Awaitable[None]] | None = None
        self.next_model: str | None = None
        # Optional hooks connected in later phases.
        self.triage: Callable[[str, list[Task], int | None], Awaitable[Triage]] | None = None
        self.review: Callable[[Task, str], Awaitable[list[str]]] | None = None
        self.reply_quick: Callable[[Task], Awaitable[str]] | None = None
        self.commands: Callable[[Engine, Entry], Awaitable[None]] | None = None
        self.transcribe: Callable[[Path], Awaitable[str]] | None = None
        self.to_change: Callable[[], Awaitable[None]] | None = None
        # Chrome open with the extension connected (tests replace it).
        self.ready_chrome: Callable[[], Awaitable[bool]] = _ready_chrome
        # Valid Claude Code login (tests replace it); by default, a minimal request.
        self.valid_login: Callable[[], Awaitable[bool]] = self._valid_login
        # Request that brings new plan limits data (tests replace it).
        self.request_limits: Callable[[], Awaitable[Any]] = self._minimal_request
        self.checks_interval = 30.0
        # Whether there is internet: tests replace it or set it offline.
        self.connection = Connection()
        self._maintenance_in_progress = False
        self._email_no_google = False
        self._email_no_network = False
        # -inf: the first check with a connection runs them right away. The Mac's monotonic
        # clock counts from the boot, so with 0 they would wait until it reached each interval.
        self._last_slow = float("-inf")
        self._last_expiry = float("-inf")
        self._last_email = float("-inf")
        self._last_calendar = float("-inf")
        self._summary_in_running = False
        self._limits_lock = asyncio.Lock()
        self._last_attempt_limits: float | None = None
        self._stopping = False
        self.started = False

    # --- life cycle ------------------------------------------------------------------

    async def start(self, workers: bool = True) -> None:
        """Connects the database and prepares folders, optionally starting worker loops."""
        await self.db.connect()
        self.config.paths.ensure()
        self.next_model = await self.db.kv_read("next_model")
        if workers:
            for _ in range(self.config.settings.simultaneous_heavy_tasks):
                self._workers.append(asyncio.create_task(self._worker()))
            self._launch(self._typing_loop())
            self._launch(self._reminders_loop())
            self._launch(self._checks_loop())
        self.started = True

    async def stop(self) -> None:
        """Clean shutdown: tasks in progress go to paused_shutdown and resume at startup.

        First the status is set and each run is cancelled (which closes its own session on exit);
        nothing is retried during shutdown. All waits are bounded.
        """
        self._stopping = True
        runs = list(self.in_progress.values())
        for run in runs:
            task = await self.tasks.get(run.task_id)
            if task and task.state_enum in IN_PROGRESS:
                with contextlib.suppress(Exception):
                    await self.tasks.change_state(
                        task.id, TaskState.PAUSED_SHUTDOWN, "program shutdown"
                    )
        # Each task is cancelled only once: a second cancellation would interrupt its
        # cleanup.
        async_tasks = {*self._workers, *self._background}
        async_tasks |= {e.async_task for e in runs if e.async_task is not None}
        for t in async_tasks:
            t.cancel()
        if async_tasks:
            await asyncio.wait(async_tasks, timeout=40)
        for run in runs:
            if run.session is not None:  # in case the cancellation did not manage to close it
                await run.session.close()
        browser = self.core.services.get("browser_no_session")
        if browser is not None:
            with contextlib.suppress(Exception):
                await browser.close()
        await self.awake.close()
        await self.db.close()

    def _launch(self, coroutine: Awaitable[Any]) -> asyncio.Task:
        t = asyncio.ensure_future(coroutine)
        self._background.add(t)
        t.add_done_callback(self._background.discard)
        return t

    def launch(self, coroutine: Awaitable[Any]) -> asyncio.Task:
        """Background work of another module; shutdown cancels it along with the rest."""
        return self._launch(coroutine)

    async def _notify_change(self) -> None:
        if self.to_change is not None:
            with contextlib.suppress(Exception):
                await self.to_change()

    # --- communication with the user -----------------------------------------------------------

    async def notify(
        self,
        task_id: int | None,
        text: str,
        buttons: list[list[Button]] | None = None,
        html: bool = False,
        record: bool = True,
    ) -> int | None:
        """Sends a message to the user through the channel; returns its id or None."""
        if self.channel is None:
            return None
        if not await self.connection.there_is():
            # With no connection (confirmed) nothing is sent or saved for later: on
            # return, no accumulated warnings arrive. What matters (Google,
            # security, their tasks') is looked at again then. With record=False,
            # without the text.
            log.info("offline: notice not sent%s", f" ({truncate(text, 80)})" if record else "")
            return None
        content = text if html else esc(text)
        message_id = await self.channel.send(content, buttons=buttons)
        await self.tasks.link_message(message_id, task_id, "output")
        return message_id


async def _ready_chrome() -> bool:
    return (await asyncio.to_thread(chrome_state)).ready
