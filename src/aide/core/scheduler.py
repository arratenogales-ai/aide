"""Scheduled tasks.

The SQLite `scheduled_tasks` table is the only store. From APScheduler only the triggers are used,
which compute the next date with the user's time zone (clock changes, short months). The engine's
check loop launches the due ones and, if the Mac was off at the scheduled
time, applies each one's policy.

Format of `timing` (used by the schedule_task tool):

    {"kind": "one_time", "date": "2026-10-21T09:00"} {"kind": "daily", "hour": "09:00"} {"kind":
    "weekly", "days": ["monday", "wednesday"], "hour": "09:00"} {"kind": "monthly", "day": 1,
    "hour": "08:30"} {"kind": "every_hours", "hours": 6}
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from apscheduler.triggers.cron import CronTrigger

KINDS = ("one_time", "daily", "weekly", "monthly", "every_hours")
POLICIES = ("run_if_recent", "run_always", "skip")
# Day names that `timing` accepts (the canonical ones, in English) and Spanish aliases, so that what
# already worked keeps working.
DAYS = {
    "monday": "mon",
    "tuesday": "tue",
    "wednesday": "wed",
    "thursday": "thu",
    "friday": "fri",
    "saturday": "sat",
    "sunday": "sun",
    "lunes": "mon",
    "martes": "tue",
    "miercoles": "wed",
    "miércoles": "wed",
    "jueves": "thu",
    "viernes": "fri",
    "sabado": "sat",
    "sábado": "sat",
    "domingo": "sun",
    # That's how they are saved in the database (Timing.to_json): they must also be readable.
    **{d: d for d in ("mon", "tue", "wed", "thu", "fri", "sat", "sun")},
}
DAYS_ORDER = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]
SHORT_MONTHS = (
    "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"
)  # fmt: skip
DAY_NAME = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}
# Delay that is considered execution on time (the loop checks every 30 seconds).
TOLERANCE = timedelta(minutes=10)
POLICY_TEXT = {
    "run_if_recent": "it runs at startup if no more than {hours} hours have passed",
    "run_always": "it runs at startup",
    "skip": "it is skipped",
}


class InvalidTiming(ValueError):
    """Error raised when a schedule description is not valid."""

    pass


@dataclass(frozen=True)
class Timing:
    """When a scheduled task runs: one time, every few hours, daily, weekly or monthly."""

    kind: str
    hour: time | None = None
    date: datetime | None = None
    days: tuple[str, ...] = ()  # mon, tue...
    day: int | None = None
    hours: int | None = None

    def to_json(self) -> str:
        """Serializes the timing to a JSON string, keeping only the fields that are set."""
        data: dict[str, Any] = {"kind": self.kind}
        if self.hour is not None:
            data["hour"] = self.hour.strftime("%H:%M")
        if self.date is not None:
            data["date"] = self.date.isoformat(timespec="minutes")
        if self.days:
            data["days"] = list(self.days)
        if self.day is not None:
            data["day"] = self.day
        if self.hours is not None:
            data["hours"] = self.hours
        return json.dumps(data, ensure_ascii=False)


def _hour(value: Any) -> time:
    try:
        return time.fromisoformat(str(value).strip())
    except ValueError as e:
        raise InvalidTiming(f"invalid time: {value!r} (use HH:MM)") from e


def interpret(data: Any, zone: ZoneInfo) -> Timing:
    """Validates the tool's `timing` (object or JSON) and normalizes it."""
    if isinstance(data, str):
        try:
            data = json.loads(data)
        except json.JSONDecodeError as e:
            raise InvalidTiming("cuando must be an object with tipo and its fields") from e
    if not isinstance(data, dict):
        raise InvalidTiming("cuando must be an object with tipo and its fields")
    kind = str(data.get("kind", "")).strip()
    if kind not in KINDS:
        raise InvalidTiming(f"invalid kind: {kind!r} (use {', '.join(KINDS)})")
    if kind == "one_time":
        try:
            date = datetime.fromisoformat(str(data.get("date", "")).strip())
        except ValueError as e:
            raise InvalidTiming("invalid date (use YYYY-MM-DDTHH:MM)") from e
        date = date.replace(tzinfo=zone) if date.tzinfo is None else date.astimezone(zone)
        return Timing(kind, date=date.replace(second=0, microsecond=0))
    if kind == "every_hours":
        try:
            hours = int(data.get("hours"))
        except (TypeError, ValueError) as e:
            raise InvalidTiming("invalid hours") from e
        if not 1 <= hours <= 24 * 7:
            raise InvalidTiming("hours must be between 1 and 168")
        return Timing(kind, hours=hours)
    hour = _hour(data.get("hour", ""))
    if kind == "daily":
        return Timing(kind, hour=hour)
    if kind == "weekly":
        days = data.get("days") or []
        if isinstance(days, str):
            days = [d for d in days.replace(",", " ").split() if d]
        normal = []
        for d in days:
            key = DAYS.get(str(d).strip().lower())
            if key is None:
                raise InvalidTiming(f"invalid day: {d!r} (use monday ... sunday)")
            if key not in normal:
                normal.append(key)
        if not normal:
            raise InvalidTiming("the days of the week are missing")
        normal.sort(key=DAY_NAME.__getitem__)
        return Timing(kind, hour=hour, days=tuple(normal))
    try:
        day = int(data.get("day"))
    except (TypeError, ValueError) as e:
        raise InvalidTiming("invalid day of the month") from e
    if not 1 <= day <= 31:
        raise InvalidTiming("the day of the month must be between 1 and 31")
    return Timing(kind, hour=hour, day=day)


def next_step(timing: Timing, since: datetime, zone: ZoneInfo) -> datetime | None:
    """Next run strictly after `since` (None if there are no more)."""
    since = since.astimezone(zone)
    if timing.kind == "one_time":
        return timing.date if timing.date and timing.date > since else None
    if timing.kind == "every_hours":
        return since + timedelta(hours=timing.hours or 1)
    assert timing.hour is not None
    fields: dict[str, Any] = {"hour": timing.hour.hour, "minute": timing.hour.minute}
    if timing.kind == "weekly":
        fields["day_of_week"] = ",".join(timing.days)
    elif timing.kind == "monthly":
        fields["day"] = timing.day
    trigger = CronTrigger(timezone=zone, **fields)
    return trigger.get_next_fire_time(None, since + timedelta(seconds=1))


def short_date(moment: date | datetime) -> str:
    """“Oct 28”."""
    return f"{SHORT_MONTHS[moment.month - 1]} {moment.day}"


def _items(names: list[str]) -> str:
    if len(names) == 1:
        return names[0]
    return ", ".join(names[:-1]) + " and " + names[-1]


def describe(timing: Timing) -> str:
    """Describes a timing in plain English, such as once on a date or every N hours."""
    if timing.kind == "one_time":
        assert timing.date is not None
        day = DAYS_ORDER[timing.date.weekday()].capitalize()
        month = SHORT_MONTHS[timing.date.month - 1]
        return (
            f"once, on {day}, {month} {timing.date.day}, {timing.date.year} at {timing.date:%H:%M}"
        )
    if timing.kind == "every_hours":
        return "every hour" if timing.hours == 1 else f"every {timing.hours} hours"
    hour = timing.hour.strftime("%H:%M") if timing.hour else ""
    if timing.kind == "daily":
        return f"every day at {hour}"
    if timing.kind == "weekly":
        names = [DAYS_ORDER[DAY_NAME[d]].capitalize() for d in timing.days]
        return f"every {_items(names)} at {hour}"
    return f"on day {timing.day} of every month at {hour}"


def describe_policy(policy: str, window_hours: float) -> str:
    return POLICY_TEXT[policy].format(hours=f"{window_hours:g}")


def run_with_delay(policy: str, delay: timedelta, window: timedelta) -> bool:
    """Whether an overdue run is launched (on time or after a shutdown) or skipped."""
    if delay <= TOLERANCE:
        return True
    if policy == "run_always":
        return True
    if policy == "run_if_recent":
        return delay <= window
    return False
