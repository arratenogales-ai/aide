"""Task progress: plan steps, what it is doing now, who is working and how long it has been.

The steps come from the orchestrator's TaskCreate and TaskUpdate tools, which the
PostToolUse hook records in the `steps` table. The rest comes from the timeline and state
changes. This module only records and reads: it doesn't intervene in the task's execution.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aide.core.db import from_json, to_date
from aide.core.states import FINAL, PAUSED, TaskState

if TYPE_CHECKING:
    from aide.core.tasks import Task, TaskRepository

STEP_STATES = {"pending": "pending", "in_progress": "in progress", "completed": "done"}
STEP_MARKS = {"pending": "○", "in_progress": "▸", "completed": "✓"}


@dataclass
class Step:
    """One step of a task's progress, with subject, action and state."""

    subject: str
    action: str | None
    state: str  # pending, in_progress o completed

    @property
    def readable_state(self) -> str:
        return STEP_STATES.get(self.state, self.state)

    @property
    def mark(self) -> str:
        return STEP_MARKS.get(self.state, "○")


@dataclass
class Progress:
    """The progress of a task: steps, current action, subagents and timing."""

    steps: list[Step] = field(default_factory=list)
    now: str | None = None  # what it is doing now, in one sentence
    subagents: list[str] = field(default_factory=list)  # subagents working now
    since: str | None = None  # ISO time when it started working
    until: str | None = None  # ISO time when it finished (if it has finished)

    @property
    def facts(self) -> int:
        return sum(1 for p in self.steps if p.state == "completed")

    @property
    def total(self) -> int:
        return len(self.steps)

    @property
    def percentage(self) -> int:
        return round(100 * self.facts / self.total) if self.total else 0

    def elapsed(self, moment: datetime | None = None) -> str | None:
        """Returns the working time from start to end as text, or None without a start."""
        start = to_date(self.since)
        if start is None:
            return None
        end = to_date(self.until) or moment or datetime.now(UTC)
        return duration((end - start).total_seconds())


def duration(seconds: float) -> str:
    """“40 s”, “12 min”, “1 h 05 min”, “2 days 3 h”."""
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} s"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes:02d} min"
    days, hours = divmod(hours, 24)
    return f"{days} days {hours} h"


# --- logging from the PostToolUse hook ---------------------------------------------------------


async def log_tool(
    tasks: TaskRepository,
    task_id: int,
    session: str,
    tool: str,
    entry: dict[str, Any],
    reply: Any,
) -> None:
    """Saves a step created (TaskCreate) or updated (TaskUpdate) by the orchestrator."""
    if tool == "TaskCreate":
        step = _id_created(reply)
        subject = str(entry.get("subject") or "").strip()
        if step and subject:
            action = str(entry.get("activeForm") or "").strip() or None
            await tasks.create_step(task_id, session, step, subject[:300], action)
    elif tool == "TaskUpdate":
        step = str(entry.get("taskId") or "").strip()
        if step:
            await tasks.update_step(
                task_id,
                session,
                step,
                state=_text(entry.get("status")),
                subject=_text(entry.get("subject")),
                action=_text(entry.get("activeForm")),
            )


def _text(value: Any) -> str | None:
    return str(value).strip()[:300] if value else None


def _id_created(reply: Any) -> str | None:
    """Id of the created step. Claude Code returns {"task": {"id": "1", ...}}."""
    if isinstance(reply, str):
        try:
            reply = json.loads(reply)
        except ValueError:
            m = re.search(r"[Tt]ask #?(\d+)", reply)
            return m.group(1) if m else None
    if isinstance(reply, dict):
        task = reply.get("task")
        if isinstance(task, dict) and task.get("id") is not None:
            return str(task["id"])
        if reply.get("taskId") is not None:
            return str(reply["taskId"])
    return None


# --- reading ---------------------------------------------------------------------------------


async def progress(tasks: TaskRepository, task: Task) -> Progress:
    """Builds a Progress for a task from its steps and state transitions."""
    steps = [Step(p["subject"], p["action"], p["state"]) for p in await tasks.steps(task.id)]
    result = Progress(steps=steps)
    transitions = await tasks.transitions(task.id)
    working = [t for t in transitions if t["until"] in ("planning", "running")]
    result.since = working[0]["hour"] if working else task.created
    if task.state_enum in FINAL and transitions:
        result.until = transitions[-1]["hour"]
    state = task.state_enum
    if state in FINAL or state in PAUSED:
        return result
    # Only counts what is in the current run: a subagent cut off by a shutdown is not still
    # active.
    current_start = working[-1]["hour"] if working else task.created
    events = await tasks.db.everything(
        "SELECT kind, summary, data FROM events WHERE task_id=? AND hour >= ? "
        "AND kind IN ('start_subagent', 'end_subagent', 'tool') ORDER BY id",
        (task.id, current_start),
    )
    active: dict[str, str] = {}
    last: dict[str, Any] | None = None
    for event in events:
        event["data"] = from_json(event["data"])
        data = event["data"] or {}
        if event["kind"] == "start_subagent":
            active[str(data.get("agent_id"))] = event["summary"] or "subagent"
        elif event["kind"] == "end_subagent":
            active.pop(str(data.get("agent_id")), None)
        else:
            last = event
    result.subagents = sorted(set(active.values()))
    result.now = current_phrase(state, steps, last)
    return result


def current_phrase(state: TaskState, steps: list[Step], last: dict[str, Any] | None) -> str:
    """What the task is doing right now, in one sentence."""
    fixed = {
        TaskState.RECEIVED: "Reading your request",
        TaskState.IN_TRIAGE: "Reading your request",
        TaskState.PLANNING: "Preparing the plan",
        TaskState.IN_QUEUE: "Queued, starts as soon as there is room",
        TaskState.WAITING_PLAN: "Waiting for you to approve the plan in Telegram",
        TaskState.WAITING_APPROVAL: "Waiting for your approval in Telegram",
        TaskState.REVIEWING: "Reviewing the result before delivering it",
    }
    if state in fixed:
        return fixed[state]
    in_progress = next((p for p in steps if p.state == "in_progress"), None)
    if in_progress is not None:
        return in_progress.action or in_progress.subject
    if last is not None:
        data = last["data"] or {}
        tool = str(data.get("tool") or "")
        detail = (last["summary"] or "").split(": ", 1)[-1]
        return describe_tool(tool, detail)
    return "Working"


def describe_tool(tool: str, detail: str = "") -> str:
    """Returns a short human description of a tool call, such as Reading a file."""
    name = Path(detail).name if detail else ""
    if tool == "Bash":
        return "Running a command"
    if tool == "Read":
        return f"Reading {name}" if name else "Reading a file"
    if tool == "Write":
        return f"Writing {name}" if name else "Writing a file"
    if tool in ("Edit", "NotebookEdit"):
        return f"Editing {name}" if name else "Editing a file"
    if tool in ("Glob", "Grep"):
        return "Searching the files"
    if tool == "WebSearch":
        query = detail.strip()[:60]
        return f"Searching the web for “{query}”" if query else "Searching the web"
    if tool == "WebFetch":
        return "Reading a web page"
    if tool in ("Agent", "Task"):
        kind = detail.split(":", 1)[0].strip()
        return f"Handing work to the {kind}" if kind and kind != "None" else "Delegating work"
    if tool.startswith("Task"):
        return "Organizing the steps"
    if tool == "ToolSearch":
        return "Getting tools ready"
    if tool == "Skill":
        return "Using a skill"
    if tool.startswith("mcp__claude-in-chrome__"):
        return "Using your Chrome"
    if tool.startswith("mcp__aide__"):
        own = tool.removeprefix("mcp__aide__")
        for prefix, text in (
            ("gmail", "Working on your email"),
            ("calendar", "Checking your calendar"),
            ("browser", "Browsing the web"),
            ("memory", "Checking memory"),
            ("history", "Checking the history"),
            ("telegram", "Messaging you on Telegram"),
            ("schedule", "Setting up a scheduled task"),
            ("cancel_scheduled_task", "Canceling a scheduled task"),
        ):
            if own.startswith(prefix):
                return text
    return "Working"
