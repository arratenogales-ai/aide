"""The morning summary. By code and without a model, and only on days with something.

Every day, from 7:30 until 11:00 (in `settings.yaml`, `morning_summary`), on the first
check with a connection, it gathers today's items: whether it is going to rain, your
calendar for today, the dates of your documents for the next 7 days (and the 60- and
30-day alerts due today) and what awaits your reply in Telegram. The rain (or not being
able to check it), an event or a document date triggers it; what awaits your reply goes
inside, but on its own does not trigger it (it already has its reminders). After 11:00
it is no longer sent: the Mac was off or offline.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aide import life, weather
from aide.core.db import to_date
from aide.paperwork import reminders

log = logging.getLogger(__name__)

DAY_KEY = "morning:summary"
# After a failure, or if the weather could not be queried, it is retried after 15 minutes (and not
# on every 30-second check).
RETRY_KEY = "morning:retry"
RETRY_MINUTES = 15
# If the weather still cannot be queried half an hour before the limit, it is sent without it.
LAST_ATTEMPT_MINUTES = 30
# If the saved calendar is older than this, it is read again before composing the summary.
CALENDAR_MINUTES = 30
# At most this many lines per part: Telegram cuts a message of more than 4096 letters.
MAX_EVENTS = 12
MAX_DOCUMENTS = 12
MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


def hour_of(text: str) -> time:
    hours, minutes = text.split(":")
    return time(int(hours), int(minutes))


def _utc(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


def _plural(n: int, one: str, several: str) -> str:
    return f"{n} {one if n == 1 else several}"


def _items(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def event_line(e: dict[str, Any]) -> str:
    """An event today, in one sentence: “At 10:00, Dentist (Downtown).”"""
    place = f" ({e['place']})" if e.get("place") else ""
    until = f", {e['until']}" if e.get("until") else ""
    if e["hour"] == life.ALL_DAY:
        return f"All day, {e['title']}{place}{until}."
    if e["hour"] == life.FOLLOWS:
        return f"Ongoing: {e['title']}{place}{until}."
    return f"At {e['hour']}, {e['title']}{place}{until}."


def calendar_notice(c: dict[str, Any]) -> str | None:
    """If the calendar is not up to date, said for Telegram (the card's is for the panel)."""
    if c.get("error") == "no_authorize":
        return "Google isn't authorized yet, so I don't know what's on your calendar."
    if c.get("error") == "google":
        return (
            "The Google authorization has expired, so I don't know what's on your calendar "
            "today. Renew it with /google."
        )
    if c.get("notice") and c.get("updated"):
        return (
            f"I haven't been able to read your calendar since {c['updated']}, so something "
            "may be missing."
        )
    if c.get("notice"):
        return "I couldn't read your calendar, so something may be missing."
    return None


async def waiting(db: Any) -> str | None:
    """What awaits their reply in Telegram, in one sentence."""
    queries = (
        (
            "SELECT COUNT(*) FROM approvals WHERE state='pending'",
            "approval",
            "approvals",
        ),
        (
            "SELECT COUNT(*) FROM tasks WHERE state IN ('waiting_plan', 'paused_cap')",
            "task waiting for your decision",
            "tasks waiting for your decision",
        ),
        (
            "SELECT COUNT(*) FROM tasks WHERE state IN ('paused_login', 'paused_browser', "
            "'paused_shutdown')",
            "paused task",
            "paused tasks",
        ),
        (
            "SELECT COUNT(*) FROM memory_proposals WHERE state='pending'",
            "memory suggestion",
            "memory suggestions",
        ),
        (
            "SELECT COUNT(*) FROM documents WHERE state='proposed'",
            "document to save",
            "documents to save",
        ),
    )
    parts: list[str] = []
    for sql, one, several in queries:
        try:
            n = await db.value(sql) or 0
        except Exception:  # a table that does not exist in an old database
            n = 0
        if n:
            parts.append(_plural(n, one, several))
    if not parts:
        return None
    return f"Waiting for you on Telegram: {_items(parts)}."


async def compose(
    engine: Any, moment: datetime, the_weather: weather.Weather
) -> tuple[str | None, reminders.Statements]:
    """The summary text, or None if there is nothing worth it today, and the document alerts it
    counts as given (only those in the text). Each part goes separately: if one fails, it is
    left out and the others continue."""
    core = engine.core
    db = engine.db
    local = moment.astimezone(ZoneInfo(engine.config.time_zone))
    today = local.date()
    lines: list[str] = []
    important = False
    marks: reminders.Statements = []

    if the_weather.failure:
        lines.append("I couldn't check today's weather. Look at the forecast just in case.")
        important = True
    elif the_weather.phrase:
        lines.append(the_weather.phrase)
        important = True

    try:
        calendar = await life.calendar(core, moment)
        events = calendar["today"]
        lines += [event_line(e) for e in events[:MAX_EVENTS]]
        if len(events) > MAX_EVENTS:
            lines.append(f"And {len(events) - MAX_EVENTS} more in your calendar.")
        important = important or bool(events)
        problem = calendar_notice(calendar)
        if problem:
            lines.append(problem)
    except Exception:
        log.exception("morning summary: could not prepare the calendar")

    try:
        # The dates of the next 7 days and the 60- and 30-day alerts due today. The alerts
        # for the dates that go in the text are marked as given with the summary.
        included: set[int] = set()
        account = 0
        for f in await life.next_documents(db, 7, today):
            if account >= MAX_DOCUMENTS:
                break
            lines.append(reminders.notice_text(f, today))
            included.add(f["id"])
            account += 1
        detail = await reminders.pending_detail(db, today)
        for f, line, _mark in detail:
            if (date.fromisoformat(f["date"]) - today).days > 7 and account < MAX_DOCUMENTS:
                lines.append(line)
                included.add(f["id"])
                account += 1
        marks = [mark for f, _line, mark in detail if f["id"] in included]
        important = important or bool(account)
    except Exception:
        log.exception("morning summary: could not prepare the documents")
        marks = []

    try:
        phrase = await waiting(db)
        if phrase:
            lines.append(phrase)
    except Exception:
        log.exception("morning summary: could not check what is waiting for a reply")

    if not important:
        return None, []
    header = (
        f"Good morning. Today is {life.WEEK_DAYS[local.weekday()]}, "
        f"{MONTHS[local.month - 1]} {local.day}."
    )
    return header + "\n" + "\n".join(f"- {x}" for x in lines), marks


async def _calendar_up_to_date(engine: Any, moment: datetime) -> None:
    """The calendar, freshly read: the Mac may have been asleep all night."""
    saved = await engine.db.kv_read(life.CALENDAR_KEY) or {}
    read = to_date(saved.get("updated"))
    if read is None or moment - read > timedelta(minutes=CALENDAR_MINUTES):
        try:
            await life.refresh_calendar(engine.core, moment)
        except Exception:
            log.exception("could not read the calendar for the morning summary")


async def review(engine: Any, moment: datetime | None = None) -> bool:
    """Sends today's summary if it is due: between the time and the limit of `morning_summary`,
    once a day and with a connection (called by the periodic check, which does not get here
    offline). Returns whether it sent it."""
    settings = engine.config.settings.morning_summary
    if not settings.active:
        return False
    zone = ZoneInfo(engine.config.time_zone)
    moment = moment or datetime.now(UTC)
    local = moment.astimezone(zone)
    if not (hour_of(settings.hour) <= local.time() < hour_of(settings.until)):
        return False
    db = engine.db
    today = local.date()
    if await db.kv_read(DAY_KEY) == today.isoformat():
        return False
    retry = to_date(await db.kv_read(RETRY_KEY))
    if retry is not None and moment < retry:
        return False
    try:
        await _calendar_up_to_date(engine, moment)
        the_weather = await weather.rain(engine.config.paths.agent / settings.weather, today)
        last = datetime.combine(today, hour_of(settings.until), zone) - timedelta(
            minutes=LAST_ATTEMPT_MINUTES
        )
        if the_weather.failure and local < last:
            # On mobile data the network comes and goes: it is retried before saying it
            # could not be checked.
            await db.kv_put(RETRY_KEY, _utc(moment + timedelta(minutes=RETRY_MINUTES)))
            return False
        text, marks = await compose(engine, moment, the_weather)
    except Exception:
        log.exception("error in the morning summary: retrying in 15 minutes")
        await db.kv_put(RETRY_KEY, _utc(moment + timedelta(minutes=RETRY_MINUTES)))
        return False
    if text is None:
        await db.kv_put(DAY_KEY, today.isoformat())
        log.info("morning summary: nothing to tell today")
        return False
    # Without its content in the log if it does not go out: it carries their calendar and where
    # it lives.
    if not await engine.notify(None, text, record=False):
        return False  # offline: it is tried again on the next check
    await db.kv_put(DAY_KEY, today.isoformat())  # before anything else: it never goes out twice
    try:
        await reminders.mark(db, marks)
    except Exception:
        log.exception("could not mark the morning summary's document alerts as given")
    log.info("morning summary sent")
    return True
