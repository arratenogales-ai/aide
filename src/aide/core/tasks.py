"""Task repository: states, timeline, files, messages and history."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, fields
from pathlib import Path
from typing import Any

from aide.common.paths import Paths
from aide.core.db import Database, from_json, fts_query, now, to_json
from aide.core.states import TaskState, check_transition

log = logging.getLogger(__name__)


@dataclass
class Task:
    """A task record with its request, state, size and plan and browser needs."""

    id: int
    created: str
    updated: str
    origin: str
    kind: str
    request: str
    title: str | None
    state: str
    size: str | None
    needs_plan: int
    needs_browser: int
    external_actions: int
    model: str | None
    forced_model: str | None
    phase: str | None
    plan: str | None
    session_id: str | None
    session_used: str | None
    turn_cap: int
    usd_cap: float
    turns_used: int
    usd_used: float
    result: str | None
    summary: str | None
    error: str | None
    reviews: int
    parent_task: int | None
    scheduled_task_id: int | None
    pause_until: str | None
    pause_reason: str | None
    previous_state: str | None
    next_step: str | None
    final_folder: str | None = None
    project: str | None = None
    token_cap: int | None = None

    @property
    def state_enum(self) -> TaskState:
        return TaskState(self.state)

    @property
    def remaining_turns(self) -> int:
        return max(0, self.turn_cap - self.turns_used)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.usd_cap - self.usd_used)

    @property
    def short_name(self) -> str:
        text = (self.title or self.request or "").strip().replace("\n", " ")
        return text if len(text) <= 60 else text[:57] + "..."


_FIELDS = {f.name for f in fields(Task)}


def _to_task(row: dict[str, Any] | None) -> Task | None:
    if row is None:
        return None
    return Task(**{k: row.get(k) for k in _FIELDS})


def task_from_row(row: dict[str, Any]) -> Task:
    """Task from a row of `tasks` (for queries with extra columns)."""
    task = _to_task(row)
    assert task is not None
    return task


class TaskRepository:
    """Stores and reads tasks, their state changes, events, steps, files and history."""

    def __init__(self, db: Database, paths: Paths | None = None) -> None:
        self.db = db
        self.paths = paths
        # Every task created or changed state is notified (this is how the engine tells what
        # is from this startup apart from what was left half done before).
        self.observer: Callable[[int], None] | None = None

    async def create(
        self,
        request: str,
        *,
        origin: str = "telegram",
        kind: str = "task",
        state: TaskState = TaskState.RECEIVED,
        title: str | None = None,
        parent_task: int | None = None,
        scheduled_task_id: int | None = None,
    ) -> Task:
        """Creates a task in the database and returns it."""
        moment = now()
        task_id = await self.db.run(
            "INSERT INTO tasks(created, updated, origin, kind, request, title, state, "
            "parent_task, scheduled_task_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                moment,
                moment,
                origin,
                kind,
                request,
                title,
                state.value,
                parent_task,
                scheduled_task_id,
            ),
        )
        await self.db.run(
            "INSERT INTO transitions(task_id, since, until, reason, hour) VALUES (?, ?, ?, ?, ?)",
            (task_id, None, state.value, "created", moment),
        )
        if self.observer is not None:
            self.observer(task_id)
        if self.paths is not None:
            old = self.paths.reserve_old_folder(task_id)
            if old is not None:
                log.warning(
                    "the folder for task %s belonged to another database; moved to %s",
                    task_id,
                    old,
                )
        task = await self.get(task_id)
        assert task is not None
        return task

    async def get(self, task_id: int) -> Task | None:
        return _to_task(await self.db.one("SELECT * FROM tasks WHERE id=?", (task_id,)))

    async def require(self, task_id: int) -> Task:
        """Returns the task with that id, or raises KeyError if it does not exist."""
        task = await self.get(task_id)
        if task is None:
            raise KeyError(f"task {task_id} does not exist")
        return task

    async def change_state(self, task_id: int, until: TaskState, reason: str = "") -> Task:
        """Moves a task to a new state if the transition is allowed, and records it."""
        task = await self.require(task_id)
        since = task.state_enum
        if since == until:
            return task
        check_transition(since, until)
        if self.observer is not None:
            self.observer(task_id)
        moment = now()
        await self.db.run_several(
            [
                (
                    "UPDATE tasks SET state=?, updated=?, previous_state=? WHERE id=?",
                    (until.value, moment, since.value, task_id),
                ),
                (
                    "INSERT INTO transitions(task_id, since, until, reason, hour) VALUES (?, ?, ?, ?, ?)",
                    (task_id, since.value, until.value, reason, moment),
                ),
            ]
        )
        log.info("task %s: %s -> %s (%s)", task_id, since.value, until.value, reason)
        return await self.require(task_id)

    async def update(self, task_id: int, **fields: Any) -> Task:
        """Updates the given task fields, but never its state or id."""
        unknown = set(fields) - _FIELDS
        if unknown or "state" in fields or "id" in fields:
            raise ValueError(f"fields that cannot be updated: {unknown or {'state'}}")
        if fields:
            fields["updated"] = now()
            assignments = ", ".join(f"{k}=?" for k in fields)
            await self.db.run(
                f"UPDATE tasks SET {assignments} WHERE id=?", (*fields.values(), task_id)
            )
        return await self.require(task_id)

    async def add_consumption(self, task_id: int, usd: float, turns: int) -> Task:
        """Adds cost and turns to the task's running totals."""
        await self.db.run(
            "UPDATE tasks SET usd_used = usd_used + ?, turns_used = turns_used + ?, "
            "updated=? WHERE id=?",
            (usd, turns, now(), task_id),
        )
        return await self.require(task_id)

    async def list_items(
        self,
        *,
        states: list[TaskState] | None = None,
        kind: str | None = None,
        limit: int = 50,
        since_id: int | None = None,
    ) -> list[Task]:
        """Lists tasks, optionally filtered by state and kind, with a limit."""
        conditions, parameters = [], []
        if states:
            conditions.append(f"state IN ({', '.join('?' for _ in states)})")
            parameters += [e.value for e in states]
        if kind:
            conditions.append("kind=?")
            parameters.append(kind)
        if since_id:
            conditions.append("id < ?")
            parameters.append(since_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = await self.db.everything(
            f"SELECT * FROM tasks {where} ORDER BY id DESC LIMIT ?", (*parameters, limit)
        )
        return [t for t in map(_to_task, rows) if t is not None]

    async def transitions(self, task_id: int) -> list[dict[str, Any]]:
        """Returns the recorded state transitions of a task, in order."""
        return await self.db.everything(
            "SELECT * FROM transitions WHERE task_id=? ORDER BY id", (task_id,)
        )

    # --- timeline -----

    async def event(
        self, task_id: int | None, kind: str, summary: str = "", data: Any = None
    ) -> int:
        """Records an event for a task, or a general one, with a short summary and data."""
        return await self.db.run(
            "INSERT INTO events(task_id, hour, kind, summary, data) VALUES (?, ?, ?, ?, ?)",
            (task_id, now(), kind, summary[:2000], to_json(data) if data is not None else None),
        )

    async def events(
        self, task_id: int, kinds: list[str] | None = None, limit: int = 2000
    ) -> list[dict[str, Any]]:
        """Returns a task's events, optionally of given kinds, with their data decoded."""
        sql = "SELECT * FROM events WHERE task_id=?"
        parameters: list[Any] = [task_id]
        if kinds:
            sql += f" AND kind IN ({', '.join('?' for _ in kinds)})"
            parameters += kinds
        rows = await self.db.everything(sql + " ORDER BY id LIMIT ?", (*parameters, limit))
        for row in rows:
            row["data"] = from_json(row["data"])
        return rows

    # --- work plan steps (the orchestrator's TaskCreate and TaskUpdate) --------

    async def create_step(
        self, task_id: int, session: str, step: str, subject: str, action: str | None
    ) -> None:
        """Creates a step of a task, or updates its subject and action if it exists."""
        moment = now()
        await self.db.run(
            "INSERT INTO steps(task_id, session, step, subject, action, state, created, updated) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?) ON CONFLICT(task_id, session, step) DO UPDATE "
            "SET subject=excluded.subject, action=excluded.action, updated=excluded.updated",
            (task_id, session, step, subject, action, moment, moment),
        )

    async def update_step(
        self, task_id: int, session: str, step: str, **fields: str | None
    ) -> None:
        """Updates the state, subject or action of a task step."""
        changes = {k: v for k, v in fields.items() if k in ("state", "subject", "action") and v}
        if not changes:
            return
        assignments = ", ".join(f"{k}=?" for k in changes)
        await self.db.run(
            f"UPDATE steps SET {assignments}, updated=? WHERE task_id=? AND session=? AND step=?",
            (*changes.values(), now(), task_id, session, step),
        )

    async def steps(self, task_id: int) -> list[dict[str, Any]]:
        """Steps of the most recent plan (from the last session that created steps), without the deleted ones."""
        session = await self.db.value(
            "SELECT session FROM steps WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)
        )
        if session is None:
            return []
        return await self.db.everything(
            "SELECT * FROM steps WHERE task_id=? AND session=? AND state != 'deleted' ORDER BY id",
            (task_id, session),
        )

    # --- files ------

    async def log_file(self, task_id: int, path: Path, kind: str) -> None:
        """Records a file belonging to a task, ignoring duplicates."""
        await self.db.run(
            "INSERT OR IGNORE INTO files(task_id, path, kind, hour) VALUES (?, ?, ?, ?)",
            (task_id, str(path), kind, now()),
        )

    async def files(self, task_id: int, kind: str | None = None) -> list[dict[str, Any]]:
        """Returns the files recorded for a task, optionally of one kind."""
        if kind:
            return await self.db.everything(
                "SELECT * FROM files WHERE task_id=? AND kind=? ORDER BY id", (task_id, kind)
            )
        return await self.db.everything(
            "SELECT * FROM files WHERE task_id=? ORDER BY id", (task_id,)
        )

    # --- Telegram messages linked to tasks ----------

    async def link_message(self, message_id: int | None, task_id: int | None, address: str) -> None:
        """Links a Telegram message to a task so replies can be traced back."""
        if not message_id or message_id <= 0:
            return
        await self.db.run(
            "INSERT OR REPLACE INTO telegram_messages(message_id, task_id, address, hour) "
            "VALUES (?, ?, ?, ?)",
            (message_id, task_id, address, now()),
        )

    async def task_from_message(self, message_id: int) -> int | None:
        """Returns the task id linked to a Telegram message, if any."""
        return await self.db.value(
            "SELECT task_id FROM telegram_messages WHERE message_id=?", (message_id,)
        )

    # --- history (FTS5) -----

    async def index_history(self, task: Task, files: list[str]) -> None:
        """Indexes a task's request, summary and files for history search."""
        await self.db.run_several(
            [
                ("DELETE FROM history_fts WHERE rowid=?", (task.id,)),
                (
                    "INSERT INTO history_fts(rowid, request, summary, files) VALUES (?, ?, ?, ?)",
                    (task.id, task.request, task.summary or "", "\n".join(files)),
                ),
            ]
        )

    async def search_history(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Searches past tasks by text and returns matches with a snippet."""
        return await self.db.everything(
            "SELECT t.id, t.created, t.state, t.request, t.summary, "
            "snippet(history_fts, -1, '[', ']', '...', 12) AS fragment "
            "FROM history_fts JOIN tasks t ON t.id = history_fts.rowid "
            "WHERE history_fts MATCH ? ORDER BY bm25(history_fts) LIMIT ?",
            (fts_query(query), limit),
        )
