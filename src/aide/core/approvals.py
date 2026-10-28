"""Level 2 approvals.

- Persistent in SQLite and without expiry: the wait has no time limit.
- Reusable: if the program restarts and the agent asks for the same action again (same key), the
  existing approval is reused instead of asking again.
- In batches: a single message with Approve all, Review one by one and Cancel.
- Decided only in Telegram: the panel shows them but cannot resolve them.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from aide.bot.channel import Button, Channel
from aide.bot.formatting import bold, esc, truncate
from aide.core.db import Database, from_json, now, to_date, to_json
from aide.core.states import IN_PROGRESS, TaskState
from aide.core.tasks import TaskRepository

log = logging.getLogger(__name__)

KIND_TITLES = {
    "email": "Send email",
    "email_batch": "Send emails",
    "event": "Calendar event",
    "bash": "Terminal command",
    "file": "File access",
    "chrome": "Your Chrome",
    "chrome_send": "Send from your Chrome",
    "browser": "Browser",
    "scheduled_task": "Scheduled task",
    "mcp": "External tool",
    "network": "Network connection",
    "web": "Web",
    "general": "Action",
}
# Who resolved an approval, as shown (the key is stored in the database).
RESOLVED_BY = {"batch": "batch", "one_to_one": "one by one", "previous": "earlier"}
MAX_DETAIL = 3000


def approval_key(task_id: int | None, tool: str, entry: dict[str, Any]) -> str:
    normal = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(f"{task_id}|{tool}|{normal}".encode()).hexdigest()[:32]


@dataclass
class Request:
    """An approval request for a tool action, with its detail, level and fingerprint."""

    task_id: int | None
    tool: str
    entry: dict[str, Any]
    kind: str
    detail: str
    level: int = 2
    external_reason: str | None = None
    screenshot: Path | None = None
    # Fingerprint of the exact content that was approved (for example, recipients and text of a
    # draft). The tool recomputes it before running and it must match.
    fingerprint: str | None = None

    @property
    def key(self) -> str:
        return approval_key(self.task_id, self.tool, self.entry)


@dataclass
class Resolution:
    """The outcome of an approval: whether approved, its id and who decided."""

    approved: bool
    approval_id: int
    by: str = ""


class ApprovalManager:
    """Manages the user's approval requests, stored in the database."""

    def __init__(
        self,
        db: Database,
        tasks: TaskRepository,
        channel: Callable[[], Channel | None],
        reminder_hours: float = 4,
        text_limit: int = 3500,
    ) -> None:
        self.db = db
        self.tasks = tasks
        self._channel = channel
        self.reminder_hours = reminder_hours
        self.text_limit = text_limit
        self._future: dict[int, asyncio.Future[bool]] = {}
        self._batches: dict[str, asyncio.Future[str]] = {}
        self._waits_by_task: dict[int, int] = {}
        self._state_before: dict[int, TaskState] = {}

    @property
    def channel(self) -> Channel | None:
        return self._channel()

    # --- individual request ----------------------------------------------------------

    async def request(self, s: Request) -> Resolution:
        """Requests approval for one action, reusing an identical earlier approval."""
        existing = await self.db.one(
            "SELECT * FROM approvals WHERE key=? AND state IN ('approved', 'pending') "
            "ORDER BY id DESC LIMIT 1",
            (s.key,),
        )
        if (
            existing
            and existing["state"] == "approved"
            and not existing["comment"]
            and existing["fingerprint"] == s.fingerprint
        ):
            # Approved before a restart, still unused and for the exact same content.
            await self.mark_used(existing["id"])
            await self.tasks.event(
                s.task_id,
                "approval_reused",
                s.detail[:300],
                {"approval": existing["id"]},
            )
            return Resolution(True, existing["id"], "previous")
        await self._enter_wait(s.task_id)
        try:
            if existing and existing["state"] == "pending":
                approval_id = existing["id"]
            else:
                approval_id = await self._create(s)
                await self._send_request(approval_id, s)
            approved = await self._wait(approval_id)
        finally:
            await self._exit_wait(s.task_id)
        if approved:
            await self.mark_used(approval_id)
        row = await self.db.one("SELECT resolved_by FROM approvals WHERE id=?", (approval_id,))
        return Resolution(approved, approval_id, (row or {}).get("resolved_by") or "")

    async def _create(self, s: Request, batch: str | None = None) -> int:
        return await self.db.run(
            "INSERT INTO approvals(task_id, batch, kind, tool, entry, detail, level, "
            "external_reason, state, created, key, fingerprint) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
            (
                s.task_id,
                batch,
                s.kind,
                s.tool,
                to_json(s.entry),
                s.detail,
                s.level,
                s.external_reason,
                now(),
                s.key,
                s.fingerprint,
            ),
        )

    def _request_text(self, approval_id: int, s: Request, reminder: bool = False) -> str:
        title = KIND_TITLES.get(s.kind, "Action")
        header = "Reminder: approval still pending" if reminder else "I need your approval"
        task = f" (task {s.task_id})" if s.task_id else ""
        parts = [f"{bold(header)}{esc(task)}", bold(title)]
        parts.append(esc(truncate(s.detail, MAX_DETAIL)))
        if s.external_reason:
            parts.append(
                bold("Warning")
                + esc(
                    f". This action seems to be prompted by external content: {s.external_reason}"
                )
            )
        parts.append(esc(f"Reference #{approval_id}"))
        return "\n\n".join(parts)

    async def _send_request(self, approval_id: int, s: Request) -> None:
        channel = self.channel
        if channel is None:
            log.warning("no channel to ask for approval %s", approval_id)
            return
        buttons = [
            [
                Button("Approve", f"apr:{approval_id}:yes"),
                Button("Reject", f"apr:{approval_id}:no"),
            ]
        ]
        text = self._request_text(approval_id, s)
        if s.screenshot and Path(s.screenshot).exists():
            await channel.send_image(Path(s.screenshot), "Screenshot of what will be sent")
        if len(s.detail) > MAX_DETAIL:
            path = await self._detail_to_file(approval_id, s)
            await channel.send_file(path, "Full details of the action")
        message_id = await channel.send(text, buttons=buttons)
        if message_id:
            await self.db.run(
                "UPDATE approvals SET message_id=? WHERE id=?", (message_id, approval_id)
            )
        await self.tasks.link_message(message_id, s.task_id, "approval")
        await self.tasks.event(
            s.task_id,
            "approval_requested",
            s.detail[:500],
            {"approval": approval_id, "kind": s.kind, "external": s.external_reason},
        )

    async def _detail_to_file(self, approval_id: int, s: Request) -> Path:
        folder = Path(self.db.path).parent / "approvals"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"approval-{approval_id}.txt"
        path.write_text(s.detail, encoding="utf-8")
        return path

    async def _wait(self, approval_id: int) -> bool:
        future = self._future.get(approval_id)
        if future is None or future.done():
            future = asyncio.get_running_loop().create_future()
            self._future[approval_id] = future
        # It may have been resolved (from the panel) between creation and the wait.
        row = await self.db.one("SELECT state FROM approvals WHERE id=?", (approval_id,))
        if row and row["state"] != "pending" and not future.done():
            future.set_result(row["state"] == "approved")
        try:
            return await future
        finally:
            self._future.pop(approval_id, None)

    async def _enter_wait(self, task_id: int | None) -> None:
        if task_id is None:
            return
        n = self._waits_by_task.get(task_id, 0)
        self._waits_by_task[task_id] = n + 1
        if n == 0:
            task = await self.tasks.get(task_id)
            if (
                task
                and task.state_enum in IN_PROGRESS
                and task.state_enum != TaskState.WAITING_APPROVAL
            ):
                self._state_before[task_id] = task.state_enum
                await self.tasks.change_state(
                    task_id, TaskState.WAITING_APPROVAL, "approval requested"
                )

    async def _exit_wait(self, task_id: int | None) -> None:
        if task_id is None:
            return
        n = max(0, self._waits_by_task.get(task_id, 1) - 1)
        self._waits_by_task[task_id] = n
        if n == 0:
            previous = self._state_before.pop(task_id, None)
            task = await self.tasks.get(task_id)
            if previous and task and task.state_enum == TaskState.WAITING_APPROVAL:
                await self.tasks.change_state(task_id, previous, "approval resolved")

    # --- resolution ------------------------------------------------------------------

    async def resolve(self, approval_id: int, approved: bool, by: str) -> bool:
        """Records an approval as approved or rejected if pending; returns success."""
        row = await self.db.one("SELECT * FROM approvals WHERE id=?", (approval_id,))
        if row is None or row["state"] != "pending":
            return False
        state = "approved" if approved else "rejected"
        await self.db.run(
            "UPDATE approvals SET state=?, resolved=?, resolved_by=? WHERE id=? AND state='pending'",
            (state, now(), by, approval_id),
        )
        await self.tasks.event(
            row["task_id"],
            f"approval_{state}",
            row["detail"][:300],
            {"approval": approval_id, "by": by},
        )
        future = self._future.get(approval_id)
        if future is not None and not future.done():
            future.set_result(approved)
        channel = self.channel
        if channel is not None and row["message_id"]:
            result = "Approved" if approved else "Rejected"
            try:
                s = Request(
                    row["task_id"],
                    row["tool"],
                    from_json(row["entry"]) or {},
                    row["kind"],
                    row["detail"],
                    row["level"],
                    row["external_reason"],
                )
                await channel.edit(
                    row["message_id"],
                    self._request_text(approval_id, s)
                    + "\n\n"
                    + bold(f"{result} ({RESOLVED_BY.get(by, by)})"),
                )
            except Exception:
                log.debug("couldn't edit the message of approval %s", approval_id, exc_info=True)
        return True

    async def mark_used(self, approval_id: int) -> None:
        await self.db.run("UPDATE approvals SET comment='used' WHERE id=?", (approval_id,))

    async def cancel_from_task(self, task_id: int) -> None:
        """Cancels the pending approvals of a task and releases their waiters."""
        rows = await self.db.everything(
            "SELECT id FROM approvals WHERE task_id=? AND state='pending'", (task_id,)
        )
        for row in rows:
            await self.db.run(
                "UPDATE approvals SET state='cancelled', resolved=? WHERE id=?",
                (now(), row["id"]),
            )
            future = self._future.get(row["id"])
            if future is not None and not future.done():
                future.set_result(False)
        for batch_id, future in list(self._batches.items()):
            if not future.done():
                row = await self.db.one(
                    "SELECT task_id FROM approvals WHERE batch=? LIMIT 1", (batch_id,)
                )
                if row and row["task_id"] == task_id:
                    future.set_result("cancel")

    # --- batches ---------------------------------------------------------------------

    async def request_batch(
        self, task_id: int | None, title: str, items: list[Request]
    ) -> list[Resolution]:
        """Requests approval for several actions together, or one if only one."""
        if len(items) == 1:
            return [await self.request(items[0])]
        batch = uuid.uuid4().hex[:12]
        ids = [await self._create(s, batch) for s in items]
        lines = [
            bold("I need your approval") + esc(f" (task {task_id})" if task_id else ""),
            bold(title),
        ]
        for n, s in enumerate(items, 1):
            lines.append(esc(f"{n}. {truncate(s.detail, 600)}"))
        external = [s.external_reason for s in items if s.external_reason]
        if external:
            lines.append(
                bold("Warning")
                + esc(
                    f". Part of the batch seems to be prompted by external content: {external[0]}"
                )
            )
        lines.append(esc(f"References #{ids[0]} to #{ids[-1]}"))
        buttons = [
            [
                Button("Approve all", f"batch:{batch}:everything"),
                Button("Review one by one", f"batch:{batch}:one"),
            ],
            [Button("Cancel", f"batch:{batch}:cancel")],
        ]
        future: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._batches[batch] = future
        await self._enter_wait(task_id)
        try:
            return await self._decide_batch(
                task_id, title, batch, ids, items, lines, buttons, future
            )
        finally:
            self._batches.pop(batch, None)
            await self._exit_wait(task_id)

    async def _decide_batch(
        self, task_id, title, batch, ids, items, lines, buttons, future
    ) -> list[Resolution]:
        channel = self.channel
        if channel is not None:
            message_id = await channel.send("\n\n".join(lines), buttons=buttons)
            await self.tasks.link_message(message_id, task_id, "approval")
        await self.tasks.event(
            task_id, "batch_approval_requested", title, {"batch": batch, "ids": ids}
        )
        decision = await future
        if decision == "everything":
            for i in ids:
                await self.resolve(i, True, "batch")
                await self.mark_used(i)
            return [Resolution(True, i, "batch") for i in ids]
        if decision == "cancel":
            for i in ids:
                await self.resolve(i, False, "batch")
            return [Resolution(False, i, "batch") for i in ids]
        # Review one by one: each item with its own message.
        results = []
        for i, s in zip(ids, items, strict=True):
            await self._send_request(i, s)
            approved = await self._wait(i)
            if approved:
                await self.mark_used(i)
            results.append(Resolution(approved, i, "one_to_one"))
        return results

    async def resolve_batch(self, batch: str, decision: str) -> bool:
        """Resolves a batch with everything, one or cancel; returns False if invalid."""
        future = self._batches.get(batch)
        if future is None or future.done() or decision not in ("everything", "one", "cancel"):
            return False
        future.set_result(decision)
        return True

    # --- query and reminders -----------------------------------------------------------

    async def pending(self) -> list[dict[str, Any]]:
        return await self.db.everything("SELECT * FROM approvals WHERE state='pending' ORDER BY id")

    async def remember_pending(self) -> int:
        """Resends a reminder of the pending approvals every `reminder_hours`."""
        channel = self.channel
        if channel is None:
            return 0
        sent = 0
        limit = datetime.now(UTC) - timedelta(hours=self.reminder_hours)
        for row in await self.pending():
            last = to_date(row["reminded"]) or to_date(row["created"])
            if last and last > limit:
                continue
            s = Request(
                row["task_id"],
                row["tool"],
                from_json(row["entry"]) or {},
                row["kind"],
                row["detail"],
                row["level"],
                row["external_reason"],
            )
            buttons = [
                [
                    Button("Approve", f"apr:{row['id']}:yes"),
                    Button("Reject", f"apr:{row['id']}:no"),
                ]
            ]
            message_id = await channel.send(
                self._request_text(row["id"], s, reminder=True), buttons=buttons
            )
            await self.tasks.link_message(message_id, row["task_id"], "approval")
            await self.db.run("UPDATE approvals SET reminded=? WHERE id=?", (now(), row["id"]))
            sent += 1
        return sent


async def current_approval(
    db: Database, key: str, fingerprint: str | None = None
) -> dict[str, Any] | None:
    """Approval granted for this exact action (double check in the tools)."""
    row = await db.one(
        "SELECT * FROM approvals WHERE key=? AND state='approved' ORDER BY id DESC LIMIT 1",
        (key,),
    )
    if row is None:
        return None
    if fingerprint is not None and row.get("fingerprint") != fingerprint:
        return None
    return row
