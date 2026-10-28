"""Your life in one view.

What the panel's “Today” page, the weekly review in the Sunday report and the morning
summary bring together. All by code, without a model: it comes from the database and,
for the calendar, from Google, which is read every 30 minutes with the permission the
agent already has (calendar.events) and saved in `kv_state`, so the panel does not wait
for Google or call it when opened.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, date, datetime, timedelta
from datetime import time as time_of_day
from typing import Any
from zoneinfo import ZoneInfo

from aide.core.db import to_date
from aide.core.scheduler import short_date
from aide.core.usage import Consumption, format_tokens

log = logging.getLogger(__name__)

CALENDAR_KEY = "life:calendar"
CALENDAR_DAYS = 8  # today and the next 7
CALENDAR_MINUTES = 30
# A calendar reading older than 2 hours is no longer taken as good: the panel shows a warning.
STALE_CALENDAR_MINUTES = 120
EMAIL_DAYS = 7
WEEK_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
# What the time says for an all-day event and for one that started before today and is still going.
ALL_DAY = "all day"
FOLLOWS = "ongoing"


def _zone(core: Any) -> ZoneInfo:
    return ZoneInfo(core.config.time_zone)


def day_name(day: date, today: date) -> str:
    """“Today”, “Tomorrow” or “Thursday 29”."""
    if day == today:
        return "Today"
    if day == today + timedelta(days=1):
        return "Tomorrow"
    return f"{WEEK_DAYS[day.weekday()]} {day.day}"


def day_in_phrase(day: date, today: date) -> str:
    """The day inside a sentence: “today”, “tomorrow” or “Thursday 29”."""
    name = day_name(day, today)
    return name.lower() if day <= today + timedelta(days=1) else name


def local_today(core: Any, moment: datetime | None = None) -> date:
    """Today's date in the user's time zone (not UTC's, which between 0:00 and 1:00 or 2:00 is
    still yesterday)."""
    return (moment or datetime.now(UTC)).astimezone(_zone(core)).date()


def _utc(moment: datetime) -> str:
    """A moment as the database stores it (`now()`): in UTC and with seconds, so that text
    comparisons with the date columns are correct."""
    return moment.astimezone(UTC).isoformat(timespec="seconds")


# --- calendar ------------------------------------------------------------------------------


def google_event(e: dict[str, Any]) -> dict[str, Any]:
    """What is saved for an event: title, place, start and end, and whether it is all-day."""
    start = e.get("start") or {}
    end = e.get("end") or {}
    return {
        "title": str(e.get("summary") or "(no title)")[:120],
        "place": str(e.get("location") or "")[:120],
        "start": start.get("dateTime") or start.get("date"),
        "end": end.get("dateTime") or end.get("date"),
        "all_day": "dateTime" not in start,
    }


def rejected(e: dict[str, Any]) -> bool:
    """Whether the user said they are not going to an event they were invited to (it does not
    show in “Today”)."""
    return any(
        isinstance(a, dict) and a.get("self") and a.get("responseStatus") == "declined"
        for a in e.get("attendees") or []
    )


async def refresh_calendar(core: Any, moment: datetime | None = None) -> int | None:
    """Reads from Google the events of today and the next 7 days and saves them. Returns how
    many; None if Google is not authorized or fails (the error is noted and the last reading
    is kept)."""
    auth = core.services.get("google")
    if auth is None:
        return None
    zone = _zone(core)
    moment = (moment or datetime.now(UTC)).astimezone(zone)
    since = datetime.combine(moment.date(), time_of_day.min, zone)
    until = since + timedelta(days=CALENDAR_DAYS)
    calendar = core.config.settings.google.calendar

    def read() -> dict[str, Any]:
        service = auth.service("calendar", "v3")
        return (
            service.events()
            .list(
                calendar_id=calendar,
                time_min=since.isoformat(),
                time_max=until.isoformat(),
                single_events=True,
                order_by="startTime",
                max_results=100,
            )
            .execute()
        )

    db = core.db
    try:
        reply = await asyncio.to_thread(read)
    except Exception as e:
        from aide.tools.google_auth import GoogleNotAuthorized, GoogleUnauthorized

        previous = await db.kv_read(CALENDAR_KEY) or {}
        if isinstance(e, GoogleUnauthorized):
            error = "no_authorize"
        elif isinstance(e, GoogleNotAuthorized):
            error = "google"
        else:
            error = type(e).__name__
        await db.kv_put(CALENDAR_KEY, {**previous, "error": error, "attempted": _utc(moment)})
        log.info("could not read the calendar: %s", error)
        return None
    events = [
        google_event(e)
        for e in reply.get("items", [])
        if isinstance(e, dict) and e.get("status") != "cancelled" and not rejected(e)
    ]
    await db.kv_put(CALENDAR_KEY, {"updated": _utc(moment), "events": events})
    return len(events)


def _local_moment(value: str | None, zone: ZoneInfo) -> tuple[date, str] | None:
    """(day, “HH:MM” or “”) of an event's start or end: with a time or all-day."""
    if not value:
        return None
    if len(value) == 10:
        try:
            return date.fromisoformat(value), ""
        except ValueError:
            return None
    date_ = to_date(value)
    if date_ is None:
        return None
    local = date_.astimezone(zone)
    return local.date(), f"{local:%H:%M}"


def _last_day(e: dict[str, Any], start: date, end: tuple[date, str] | None) -> date:
    """The last day an event occupies. In Google, the end of an all-day event is the day after
    the last one, and one with a time that ends at 0:00 does not occupy that day."""
    if end is None:
        return start
    day, hour = end
    if (e.get("all_day") or hour == "00:00") and day > start:
        day -= timedelta(days=1)
    return max(day, start)


def _read_when(moment: datetime, today: date) -> str:
    """“15:53”, “yesterday at 15:53” or “Oct 16 at 15:53”."""
    if moment.date() == today:
        return f"{moment:%H:%M}"
    if moment.date() == today - timedelta(days=1):
        return f"yesterday at {moment:%H:%M}"
    return f"{short_date(moment)} at {moment:%H:%M}"


def calendar_notice(error: str | None, read: bool, old: bool, timing: str | None) -> str | None:
    """What the calendar card has to warn about, or None if it is up to date."""
    if error == "no_authorize":
        return (
            "Google isn't authorized yet: authorize it with /google in Telegram to see your "
            "calendar here."
        )
    if error == "google":
        return (
            "Google is asking you to renew the authorization: do it with /google in Telegram to "
            "keep your calendar up to date."
        )
    if read and (error or old):
        return (
            f"I haven't been able to read your calendar since {timing}: something may be missing."
        )
    if error:
        return "I haven't been able to read your calendar yet: I try again every 30 minutes."
    return None


async def calendar(core: Any, moment: datetime | None = None) -> dict[str, Any]:
    """The saved events, by day, from today onward, with when they were read and whether Google
    fails (for example, because the authorization expired) or the reading has become stale."""
    zone = _zone(core)
    moment = moment or datetime.now(UTC)
    today = moment.astimezone(zone).date()
    data = await core.db.kv_read(CALENDAR_KEY) or {}
    days: dict[date, list[dict[str, Any]]] = {}
    for e in data.get("events") or []:
        start = _local_moment(e.get("start"), zone)
        if start is None:
            continue
        day, hour = start
        end = _local_moment(e.get("end"), zone)
        last = _last_day(e, day, end)
        if last < today:
            continue
        # What started before today and is still going (a trip, a night shift) shows up in
        # today.
        in_progress = day < today
        where = today if in_progress else day
        hour_text = ALL_DAY if e.get("all_day") else FOLLOWS if in_progress else hour
        until = None
        if last > where:
            until = f"until {day_in_phrase(last, today)}"
            if end is not None and end[1] and end[1] != "00:00":
                until += f" at {end[1]}"
        elif in_progress and end is not None and end[1]:
            until = f"until {end[1]}"
        days.setdefault(where, []).append({**e, "hour": hour_text, "until": until})
    updated = to_date(data.get("updated"))
    read = bool(data.get("events") is not None and updated is not None)
    old = updated is None or moment - updated > timedelta(minutes=STALE_CALENDAR_MINUTES)
    timing = _read_when(updated.astimezone(zone), today) if updated else None
    return {
        "days": [{"date": d, "name": day_name(d, today), "events": days[d]} for d in sorted(days)],
        "today": days.get(today, []),
        "updated": timing,
        "error": data.get("error"),
        "read": read,
        "old": old,
        "notice": calendar_notice(data.get("error"), read, old, timing),
        "note": None
        if timing is None
        else f"Read {'at ' if timing[:1].isdigit() else ''}{timing}.",
    }


# --- mail -------------------------------------------------------------------------------------


async def important_emails(db: Any, days: int = EMAIL_DAYS, limit: int = 8) -> list[dict]:
    """The emails the agent alerted you about in the last few days."""
    since = (datetime.now(UTC) - timedelta(days=days)).isoformat(timespec="seconds")
    return await db.everything(
        "SELECT id, thread, from_name, from_address, subject, category, decision, reviewed "
        "FROM emails WHERE decision='notice' AND reviewed >= ? "
        "ORDER BY reviewed DESC LIMIT ?",
        (since, limit),
    )


# --- usage and documents -------------------------------------------------------------------


async def consumption_between(db: Any, since: datetime, until: datetime) -> Consumption:
    """Returns the token consumption between two moments."""
    row = await db.one(
        "SELECT SUM(input_tokens) AS entry, SUM(output_tokens) AS output, "
        "SUM(cache_read) AS cache_read, SUM(cache_creation) AS cache_creation "
        "FROM usage WHERE hour >= ? AND hour < ?",
        (
            since.astimezone(UTC).isoformat(timespec="seconds"),
            until.astimezone(UTC).isoformat(timespec="seconds"),
        ),
    )
    return Consumption.from_row(row)


async def next_documents(db: Any, days: int, today: date) -> list[dict[str, Any]]:
    """The dates of your saved documents that fall from `today` (the local date) to `days` days
    later. Empty while there are no documents."""
    try:
        return await db.everything(
            "SELECT f.id, f.document_id, f.kind, f.date, f.description, d.title "
            "FROM document_dates f JOIN documents d ON d.id = f.document_id "
            "WHERE d.state='saved' AND f.date >= ? AND f.date <= ? ORDER BY f.date",
            (today.isoformat(), (today + timedelta(days=days)).isoformat()),
        )
    except (
        Exception
    ):  # the table does not exist yet (on older databases) or the database is closing
        return []


# --- the week (for the Sunday report) -------------------------------------------------------


async def since_last_report(db: Any, end: datetime) -> datetime:
    """Where the report's week starts: at the previous report if it went out between 3 and 14
    days ago (so a late report neither counts the same thing twice nor leaves gaps);
    otherwise, 7 days before `end`."""
    previous = to_date(await db.kv_read("last_maintenance"))
    if previous is not None and timedelta(days=3) <= end - previous <= timedelta(days=14):
        return previous
    return end - timedelta(days=7)


async def week(core: Any, end: datetime | None = None) -> dict[str, Any]:
    """What has happened since the previous report (about 7 days), counted by code."""
    db = core.db
    end = (end or datetime.now(UTC)).astimezone(UTC)
    since = await since_last_report(db, end)
    d, f = _utc(since), _utc(end)
    # The tasks that finished during the week, by their move to completed, failed or cancelled
    # (not by their last update, which may be later).
    finished = await db.everything(
        "SELECT t.id, t.title, t.error, x.until AS final FROM tasks t JOIN transitions x "
        "ON x.task_id = t.id WHERE t.kind='task' AND x.until IN ('completed', 'failed', "
        "'cancelled') AND x.hour >= ? AND x.hour < ? AND x.id = (SELECT MAX(y.id) FROM "
        "transitions y WHERE y.task_id = t.id AND y.hour < ?) ORDER BY t.id",
        (d, f, f),
    )
    tasks: dict[str, int] = {}
    for t in finished:
        tasks[t["final"]] = tasks.get(t["final"], 0) + 1
    failed = [t for t in finished if t["final"] == "failed"][:3]
    this = await consumption_between(db, since, end)
    previous = await consumption_between(db, since - (end - since), since)
    return {
        "tasks": tasks,
        "failed": failed,
        "consumption": this.total,
        "previous_consumption": previous.total,
        "expire": await next_documents(db, 30, local_today(core, end)),
    }


def _plural(n: int, one: str, several: str) -> str:
    return f"{n} {one if n == 1 else several}"


def _one_line(text: str | None, maximum: int) -> str:
    """A text on a single line (an error can contain line breaks), trimmed."""
    clean = " ".join((text or "").split())
    return clean if len(clean) <= maximum else clean[: maximum - 1].rstrip() + "…"


def week_text(s: dict[str, Any]) -> str:
    """“Your week”: what went well and what to improve, in a few lines and without a model."""
    well: list[str] = []
    if s["tasks"].get("completed"):
        well.append(f"I finished {_plural(s['tasks']['completed'], 'task', 'tasks')}.")
    improve: list[str] = []
    failed = s["tasks"].get("failed", 0)
    if failed:
        detail = "; ".join(
            f"task {t['id']} ({_one_line(t.get('error') or t.get('title'), 60)})"
            for t in s["failed"]
        )
        improve.append(
            f"{_plural(failed, 'task failed', 'tasks failed')}"
            + (f": {detail}." if detail else ".")
        )
    if s["consumption"] and s["previous_consumption"]:
        change = (s["consumption"] - s["previous_consumption"]) / s["previous_consumption"] * 100
        if change >= 25:
            improve.append(
                f"Usage went up {change:.0f}% compared with the week before "
                f"({format_tokens(s['consumption'])} versus {format_tokens(s['previous_consumption'])})."
            )
    for v in s["expire"][:2]:
        day = date.fromisoformat(v["date"])
        title = _one_line(v.get("title"), 60) or "A document"
        improve.append(f"{title}: {_one_line(v['description'], 120)}, on {short_date(day)}.")
    parts = []
    if well:
        parts.append("What went well\n" + "\n".join(f"- {x}" for x in well))
    if improve:
        parts.append("To improve or keep in mind\n" + "\n".join(f"- {x}" for x in improve))
    if not parts:
        return "A quiet week: no new tasks or alerts."
    return "\n\n".join(parts)
