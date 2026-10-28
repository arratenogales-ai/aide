"""Log of external actions with an idempotency key.

Key = (task, type, recipient, content hash). Before executing an action the log
is consulted; a `done` action is never repeated and returns its saved result.
An action left `in_progress` by an interruption is checked against the service before deciding.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from aide.core.db import Database, from_json, now, to_json


def content_hash(content: Any) -> str:
    """Returns the SHA-256 hash of a text or of JSON-serialized content."""
    text = (
        content
        if isinstance(content, str)
        else json.dumps(content, sort_keys=True, ensure_ascii=False, default=str)
    )
    return hashlib.sha256(text.encode()).hexdigest()


def action_key(task_id: int | None, kind: str, recipient: str, content: Any) -> str:
    base = f"{task_id}|{kind}|{recipient.strip().lower()}|{content_hash(content)}"
    return hashlib.sha256(base.encode()).hexdigest()[:40]


@dataclass
class Action:
    """An external action record: key, task, kind, recipient, state and result."""

    key: str
    task_id: int | None
    kind: str
    recipient: str | None
    state: str
    result: Any


class ActionLog:
    """A database-backed log of external actions."""

    def __init__(self, db: Database) -> None:
        self.db = db

    async def get(self, key: str) -> Action | None:
        """Returns the action stored under a key, or None if absent."""
        row = await self.db.one("SELECT * FROM external_actions WHERE key=?", (key,))
        if row is None:
            return None
        return Action(
            row["key"],
            row["task_id"],
            row["kind"],
            row["recipient"],
            row["state"],
            from_json(row["result"]),
        )

    async def start(
        self, key: str, task_id: int | None, kind: str, recipient: str, content: Any
    ) -> Action | None:
        """Marks the action as in progress. Returns the existing one if it was already registered (done or in progress)."""
        existing = await self.get(key)
        if existing is not None and existing.state in ("done", "in_progress"):
            return existing
        moment = now()
        await self.db.run(
            "INSERT INTO external_actions(key, task_id, kind, recipient, content_hash, state, created, updated) "
            "VALUES (?, ?, ?, ?, ?, 'in_progress', ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET state='in_progress', updated=excluded.updated",
            (key, task_id, kind, recipient, content_hash(content), moment, moment),
        )
        return None

    async def complete(self, key: str, result: Any) -> None:
        """Marks an action as done and stores its result."""
        await self.db.run(
            "UPDATE external_actions SET state='done', result=?, updated=? WHERE key=?",
            (to_json(result), now(), key),
        )

    async def fail(self, key: str, error: str) -> None:
        """Marks an action as failed and stores the error."""
        await self.db.run(
            "UPDATE external_actions SET state='failed', result=?, updated=? WHERE key=?",
            (to_json({"error": error}), now(), key),
        )

    async def from_task(self, task_id: int) -> list[Action]:
        """Returns the actions of a task, ordered by creation."""
        rows = await self.db.everything(
            "SELECT * FROM external_actions WHERE task_id=? ORDER BY created", (task_id,)
        )
        return [
            Action(
                f["key"],
                f["task_id"],
                f["kind"],
                f["recipient"],
                f["state"],
                from_json(f["result"]),
            )
            for f in rows
        ]

    async def done_descriptions(self, task_id: int) -> list[str]:
        """Describes a task's finished or interrupted actions as short texts."""
        texts = []
        for a in await self.from_task(task_id):
            if a.state == "done":
                texts.append(f"{a.kind} to {a.recipient or '-'} (done)")
            elif a.state == "in_progress":
                texts.append(
                    f"{a.kind} to {a.recipient or '-'} (cut off while running; it may be done)"
                )
        return texts
