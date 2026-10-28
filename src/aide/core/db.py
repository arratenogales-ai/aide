"""SQLite database (aiosqlite, WAL mode, FTS5).

The schema is created on connecting and migrated with PRAGMA user_version.
All times are stored in ISO 8601 with UTC zone.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

VERSION_SCHEMA = 1

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    origin TEXT NOT NULL DEFAULT 'telegram',
    kind TEXT NOT NULL DEFAULT 'task',
    request TEXT NOT NULL,
    title TEXT,
    state TEXT NOT NULL,
    size TEXT,
    needs_plan INTEGER NOT NULL DEFAULT 0,
    needs_browser INTEGER NOT NULL DEFAULT 0,
    external_actions INTEGER NOT NULL DEFAULT 0,
    model TEXT,
    forced_model TEXT,
    phase TEXT,
    plan TEXT,
    session_id TEXT,
    session_used TEXT,
    turn_cap INTEGER NOT NULL DEFAULT 0,
    usd_cap REAL NOT NULL DEFAULT 0,
    turns_used INTEGER NOT NULL DEFAULT 0,
    usd_used REAL NOT NULL DEFAULT 0,
    result TEXT,
    summary TEXT,
    error TEXT,
    reviews INTEGER NOT NULL DEFAULT 0,
    parent_task INTEGER REFERENCES tasks(id),
    scheduled_task_id INTEGER,
    pause_until TEXT,
    pause_reason TEXT,
    previous_state TEXT,
    next_step TEXT,
    final_folder TEXT,
    -- project the task belongs to, if any
    project TEXT,
    -- the task's own token budget
    token_cap INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);

-- Steps of the orchestrator's work plan (TaskCreate and TaskUpdate), to show progress.
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    session TEXT NOT NULL DEFAULT '',
    step TEXT NOT NULL,
    subject TEXT NOT NULL,
    action TEXT,
    state TEXT NOT NULL,
    created TEXT NOT NULL,
    updated TEXT NOT NULL,
    UNIQUE(task_id, session, step)
);
CREATE INDEX IF NOT EXISTS idx_steps_task ON steps(task_id, id);

CREATE TABLE IF NOT EXISTS transitions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    since TEXT,
    until TEXT NOT NULL,
    reason TEXT,
    hour TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_transitions_task ON transitions(task_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    hour TEXT NOT NULL,
    kind TEXT NOT NULL,
    summary TEXT,
    data TEXT
);
CREATE INDEX IF NOT EXISTS idx_events_task ON events(task_id, id);
CREATE INDEX IF NOT EXISTS idx_events_kind ON events(kind, id);

CREATE TABLE IF NOT EXISTS telegram_messages (
    message_id INTEGER PRIMARY KEY,
    task_id INTEGER REFERENCES tasks(id),
    address TEXT NOT NULL,
    hour TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS files (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER NOT NULL REFERENCES tasks(id),
    path TEXT NOT NULL,
    kind TEXT NOT NULL,
    hour TEXT NOT NULL,
    UNIQUE(task_id, path)
);

CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER REFERENCES tasks(id),
    batch TEXT,
    kind TEXT NOT NULL,
    tool TEXT NOT NULL,
    entry TEXT NOT NULL,
    detail TEXT NOT NULL,
    level INTEGER NOT NULL,
    external_reason TEXT,
    state TEXT NOT NULL,
    created TEXT NOT NULL,
    resolved TEXT,
    resolved_by TEXT,
    reminded TEXT,
    key TEXT,
    message_id INTEGER,
    comment TEXT,
    fingerprint TEXT
);
CREATE INDEX IF NOT EXISTS idx_approvals_state ON approvals(state);
CREATE INDEX IF NOT EXISTS idx_approvals_key ON approvals(key);

CREATE TABLE IF NOT EXISTS external_actions (
    key TEXT PRIMARY KEY,
    task_id INTEGER,
    kind TEXT NOT NULL,
    recipient TEXT,
    content_hash TEXT,
    state TEXT NOT NULL,
    result TEXT,
    created TEXT NOT NULL,
    updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id INTEGER,
    hour TEXT NOT NULL,
    origin TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    cache_creation INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    turns INTEGER NOT NULL DEFAULT 0,
    web_searches INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_task ON usage(task_id);
CREATE INDEX IF NOT EXISTS idx_usage_hour ON usage(hour);

-- Last accumulated usage reported by each SDK session. When resuming, the CLI starts from that
-- accumulated figure, so differences are calculated from it and nothing is counted twice.
CREATE TABLE IF NOT EXISTS usage_sessions (
    session_id TEXT PRIMARY KEY,
    by_model TEXT NOT NULL,
    updated TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS limits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    hour TEXT NOT NULL,
    kind TEXT,
    state TEXT,
    utilization REAL,
    reset TEXT,
    data TEXT
);

CREATE TABLE IF NOT EXISTS memory_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    done TEXT NOT NULL,
    task_id INTEGER,
    state TEXT NOT NULL,
    created TEXT NOT NULL,
    resolved TEXT,
    message_id INTEGER
);

CREATE TABLE IF NOT EXISTS scheduled_tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    description TEXT NOT NULL,
    timing TEXT NOT NULL,
    policy TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1,
    created TEXT NOT NULL,
    last_run TEXT,
    origin_task INTEGER,
    last_task INTEGER,
    next_run TEXT,
    timing_text TEXT,
    -- model (a task for the agent), script (a fixed command without the model) or maintenance
    kind TEXT NOT NULL DEFAULT 'model',
    project TEXT,
    command TEXT,
    silence TEXT,
    pending INTEGER NOT NULL DEFAULT 0,
    -- fixed size of the task it creates (otherwise triage decides) and its caps in JSON
    -- ({"turns": 80, "usd": 4.0}; otherwise, those of its size)
    size TEXT,
    caps TEXT
);

-- Improvement proposals from weekly maintenance (type A applied with a button; B, for Claude Code).
CREATE TABLE IF NOT EXISTS improvements (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    reason TEXT NOT NULL,
    file TEXT,
    change TEXT,
    state TEXT NOT NULL,
    backup TEXT,
    resolved TEXT
);

CREATE TABLE IF NOT EXISTS kv_state (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated TEXT NOT NULL
);

-- Gmail emails already reviewed: read only; records what was decided for each one.
CREATE TABLE IF NOT EXISTS emails (
    id TEXT PRIMARY KEY,
    thread TEXT,
    received TEXT,
    -- arrival in Gmail, in Unix seconds (internalDate)
    arrival INTEGER,
    from_name TEXT,
    from_address TEXT,
    subject TEXT,
    -- the start of the email and whether it is a bulk send: to classify it again
    start TEXT,
    bulk INTEGER NOT NULL DEFAULT 0,
    -- appointment | bank | insurance | official | invoice | other
    category TEXT,
    -- notice | silence | previous | model_pending
    decision TEXT NOT NULL,
    reason TEXT,
    message_id INTEGER,
    reviewed TEXT NOT NULL,
    -- attempts to classify it with the model and when the next one is due (Unix seconds)
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt REAL
);
CREATE INDEX IF NOT EXISTS idx_emails_address ON emails(from_address);
CREATE INDEX IF NOT EXISTS idx_emails_decision ON emails(decision);

-- Email notices Telegram could not deliver: retried on each review.
CREATE TABLE IF NOT EXISTS pending_notices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email_id TEXT,
    created TEXT NOT NULL,
    text TEXT NOT NULL,
    -- buttons in JSON: [[{text, datum}, ...], ...]
    buttons TEXT,
    attempts INTEGER NOT NULL DEFAULT 0
);

-- What the user teaches with the notice buttons: by exact address, never by domain.
CREATE TABLE IF NOT EXISTS senders (
    address TEXT PRIMARY KEY,
    -- important | silence
    preference TEXT NOT NULL,
    since TEXT NOT NULL
);

-- Your documents: the file is in data/documents, out of what the agent reads. What was
-- read is a proposal until the user presses “Save”; only the code writes it.
CREATE TABLE IF NOT EXISTS documents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    -- proposed | saved | discarded
    state TEXT NOT NULL,
    title TEXT,
    kind TEXT,
    entity TEXT,
    number TEXT,
    summary TEXT,
    -- the file path, relative to data/documents
    file TEXT NOT NULL,
    original_name TEXT,
    -- the document text: from the PDF, or what the model transcribed from an image
    text TEXT,
    -- where the text comes from: pdf | image
    origin_text TEXT,
    -- what the user wrote in the caption and the doubts from reading it
    note TEXT,
    doubts TEXT,
    message_id INTEGER,
    saved TEXT,
    -- if it came through the inbox: its path inside it until it is saved
    inbox TEXT,
    -- SHA-256 of the file, so the same inbox file is not read twice
    fingerprint TEXT
);
CREATE TABLE IF NOT EXISTS document_dates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    document_id INTEGER NOT NULL,
    -- expiry | renewal | end | deadline | payment | appointment | advance_notice | other
    kind TEXT NOT NULL,
    -- YYYY-MM-DD
    date TEXT NOT NULL,
    -- what happens that day, as a phrase with a verb: “the insurance expires”
    description TEXT NOT NULL,
    -- the sentence from the document, if it came from it, and whether the code found it in the text
    phrase TEXT,
    checked INTEGER NOT NULL DEFAULT 0,
    -- the notices already sent: days before (60, 30, 7, 0), in JSON
    notices TEXT NOT NULL DEFAULT '[]',
    -- in an advance notice, how it was calculated, in JSON: {"months", "days", "base"} (the date it
    -- comes from), to redo it if that date is corrected
    rule TEXT
);
CREATE INDEX IF NOT EXISTS idx_documents_dates_date ON document_dates(date);
CREATE VIRTUAL TABLE IF NOT EXISTS documents_fts USING fts5(
    title, kind, entity, number, summary, text, tokenize = 'unicode61 remove_diacritics 2'
);

-- Health: only the code writes and reads it. The agent does not see it (neither the database nor
-- data/health) and nothing from here goes to the model, except a meal photo.
-- The days with the period: a cycle starts on the first of each streak.
CREATE TABLE IF NOT EXISTS health_period (
    -- YYYY-MM-DD
    day TEXT PRIMARY KEY,
    created TEXT NOT NULL,
    -- telegram | panel
    origin TEXT NOT NULL
);
-- Each gym exercise: sets, reps and weight (in kg; empty if no weight).
CREATE TABLE IF NOT EXISTS health_gym (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    -- YYYY-MM-DD, the user's day
    date TEXT NOT NULL,
    created TEXT NOT NULL,
    exercise TEXT NOT NULL,
    exercise_group TEXT NOT NULL,
    series INTEGER NOT NULL,
    reps INTEGER NOT NULL,
    weight REAL,
    -- the line as it was written
    text TEXT,
    origin TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_gym_date ON health_gym(date);
-- Meals by photo: what the model estimates and what the user corrects.
CREATE TABLE IF NOT EXISTS health_meals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created TEXT NOT NULL,
    date TEXT NOT NULL,
    -- MEALTYPES
    kind TEXT NOT NULL,
    -- pending (not looked at yet) | items | no_meal
    state TEXT NOT NULL,
    -- the names of its photos in data/health/photos (JSON); empty once cleanup deletes them
    photos TEXT,
    -- ITEMS
    foods TEXT,
    kcal REAL,
    protein REAL,
    carbs REAL,
    fats REAL,
    message_id INTEGER,
    origin TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_meals_date ON health_meals(date);
CREATE INDEX IF NOT EXISTS idx_health_meals_message ON health_meals(message_id);
-- Water (ml), sleep (hours), steps and weight (kg).
CREATE TABLE IF NOT EXISTS health_habits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    -- water | sleep | steps | weight
    kind TEXT NOT NULL,
    value REAL NOT NULL,
    created TEXT NOT NULL,
    origin TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_health_habits_date ON health_habits(kind, date);
-- The health profile (sex, birth, height, goal, activity, water): here and not in
-- profile.md, which the agent reads.
CREATE TABLE IF NOT EXISTS health_profile (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated TEXT NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS history_fts USING fts5(
    request, summary, files, tokenize = 'unicode61 remove_diacritics 2'
);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(
    done, tokenize = 'unicode61 remove_diacritics 2'
);
"""


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def to_date(value: str | None) -> datetime | None:
    """Parses an ISO date string, assuming UTC if it has no time zone; None if empty."""
    if not value:
        return None
    date = datetime.fromisoformat(value)
    return date if date.tzinfo else date.replace(tzinfo=UTC)


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def from_json(value: str | None) -> Any:
    return json.loads(value) if value else None


def fts_query(text: str) -> str:
    """Converts free text into a safe FTS5 query (quoted terms, OR)."""
    terms = [t for t in "".join(c if c.isalnum() else " " for c in text).split() if len(t) > 1]
    if not terms:
        return '""'
    return " OR ".join(f'"{t}"' for t in terms[:20])


class Database:
    """Single connection shared by the whole process, with a lock for writes."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()
        self._locks: dict[str, asyncio.Lock] = {}

    def lock(self, name: str) -> asyncio.Lock:
        """A named lock to check and then write without races (two taps of the same button arrive
        at once)."""
        return self._locks.setdefault(name, asyncio.Lock())

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("the database is not connected")
        return self._conn

    async def connect(self) -> None:
        """Opens the SQLite database, sets its pragmas, applies the schema and migrates."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path)
        self._conn.row_factory = aiosqlite.Row
        await self._conn.execute("PRAGMA journal_mode=WAL")
        await self._conn.execute("PRAGMA foreign_keys=ON")
        await self._conn.execute("PRAGMA busy_timeout=10000")
        await self._conn.executescript(SCHEMA)
        await self._migrate()
        await self._conn.execute(f"PRAGMA user_version={VERSION_SCHEMA}")
        await self._conn.commit()

    async def _migrate(self) -> None:
        """Adds the new columns to a database created with an earlier schema."""
        new_columns = {
            "tasks": {
                "next_step": "TEXT",
                "final_folder": "TEXT",
                "project": "TEXT",
                "token_cap": "INTEGER",
            },
            "approvals": {"fingerprint": "TEXT"},
            "documents": {"inbox": "TEXT", "fingerprint": "TEXT"},
            "scheduled_tasks": {
                "next_run": "TEXT",
                "timing_text": "TEXT",
                "kind": "TEXT NOT NULL DEFAULT 'model'",
                "project": "TEXT",
                "command": "TEXT",
                "silence": "TEXT",
                "pending": "INTEGER NOT NULL DEFAULT 0",
                "size": "TEXT",
                "caps": "TEXT",
            },
        }
        for table, columns in new_columns.items():
            cursor = await self._conn.execute(f"PRAGMA table_info({table})")
            existing = {row[1] for row in await cursor.fetchall()}
            for name, kind in columns.items():
                if name not in existing:
                    await self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {kind}")

    async def close(self) -> None:
        """Closes the database connection if it is open."""
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    async def run(self, sql: str, parameters: Iterable[Any] = ()) -> int:
        """Runs a write statement under the lock, commits, and returns the last row id."""
        async with self._lock:
            cursor = await self.conn.execute(sql, tuple(parameters))
            await self.conn.commit()
            return cursor.lastrowid or 0

    async def change(self, sql: str, parameters: Iterable[Any] = ()) -> int:
        """An UPDATE, DELETE or INSERT OR IGNORE: returns how many rows it touched (to check and
        change in a single step, without races)."""
        async with self._lock:
            cursor = await self.conn.execute(sql, tuple(parameters))
            await self.conn.commit()
            return cursor.rowcount

    async def run_several(self, statements: list[tuple[str, Iterable[Any]]]) -> None:
        """Runs several statements under one lock and commits them together."""
        async with self._lock:
            for sql, parameters in statements:
                await self.conn.execute(sql, tuple(parameters))
            await self.conn.commit()

    async def one(self, sql: str, parameters: Iterable[Any] = ()) -> dict[str, Any] | None:
        """Returns the first row of a query as a dict, or None if there is none."""
        cursor = await self.conn.execute(sql, tuple(parameters))
        row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def everything(self, sql: str, parameters: Iterable[Any] = ()) -> list[dict[str, Any]]:
        cursor = await self.conn.execute(sql, tuple(parameters))
        return [dict(f) for f in await cursor.fetchall()]

    async def value(self, sql: str, parameters: Iterable[Any] = ()) -> Any:
        row = await self.one(sql, parameters)
        return next(iter(row.values())) if row else None

    # --- key-value state (forced model, quick session, active limits...) ----------

    async def kv_put(self, key: str, value: Any) -> None:
        """Stores a JSON value under a key in the key-value table, replacing any old one."""
        await self.run(
            "INSERT INTO kv_state(key, value, updated) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated=excluded.updated",
            (key, to_json(value), now()),
        )

    async def kv_read(self, key: str, default: Any = None) -> Any:
        row = await self.one("SELECT value FROM kv_state WHERE key=?", (key,))
        return from_json(row["value"]) if row else default

    async def kv_delete(self, key: str) -> None:
        await self.run("DELETE FROM kv_state WHERE key=?", (key,))
