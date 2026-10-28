"""Scheduler tools.

`schedule_task`, `schedule_script` and `cancel_scheduled_task` need the user's approval
(permissions.yaml), which the system requests with the exact detail (what, when, next
run and what happens if the Mac is off; for scripts, the exact command). The tool checks
the approval again before writing.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import ToolAnnotations, tool

from aide.core import scripts
from aide.core.approvals import approval_key, current_approval
from aide.core.context import SessionContext
from aide.core.db import now
from aide.core.hooks import note_progress
from aide.core.idempotency import action_key
from aide.core.permissions import Detail, formatters
from aide.core.policy import Decision
from aide.core.scheduler import (
    KINDS,
    POLICIES,
    SHORT_MONTHS,
    InvalidTiming,
    describe,
    describe_policy,
    interpret,
    next_step,
)
from aide.tools.server import error, log_factory, text


def _date(moment: datetime) -> str:
    """“Oct 28, 2026 09:00”."""
    return f"{SHORT_MONTHS[moment.month - 1]} {moment.day}, {moment.year} {moment:%H:%M}"


SCHEMA_WHEN = {
    "type": "object",
    "description": (
        "kind one_time (date YYYY-MM-DDTHH:MM), daily (hour HH:MM), weekly (days as lowercase "
        "English weekday names such as monday or wednesday, and hour), monthly (day 1-31 and hour) "
        "or every_hours (hours). Times are in the user's local time."
    ),
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "date": {"type": "string"},
        "hour": {"type": "string"},
        "days": {"type": "array", "items": {"type": "string"}},
        "day": {"type": "integer"},
        "hours": {"type": "integer"},
    },
    "required": ["kind"],
}


@log_factory
def scheduler_tools(ses: SessionContext) -> list[Any]:
    """Builds the tools to create and manage scheduled tasks."""
    core = ses.core
    zone = ZoneInfo(core.config.time_zone)
    settings = core.config.settings.scheduler

    def prepare(entry: dict[str, Any]) -> tuple[str, Any, str, datetime | None]:
        description = " ".join(str(entry.get("description", "")).split())
        if not description:
            raise InvalidTiming("the task description is missing")
        timing = interpret(entry.get("timing"), zone)
        policy = str(entry.get("policy_if_shutdown") or settings.default_policy)
        if policy not in POLICIES:
            raise InvalidTiming(f"invalid policy: {policy} (use {', '.join(POLICIES)})")
        return description, timing, policy, next_step(timing, datetime.now(zone), zone)

    def program_text(description: str, timing: Any, policy: str, next_run: datetime | None) -> str:
        lines = [
            f"Task: {description}",
            f"When: {describe(timing)}",
            f"Next run: {_date(next_run.astimezone(zone))}" if next_run else "Next run: none",
            f"If the Mac is off at that time: {describe_policy(policy, settings.window_hours)}",
        ]
        return "\n".join(lines)

    async def format_schedule(_ses, _h, entry: dict[str, Any], _d: Decision) -> Detail:
        return Detail("scheduled_task", "Schedule a task\n" + program_text(*prepare(entry)))

    def prepare_script(entry: dict[str, Any]) -> tuple[scripts.Command, str, Any, str, Any]:
        command = scripts.validate(
            core.paths, str(entry.get("project", "")), str(entry.get("command", ""))
        )
        silence = str(entry.get("silence") or "").strip()
        timing = interpret(entry.get("timing"), zone)
        policy = str(entry.get("policy_if_shutdown") or settings.default_policy)
        if policy not in POLICIES:
            raise InvalidTiming(f"invalid policy: {policy} (use {', '.join(POLICIES)})")
        return command, silence, timing, policy, next_step(timing, datetime.now(zone), zone)

    async def script_format(_ses, _h, entry: dict[str, Any], _d: Decision) -> Detail:
        command, silence, timing, policy, next_run = prepare_script(entry)
        lines = [
            "Schedule a script task (no model)",
            scripts.describe(command, silence),
            f"When: {describe(timing)}",
            f"Next run: {_date(next_run.astimezone(zone))}" if next_run else "Next run: none",
            f"If the Mac is off at that time: {describe_policy(policy, settings.window_hours)}",
        ]
        return Detail("scheduled_task", "\n".join(lines))

    async def format_cancel(_ses, _h, entry: dict[str, Any], _d: Decision) -> Detail:
        row = await core.db.one("SELECT * FROM scheduled_tasks WHERE id=?", (int(entry["id"]),))
        if row is None:
            raise ValueError(f"scheduled task {entry['id']} does not exist")
        return Detail(
            "scheduled_task",
            f"Cancel scheduled task {row['id']}\nTask: {row['description']}\n"
            f"When: {row['timing_text'] or row['timing']}",
        )

    formatters(ses).update(
        {
            "mcp__aide__schedule_task": format_schedule,
            "mcp__aide__schedule_script": script_format,
            "mcp__aide__cancel_scheduled_task": format_cancel,
        }
    )

    async def approved(tool_: str, entry: dict[str, Any]) -> bool:
        key = approval_key(ses.task_id, f"mcp__aide__{tool_}", entry)
        return await current_approval(core.db, key) is not None

    @tool(
        "schedule_task",
        "Schedules a task to run later, once or on a recurring basis. It needs the user's "
        "approval, which the system asks for with the details. policy_if_shutdown decides what "
        "happens if the Mac was off at that time: run_if_recent (default), "
        "run_always or skip.",
        {
            "type": "object",
            "properties": {
                "description": {
                    "type": "string",
                    "description": "What the agent must do, written as a complete request",
                },
                "timing": SCHEMA_WHEN,
                "policy_if_shutdown": {"type": "string", "enum": list(POLICIES)},
            },
            "required": ["description", "timing"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    )
    async def schedule_task(args: dict[str, Any]) -> dict[str, Any]:
        try:
            description, timing, policy, next_run = prepare(args)
        except (InvalidTiming, ValueError) as e:
            return error(f"Cannot schedule it: {e}")
        if next_run is None:
            return error("That date has already passed: there would be no run.")
        if not await approved("schedule_task", args):
            return error("Scheduling a task needs the user's approval and it does not have it.")
        content = {"description": description, "timing": timing.to_json(), "policy": policy}
        key = action_key(ses.task_id, "schedule", "", content)
        actions = core.actions
        existing = await actions.start(key, ses.task_id, "schedule", "scheduler", content)
        if existing is not None and existing.state == "done":
            return text(
                {"id": (existing.result or {}).get("id"), "note": "It was already scheduled."}
            )
        scheduled_task_id = await core.db.run(
            "INSERT INTO scheduled_tasks(description, timing, policy, active, created, origin_task, "
            "next_run, timing_text) VALUES (?, ?, ?, 1, ?, ?, ?, ?)",
            (
                description,
                timing.to_json(),
                policy,
                now(),
                ses.task_id,
                next_run.astimezone(UTC).isoformat(timespec="seconds"),
                describe(timing),
            ),
        )
        await actions.complete(key, {"id": scheduled_task_id})
        note_progress(ses.folder, f"Scheduled task {scheduled_task_id}: {describe(timing)}.")
        await core.tasks.event(
            ses.task_id,
            "scheduled_task",
            description[:200],
            {"id": scheduled_task_id, "timing": describe(timing)},
        )
        return text(
            {
                "id": scheduled_task_id,
                "timing": describe(timing),
                "next_run": next_run.astimezone(zone).isoformat(timespec="minutes"),
            }
        )

    @tool(
        "schedule_script",
        "Schedules a project script for the system to run without the model (cheaper and "
        "reliable for simple alerts). The command is an interpreter (python3, bash or sh) and a "
        "script in agent/projects/<project>/, with a 2-minute maximum and inside the sandbox. "
        "If the output is the silence word, nothing is sent; otherwise the user gets the output "
        "as is. It needs the user's approval, which the system asks for with the exact command.",
        {
            "type": "object",
            "properties": {
                "project": {"type": "string", "description": "Folder in agent/projects/"},
                "command": {
                    "type": "string",
                    "description": "For example: python3 rain_alert.py --threshold 50",
                },
                "timing": SCHEMA_WHEN,
                "silence": {
                    "type": "string",
                    "description": 'Exact output that means "nothing to report" (for example NO_RAIN)',
                },
                "policy_if_shutdown": {"type": "string", "enum": list(POLICIES)},
            },
            "required": ["project", "command", "timing"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    )
    async def schedule_script(args: dict[str, Any]) -> dict[str, Any]:
        try:
            command, silence, timing, policy, next_run = prepare_script(args)
        except (InvalidTiming, ValueError) as e:
            return error(f"Cannot schedule it: {e}")
        if next_run is None:
            return error("That date has already passed: there would be no run.")
        if not await approved("schedule_script", args):
            return error("Scheduling a script needs the user's approval and it does not have it.")
        content = {
            "project": command.project,
            "command": list(command.argv),
            "timing": timing.to_json(),
            "silence": silence,
        }
        key = action_key(ses.task_id, "schedule_script", "", content)
        existing = await core.actions.start(key, ses.task_id, "schedule", "scheduler", content)
        if existing is not None and existing.state == "done":
            return text(
                {"id": (existing.result or {}).get("id"), "note": "It was already scheduled."}
            )
        scheduled_task_id = await core.db.run(
            "INSERT INTO scheduled_tasks(description, timing, policy, active, created, origin_task, "
            "next_run, timing_text, kind, project, command, silence, pending) "
            "VALUES (?, ?, ?, 1, ?, ?, ?, ?, 'script', ?, ?, ?, 0)",
            (
                f"Script from {command.project}: {command.text()}",
                timing.to_json(),
                policy,
                now(),
                ses.task_id,
                next_run.astimezone(UTC).isoformat(timespec="seconds"),
                describe(timing),
                command.project,
                json.dumps(list(command.argv), ensure_ascii=False),
                silence,
            ),
        )
        await core.actions.complete(key, {"id": scheduled_task_id})
        note_progress(ses.folder, f"Scheduled script {scheduled_task_id}: {command.text()}.")
        await core.tasks.event(
            ses.task_id,
            "scheduled_task",
            f"script {command.text()}"[:200],
            {"id": scheduled_task_id, "timing": describe(timing)},
        )
        return text(
            {
                "id": scheduled_task_id,
                "timing": describe(timing),
                "next_run": next_run.astimezone(zone).isoformat(timespec="minutes"),
            }
        )

    @tool(
        "list_scheduled_tasks",
        "Lists the user's scheduled tasks with their frequency, next run and status.",
        {"type": "object", "properties": {}},
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def list_scheduled_tasks(args: dict[str, Any]) -> dict[str, Any]:
        rows = await core.db.everything("SELECT * FROM scheduled_tasks ORDER BY id DESC LIMIT 50")
        if not rows:
            return text("There are no scheduled tasks.")
        result = []
        for f in rows:
            next_run = (
                datetime.fromisoformat(f["next_run"]).astimezone(zone).isoformat(timespec="minutes")
                if f["next_run"]
                else None
            )
            result.append(
                {
                    "id": f["id"],
                    "kind": f.get("kind") or "model",
                    "description": f["description"],
                    "timing": f["timing_text"] or f["timing"],
                    "active": bool(f["active"]),
                    "next_run": next_run if f["active"] else None,
                    "last_run": f["last_run"],
                    "last_task": f["last_task"],
                }
            )
        return text(result)

    @tool(
        "cancel_scheduled_task",
        "Cancels a scheduled task by its number. It needs the user's approval.",
        {"type": "object", "properties": {"id": {"type": "integer"}}, "required": ["id"]},
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=False),
    )
    async def cancel_scheduled_task(args: dict[str, Any]) -> dict[str, Any]:
        try:
            scheduled_task_id = int(args["id"])
        except (TypeError, ValueError, KeyError):
            return error("Invalid scheduled task number.")
        if not await approved("cancel_scheduled_task", args):
            return error(
                "Canceling a scheduled task needs the user's approval and it does not have it."
            )
        row = await core.db.one("SELECT * FROM scheduled_tasks WHERE id=?", (scheduled_task_id,))
        if row is None:
            return error(f"Scheduled task {scheduled_task_id} does not exist.")
        await core.db.run("UPDATE scheduled_tasks SET active=0 WHERE id=?", (scheduled_task_id,))
        note_progress(ses.folder, f"Scheduled task {scheduled_task_id} canceled.")
        return text(f"Scheduled task {scheduled_task_id} canceled.")

    return [schedule_task, schedule_script, list_scheduled_tasks, cancel_scheduled_task]
