"""How task states are shown to the user (panel and Telegram).

The 16 internal states (states.py) don't change: here they are only grouped into five and
explained in plain language why it is paused or what is expected from the user.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aide.core.db import to_date
from aide.core.states import PAUSED, TaskState

if TYPE_CHECKING:
    from aide.core.tasks import Task


@dataclass(frozen=True)
class Group:
    """A task group for the panel, with its key, name and states."""

    key: str  # also the CSS class and the panel filter value
    name: str
    states: frozenset[TaskState]


GROUPS: tuple[Group, ...] = (
    Group(
        "in_progress",
        "In progress",
        frozenset(
            {
                TaskState.RECEIVED,
                TaskState.IN_TRIAGE,
                TaskState.PLANNING,
                TaskState.IN_QUEUE,
                TaskState.RUNNING,
                TaskState.REVIEWING,
            }
        ),
    ),
    Group(
        "waiting",
        "Waiting for you",
        frozenset({TaskState.WAITING_PLAN, TaskState.WAITING_APPROVAL}),
    ),
    Group("in_pause", "Paused", PAUSED),
    Group("completed", "Completed", frozenset({TaskState.COMPLETED})),
    Group("cancelled", "Canceled or failed", frozenset({TaskState.CANCELLED, TaskState.FAILED})),
)
_BY_STATE = {state: task_group for task_group in GROUPS for state in task_group.states}
_BY_KEY = {task_group.key: task_group for task_group in GROUPS}


def task_group(state: str | TaskState) -> Group:
    return _BY_STATE[TaskState(state)]


def group_by_key(key: str) -> Group | None:
    return _BY_KEY.get(key)


def subtitle(task: Task, zone: str, moment: datetime | None = None) -> str | None:
    """Reason for the pause, or what is expected from the user, in plain language."""
    state = task.state_enum
    reason = task.pause_reason or ""
    if state == TaskState.PAUSED_LIMIT:
        until = to_date(task.pause_until)
        if until is None:
            return "plan limit, resumes on its own"
        return f"plan limit, resumes {_at_time(until, zone, moment)}"
    if state == TaskState.PAUSED_CAP:
        return "usage cap reached, reply in Telegram"
    if state == TaskState.PAUSED_LOGIN:
        if reason == "google":
            return "Google authorization expired"
        return "Claude session expired"
    if state == TaskState.PAUSED_BROWSER:
        return "Chrome closed"
    if state == TaskState.PAUSED_SHUTDOWN:
        if reason == "paused by the user":
            return "paused by you"
        return "the program stopped, resumes on startup"
    if state == TaskState.WAITING_PLAN:
        return "plan awaiting approval in Telegram"
    if state == TaskState.WAITING_APPROVAL:
        return "approval pending in Telegram"
    return None


def readable_state(task: Task, zone: str) -> str:
    """“Paused (Chrome closed)”, “Completed”..."""
    text = task_group(task.state).name
    detail = subtitle(task, zone)
    return f"{text} ({detail})" if detail else text


def _at_time(date: datetime, zone: str, moment: datetime | None) -> str:
    local = date.astimezone(ZoneInfo(zone))
    today = (moment or datetime.now(UTC)).astimezone(ZoneInfo(zone)).date()
    days = (local.date() - today).days
    if days == 0:
        return f"at {local:%H:%M}"
    if days == 1:
        return f"tomorrow at {local:%H:%M}"
    return f"on {local:%b} {local.day} at {local:%H:%M}"
