"""What the engine parts share: types, keys and waits."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aide.core import scheduler
from aide.core.context import SessionContext
from aide.core.orchestrator import AgentSession

# All parts of the engine log under the same name, the usual one.
log = logging.getLogger("aide.core.engine")

# With no connection: the task that loses it midway waits for the user to resume it, the
# maintenance that comes due without it waits for the next one and the user is told about the tasks
# on return.
OFFLINE_REASON = "offline"
PENDING_MAINTENANCE_KEY = "maintenance:pending"
# What from their tasks did not reach them for lack of connection: {id:
# paused|finished|failed|reply}.
NOT_NOTIFIED_KEY = "connection:not_notified"
# To say “the photo” or the file name when asking what to do with it.
PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".heic", ".heif"}
# Time during which the user's next message applies to a photo that arrived without text.
PHOTO_WAIT = timedelta(minutes=30)
# And to a document after pressing “Edit”.
CORRECTION_WAIT = timedelta(minutes=30)


def short_day(local: datetime) -> str:
    """“Oct 28” (independent of the system language)."""
    return f"{scheduler.SHORT_MONTHS[local.month - 1]} {local.day}"


@dataclass
class TaskRun:
    """The live state of a running task: its session, async task and flags."""

    task_id: int
    ses: SessionContext
    session: AgentSession | None = None
    async_task: asyncio.Task | None = None
    cancelled: bool = False
    paused: bool = False


@dataclass
class TextWait:
    """A pending wait for the user's text on a task, with its kind and time."""

    # plan_change | document_correction (the id is the document's)
    kind: str
    task_id: int
    hour: datetime = field(default_factory=lambda: datetime.now(UTC))


@dataclass
class PendingPhoto:
    """Photos or files that arrived without text: it waits for the user to say what to do with
    them, with a button or with their next message."""

    paths: list[Path]
    hour: datetime
    # What its buttons carry, to know they are still these files.
    key: str = ""


@dataclass
class Triage:
    """The triage outcome for a message: kind, size, needs and optional title."""

    kind: str = "new_task"
    size: str = "small"
    external_actions: bool = False
    related_task: int | None = None
    needs_plan: bool = False
    needs_real_browser: bool = False
    title: str | None = None
    # Answered in Telegram, but with the “Do it as a task” button.
    offer_task: bool = False
