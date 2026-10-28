"""Task states and valid transitions.

Any transition not in the table is a programming error and is rejected.
"""

from __future__ import annotations

from enum import StrEnum


class TaskState(StrEnum):
    """The states a task goes through, from received to completed, failed or cancelled."""

    RECEIVED = "received"
    IN_TRIAGE = "in_triage"
    PLANNING = "planning"
    WAITING_PLAN = "waiting_plan"
    IN_QUEUE = "in_queue"
    RUNNING = "running"
    WAITING_APPROVAL = "waiting_approval"
    REVIEWING = "reviewing"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    PAUSED_LIMIT = "paused_limit"
    PAUSED_CAP = "paused_cap"
    PAUSED_LOGIN = "paused_login"
    PAUSED_BROWSER = "paused_browser"
    PAUSED_SHUTDOWN = "paused_shutdown"


FINAL = frozenset({TaskState.COMPLETED, TaskState.FAILED, TaskState.CANCELLED})
PAUSED = frozenset(
    {
        TaskState.PAUSED_LIMIT,
        TaskState.PAUSED_CAP,
        TaskState.PAUSED_LOGIN,
        TaskState.PAUSED_BROWSER,
        TaskState.PAUSED_SHUTDOWN,
    }
)
# States in which an SDK session is working or waiting for the user within a turn.
IN_PROGRESS = frozenset(
    {
        TaskState.IN_TRIAGE,
        TaskState.PLANNING,
        TaskState.RUNNING,
        TaskState.WAITING_APPROVAL,
        TaskState.REVIEWING,
    }
)

_ALWAYS = {TaskState.CANCELLED, TaskState.FAILED, *PAUSED}

TRANSITIONS: dict[TaskState, frozenset[TaskState]] = {
    TaskState.RECEIVED: frozenset(
        {TaskState.IN_TRIAGE, TaskState.PLANNING, TaskState.IN_QUEUE, TaskState.RUNNING, *_ALWAYS}
    ),
    TaskState.IN_TRIAGE: frozenset(
        {TaskState.PLANNING, TaskState.IN_QUEUE, TaskState.RUNNING, TaskState.COMPLETED, *_ALWAYS}
    ),
    TaskState.PLANNING: frozenset({TaskState.WAITING_PLAN, TaskState.WAITING_APPROVAL, *_ALWAYS}),
    TaskState.WAITING_PLAN: frozenset({TaskState.PLANNING, TaskState.IN_QUEUE, *_ALWAYS}),
    TaskState.IN_QUEUE: frozenset({TaskState.RUNNING, TaskState.PLANNING, *_ALWAYS}),
    TaskState.RUNNING: frozenset(
        {TaskState.WAITING_APPROVAL, TaskState.REVIEWING, TaskState.COMPLETED, *_ALWAYS}
    ),
    TaskState.WAITING_APPROVAL: frozenset(
        {TaskState.RUNNING, TaskState.PLANNING, TaskState.REVIEWING, *_ALWAYS}
    ),
    TaskState.REVIEWING: frozenset({TaskState.RUNNING, TaskState.COMPLETED, *_ALWAYS}),
    # A finished task is reopened when the user asks for a follow-up.
    TaskState.COMPLETED: frozenset({TaskState.IN_QUEUE, TaskState.RUNNING}),
    TaskState.FAILED: frozenset({TaskState.IN_QUEUE, TaskState.RUNNING}),
    TaskState.CANCELLED: frozenset(),
    **{
        pause: frozenset(
            {
                TaskState.IN_QUEUE,
                TaskState.RUNNING,
                TaskState.PLANNING,
                TaskState.WAITING_PLAN,
                TaskState.REVIEWING,
                *_ALWAYS,
            }
            - {pause}
        )
        for pause in PAUSED
    },
}


class InvalidTransition(Exception):
    """Error raised when a task is moved to a state it cannot go to from its current one."""

    pass


def check_transition(since: TaskState, until: TaskState) -> None:
    if until not in TRANSITIONS[since]:
        raise InvalidTransition(f"transition not allowed: {since} -> {until}")
