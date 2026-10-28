"""Health: cycle, gym, meals, habits and recommendations.

All by code except looking at a meal photo. The data lives in the database (`salud_*`
tables) and the photos in data/health/photos, which the agent cannot read (policy and
sandbox, like your documents). No agent tool or the quick reply sees them, they do not
go through triage or into memory, and only the meal photo enters the model (and, to
correct it, the list of foods). Health messages do not leave their text in the log.

The estimates (the next period, the fertile days, the calories) are approximate: they
are not medical advice.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any
from zoneinfo import ZoneInfo

NO_DOCTOR = "Not medical advice."
SHORT_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def today_of(engine: Any, moment: datetime | None = None) -> date:
    """Today's date in the user's time zone."""
    return (moment or datetime.now(UTC)).astimezone(ZoneInfo(engine.config.time_zone)).date()


def number(value: float, decimals: int = 0) -> str:
    """A number with a thousands separator and a decimal point: 1,450 or 61.5."""
    return f"{value:,.{decimals}f}"


def month_day(day: date) -> str:
    """“Oct 24”."""
    return f"{SHORT_MONTHS[day.month - 1]} {day.day}"


def day_name(day: date, today: date) -> str:
    """“today”, “tomorrow”, “yesterday” or “Thursday, Nov 13”."""
    from aide.life import WEEK_DAYS

    difference = (day - today).days
    if difference == 0:
        return "today"
    if difference == 1:
        return "tomorrow"
    if difference == -1:
        return "yesterday"
    return f"{WEEK_DAYS[day.weekday()]}, {month_day(day)}"


def plural(n: float, one: str, several: str | None = None) -> str:
    """“1 day”, “3 days”."""
    return f"{number(n)} {one if n == 1 else (several or one + 's')}"


async def notify(engine: Any, text: str, buttons: Any = None, html: bool = False) -> int | None:
    """A health message to the user: like the others, but without its text in the log if there
    is no connection (the log is in logs/, and health data only goes in the database)."""
    return await engine.notify(None, text, buttons=buttons, html=html, record=False)
