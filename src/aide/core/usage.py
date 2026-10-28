"""Usage log per task and model, and of plan limit events.

In streaming mode, `model_usage` and `total_cost_usd` are cumulative, so differences
between successive results are saved. When resuming a session, the CLI starts from the total that
session already had (checked with Haiku on 23-10-2026, also if another ran in between): the
new connection starts with that session's saved snapshot, not with zero.

The estimated cost in USD is only used internally, for each task's caps
(`max_budget_usd`). The user is shown tokens and the percentage of plan limits.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aide.core.db import Database, from_json, now, to_json
from aide.core.scheduler import short_date

FIELDS = {
    "inputTokens": "input_tokens",
    "outputTokens": "output_tokens",
    "cacheReadInputTokens": "cache_read",
    "cacheCreationInputTokens": "cache_creation",
    "costUSD": "cost_usd",
    "webSearchRequests": "web_searches",
}
TOKENS_SUM = "SUM(input_tokens + output_tokens + cache_read + cache_creation)"

log = logging.getLogger(__name__)


def model_name(model_id: str) -> str:
    """Returns the model family name, such as Opus or Sonnet, for a model id."""
    model = model_id.lower()
    for family in ("opus", "sonnet", "haiku", "fable"):
        if family in model:
            return family.capitalize()
    return model_id


def format_tokens(quantity: float | None, unit: bool = True) -> str:
    """“950 tokens”, “85K tokens”, “1.2M tokens” (without the word if `unit` is false)."""
    n = int(quantity or 0)
    if n < 1000:
        text = str(n)
    elif round(n / 1000) < 1000:
        text = f"{round(n / 1000)}K"
    else:
        text = f"{n / 1_000_000:,.1f}M"
    return f"{text} tokens" if unit else text


@dataclass
class Consumption:
    """Tokens consumed (by a task, a model or a period)."""

    entry: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0

    @property
    def cache(self) -> int:
        return self.cache_read + self.cache_creation

    @property
    def total(self) -> int:
        return self.entry + self.output + self.cache

    def text(self) -> str:
        return format_tokens(self.total)

    def breakdown(self) -> str:
        """Describes the input, output and cache token counts as text."""
        return (
            f"{format_tokens(self.entry, False)} input, "
            f"{format_tokens(self.output, False)} output and "
            f"{format_tokens(self.cache, False)} cache"
        )

    @classmethod
    def from_row(cls, row: dict[str, Any] | None) -> Consumption:
        """Builds a consumption from a database row, treating missing values as zero."""
        row = row or {}
        return cls(
            entry=int(row.get("entry") or 0),
            output=int(row.get("output") or 0),
            cache_read=int(row.get("cache_read") or 0),
            cache_creation=int(row.get("cache_creation") or 0),
        )


_SUMS = (
    "SUM(input_tokens) AS entry, SUM(output_tokens) AS output, "
    "SUM(cache_read) AS cache_read, SUM(cache_creation) AS cache_creation"
)


@dataclass
class Snapshot:
    """Last cumulative `model_usage` seen on a connection."""

    by_model: dict[str, dict[str, float]] = field(default_factory=dict)


def differences(
    model_usage: dict[str, Any] | None, previous: Snapshot
) -> dict[str, dict[str, float]]:
    """Returns the per-model increase in usage since a previous snapshot."""
    result: dict[str, dict[str, float]] = {}
    for model, data in (model_usage or {}).items():
        prior = previous.by_model.get(model, {})
        delta = {}
        for key in FIELDS:
            current = float(data.get(key) or 0)
            delta[key] = max(0.0, current - float(prior.get(key) or 0))
        if any(delta.values()):
            result[model] = delta
    return result


@dataclass
class Window:
    """A plan limits window (5 hours or weekly), ready to display."""

    key: str
    name: str  # “5-hour window”, “Weekly limit”
    article: str
    used: int | None = None  # percentage; None if it has already reset and there is no new data
    reset: str | None = None  # “today at 15:20”
    restarted: bool = False

    @property
    def free(self) -> int | None:
        return None if self.used is None else max(0, 100 - self.used)

    @property
    def level(self) -> str:
        """Bar color: low, medium or high."""
        used = self.used or 0
        return "high" if used >= 80 else "medium" if used >= 50 else "low"

    def text(self) -> str:
        """Describes the window usage as text, or says there is no data since its reset."""
        if self.restarted:
            return (
                f"{self.article.capitalize()} {self.name.lower()} reset {self.reset}; "
                "there's no usage data since then."
            )
        text = f"{self.name} {bar(self.used or 0)} {self.used}% used, {self.free}% free"
        if self.reset:
            text += f", resets {self.reset}"
        return text + "."


@dataclass
class LimitsState:
    """The plan limit windows with their data age and notices."""

    there_is_data: bool
    age: str | None = None  # “Limit data from 20 h ago (yesterday at 16:05).”
    windows: list[Window] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)

    def text(self) -> str:
        """Describes the limits as text, or says there is no data yet."""
        if not self.there_is_data:
            return "There's no data on the plan limits yet."
        lines = [self.age] if self.age else []
        lines += [v.text() for v in self.windows]
        return "\n".join(lines + self.notices)


def bar(percentage: int, cells: int = 10) -> str:
    full = max(0, min(cells, round(percentage * cells / 100)))
    return "▰" * full + "▱" * (cells - full)


class UsageLog:
    """Records token usage and plan limit events in the database."""

    def __init__(self, db: Database) -> None:
        self.db = db
        # Called with the raw data of each limits event (70% and 90% warnings).
        self.to_log: Callable[[dict[str, Any]], Awaitable[None]] | None = None

    async def record(
        self,
        *,
        task_id: int | None,
        origin: str,
        model_usage: dict[str, Any] | None,
        previous: Snapshot,
        turns: int,
        main_model: str | None = None,
    ) -> float:
        """Saves the differences per model and updates the snapshot. Returns the new cost
        (USD estimate, only for the caps)."""
        deltas = differences(model_usage, previous)
        moment = now()
        total = 0.0
        statements = []
        # Turns are attributed to the session's main model (or to the first if none
        # appears).
        with_turns = main_model if main_model in deltas else next(iter(deltas), None)
        for model, delta in deltas.items():
            total += delta["costUSD"]
            statements.append(
                (
                    "INSERT INTO usage(task_id, hour, origin, model, input_tokens, output_tokens, "
                    "cache_read, cache_creation, cost_usd, turns, web_searches) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        task_id,
                        moment,
                        origin,
                        model,
                        int(delta["inputTokens"]),
                        int(delta["outputTokens"]),
                        int(delta["cacheReadInputTokens"]),
                        int(delta["cacheCreationInputTokens"]),
                        delta["costUSD"],
                        turns if model == with_turns else 0,
                        int(delta["webSearchRequests"]),
                    ),
                )
            )
        if not deltas and turns:
            statements.append(
                (
                    "INSERT INTO usage(task_id, hour, origin, model, turns) VALUES (?, ?, ?, ?, ?)",
                    (task_id, moment, origin, "unknown", turns),
                )
            )
        if statements:
            await self.db.run_several(statements)
        for model, data in (model_usage or {}).items():
            previous.by_model[model] = {k: float(data.get(k) or 0) for k in FIELDS}
        return total

    async def save_snapshot(self, session_id: str | None, previous: Snapshot) -> None:
        """A session's total after its last result: if resumed, the CLI starts from it."""
        if not session_id or not previous.by_model:
            return
        await self.db.run(
            "INSERT INTO usage_sessions(session_id, by_model, updated) VALUES (?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET by_model=excluded.by_model, "
            "updated=excluded.updated",
            (session_id, to_json(previous.by_model), now()),
        )

    async def snapshot_of(self, session_id: str | None) -> Snapshot:
        """What the CLI already has accumulated when resuming that session (empty if unknown)."""
        if not session_id:
            return Snapshot()
        row = await self.db.one(
            "SELECT by_model FROM usage_sessions WHERE session_id=?", (session_id,)
        )
        data = from_json(row["by_model"]) if row else None
        if not isinstance(data, dict):
            return Snapshot()
        return Snapshot(
            {
                str(model): {str(k): float(v or 0) for k, v in fields.items()}
                for model, fields in data.items()
                if isinstance(fields, dict)
            }
        )

    # --- token usage -------------------------------------------------------------------------

    async def by_task(self, task_id: int) -> dict[str, dict[str, Any]]:
        """A task's usage per model: tokens and turns (and the estimated cost, internal only)."""
        rows = await self.db.everything(
            f"SELECT model, {_SUMS}, SUM(turns) AS turns, SUM(cost_usd) AS cost "
            "FROM usage WHERE task_id=? GROUP BY model",
            (task_id,),
        )
        return {f["model"]: {**f, "consumption": Consumption.from_row(f)} for f in rows}

    async def task_consumption(self, task_id: int) -> Consumption:
        """Returns the total token consumption of a task."""
        return Consumption.from_row(
            await self.db.one(f"SELECT {_SUMS} FROM usage WHERE task_id=?", (task_id,))
        )

    async def task_text(self, task_id: int) -> str:
        """A task's usage line, for example “Usage: 1.2M tokens.”."""
        return f"Usage: {(await self.task_consumption(task_id)).text()}."

    async def consumption_since(self, since: datetime) -> Consumption:
        """Returns the total token consumption since a given moment."""
        return Consumption.from_row(
            await self.db.one(
                f"SELECT {_SUMS} FROM usage WHERE hour >= ?",
                (since.astimezone(UTC).isoformat(timespec="seconds"),),
            )
        )

    async def consumption_today(self, zone: str, moment: datetime | None = None) -> Consumption:
        """Usage since midnight in the user's time zone."""
        local = (moment or datetime.now(UTC)).astimezone(ZoneInfo(zone))
        return await self.consumption_since(
            local.replace(hour=0, minute=0, second=0, microsecond=0)
        )

    async def consumption_by_tasks(
        self, since: datetime | None = None, limit: int = 30
    ) -> list[dict[str, Any]]:
        """Tasks with usage (most recent first), with their total in tokens."""
        condition, parameters = "", []
        if since is not None:
            condition = "AND u.hour >= ?"
            parameters.append(since.astimezone(UTC).isoformat(timespec="seconds"))
        rows = await self.db.everything(
            f"SELECT u.task_id, t.title, t.request, t.kind, {TOKENS_SUM} AS tokens "
            "FROM usage u JOIN tasks t ON t.id = u.task_id "
            f"WHERE u.task_id IS NOT NULL {condition} "
            "GROUP BY u.task_id ORDER BY u.task_id DESC LIMIT ?",
            (*parameters, limit),
        )
        for row in rows:
            text = (row["title"] or row["request"] or "").strip().replace("\n", " ")
            row["name"] = text if len(text) <= 60 else text[:57] + "..."
            row["tokens_text"] = format_tokens(row["tokens"])
        return rows

    async def consumption_by_day(
        self, days: int, zone: str, moment: datetime | None = None
    ) -> list[tuple[str, dict[str, int]]]:
        """Tokens per day (in the user's time) and model family, for the last `days` days,
        including today and also days with no usage (for the chart)."""
        tz = ZoneInfo(zone)
        today = (moment or datetime.now(UTC)).astimezone(tz).date()
        first = today - timedelta(days=days - 1)
        since = datetime(first.year, first.month, first.day, tzinfo=tz)
        rows = await self.db.everything(
            "SELECT hour, model, input_tokens + output_tokens + cache_read + cache_creation "
            "AS tokens FROM usage WHERE hour >= ?",
            (since.astimezone(UTC).isoformat(timespec="seconds"),),
        )
        by_day: dict[str, dict[str, int]] = {
            (first + timedelta(days=i)).isoformat(): {} for i in range(days)
        }
        for row in rows:
            day = datetime.fromisoformat(row["hour"]).astimezone(tz).date().isoformat()
            if day in by_day:
                family = model_name(row["model"] or "")
                by_day[day][family] = by_day[day].get(family, 0) + int(row["tokens"] or 0)
        return list(by_day.items())

    # --- plan limits --------

    async def log_limit(self, info: Any) -> None:
        """Records a limit event with its type, status, utilization and reset time."""
        reset = (
            datetime.fromtimestamp(info.resets_at, UTC).isoformat(timespec="seconds")
            if getattr(info, "resets_at", None)
            else None
        )
        await self.db.run(
            "INSERT INTO limits(hour, kind, state, utilization, reset, data) VALUES (?, ?, ?, ?, ?, ?)",
            (
                now(),
                getattr(info, "rate_limit_type", None),
                getattr(info, "status", None),
                getattr(info, "utilization", None),
                reset,
                to_json(getattr(info, "raw", {}) or {}),
            ),
        )
        if self.to_log is not None:
            try:
                await self.to_log(getattr(info, "raw", {}) or {})
            except Exception:
                log.exception("could not check the weekly limit alert")

    async def last_limit(self) -> dict[str, Any] | None:
        """Returns the most recent limit event with its data decoded, if any."""
        row = await self.db.one("SELECT * FROM limits ORDER BY id DESC LIMIT 1")
        if row:
            row["data"] = from_json(row["data"]) or {}
        return row

    async def reached_limit(self, moment: datetime | None = None) -> tuple[str, datetime] | None:
        """(window, reset) if the last limits data is a rejection that is still in force."""
        row = await self.last_limit()
        if not row or row["state"] != "rejected" or not row.get("reset"):
            return None
        reset = datetime.fromisoformat(row["reset"])
        if reset <= (moment or datetime.now(UTC)):
            return None
        return str(row.get("kind") or ""), reset

    async def limits_age(self, moment: datetime | None = None) -> timedelta | None:
        """Age of the last limits data (None if there is none)."""
        row = await self.db.one("SELECT hour FROM limits ORDER BY id DESC LIMIT 1")
        if not row:
            return None
        return (moment or datetime.now(UTC)) - datetime.fromisoformat(row["hour"])

    async def limits_state(self, zone: str, moment: datetime | None = None) -> LimitsState:
        """State of the plan limits, saying how recent the data is.

        The data arrives with each model request (SDK limits events). If it is from a while
        ago this is indicated, and a window that has already reset doesn't show its old usage as current.
        """
        moment = moment or datetime.now(UTC)
        tz = ZoneInfo(zone)
        row = await self.last_limit()
        if not row:
            return LimitsState(there_is_data=False)
        state = LimitsState(there_is_data=True)
        collected = datetime.fromisoformat(row["hour"])
        windows = (row["data"] or {}).get("unifiedWindows") or {}
        age = moment - collected
        if age > NOTICE_AGE:
            state.age = f"Limit data from {_ago(age)} ago ({_timing(collected, moment, tz)})."
        current_rejection = False
        for key, (article, name) in WINDOW_NAMES.items():
            data = windows.get(key)
            if not data:
                continue
            reset = datetime.fromtimestamp(data["resetsAt"], UTC) if data.get("resetsAt") else None
            window = Window(key, name, article)
            if reset is not None:
                window.reset = _timing(reset, moment, tz)
            if reset is not None and reset <= moment:
                window.restarted = True
            else:
                window.used = round(float(data.get("utilization") or 0) * 100)
                current_rejection = current_rejection or (
                    reset is not None and key == row.get("kind")
                )
            state.windows.append(window)
        # With no per-window data, the rejection stays in force until the event's reset
        # time.
        event_reset = datetime.fromisoformat(row["reset"]) if row.get("reset") else None
        current_event = event_reset is None or event_reset > moment
        if row["state"] == "rejected" and (current_rejection or (not windows and current_event)):
            name = WINDOW_NAMES.get(row.get("kind") or "", ("", ""))[1]
            state.notices.append(f"Limit reached ({name.lower()})." if name else "Limit reached.")
        elif not state.windows:
            if row["state"] == "rejected" and event_reset is not None:
                state.notices.append(f"The limit reset {_timing(event_reset, moment, tz)}.")
            else:
                state.notices.append(LIMIT_STATES.get(row["state"], "No details on the limits."))
        return state

    async def limits_text(self, zone: str, moment: datetime | None = None) -> str:
        return (await self.limits_state(zone, moment)).text()


NOTICE_AGE = timedelta(minutes=10)
WINDOW_NAMES = {
    "five_hour": ("the", "5-hour window"),
    "seven_day": ("the", "Weekly limit"),
}
LIMIT_STATES = {
    "allowed": "Within the plan limits.",
    "allowed_warning": "Close to a plan limit.",
    "rejected": "Plan limit reached.",
}


def limit_text(kind: str, reset: datetime | None, zone: str, moment: datetime | None = None) -> str:
    """“you've reached your plan's weekly limit, which resets today at 20:00”."""
    name = {
        "seven_day": "weekly limit",
        "seven_day_opus": "weekly Opus limit",
        "five_hour": "5-hour limit",
    }.get(kind, "limit")
    text = f"you've reached your plan's {name}"
    if reset is not None:
        text += f", which resets {_timing(reset, moment or datetime.now(UTC), ZoneInfo(zone))}"
    return text


def _ago(age: timedelta) -> str:
    minutes = int(age.total_seconds() // 60)
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    hours = minutes // 60
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} days"


def _timing(date: datetime, moment: datetime, tz: ZoneInfo) -> str:
    """“today at 15:20”, “tomorrow at 09:00”, “yesterday at 20:50” or “on Oct 21 at 21:00”."""
    local, today = date.astimezone(tz), moment.astimezone(tz).date()
    days = (local.date() - today).days
    name = {0: "today", 1: "tomorrow", -1: "yesterday"}.get(days)
    if name:
        return f"{name} at {local:%H:%M}"
    return f"on {short_date(local)} at {local:%H:%M}"
