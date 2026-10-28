"""The habits: water, sleep, steps and weight, all by code.

`/water` adds 250 ml (or whatever is given: “/water 500”, “/water 0.5 l”), `/sleep 7.5`
or “/sleep 23:30-7:00”, `/steps 8000` and `/weight 61.5`. Water is added to the day's
total; sleep, steps and weight take the last value noted that day (phone steps are
already the day's total). Sleep counts for the day the user wakes up.
"""

from __future__ import annotations

import re
from datetime import date, timedelta
from typing import Any

from aide.core.db import now

KINDS = ("water", "sleep", "steps", "weight")
DEFAULT_WATER = 250
WATER_GOAL = 2000
GOAL_STEPS = 8000
# What is accepted for each one (anything outside is an error when writing it).
RANGES = {
    "water": (50, 3000),
    "sleep": (0.5, 16),
    "steps": (0, 100_000),
    "weight": (30, 250),
}


class InvalidValue(ValueError):
    """What was written is not valid; the message is for the user."""


def _number(text: str) -> float | None:
    t = text.strip().lower().replace(" ", "")
    t = re.sub(r"(ml|kg|kilos?|hours?|hrs?|h|steps|pasos|l)$", "", t)
    if re.fullmatch(r"\d{1,3}(,\d{3})+", t):
        t = t.replace(",", "")  # “8,000” steps
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", t):
        t = t.replace(".", "")  # “8.000”, as it is written in other languages
    t = t.replace(",", ".")
    return float(t) if re.fullmatch(r"\d{1,6}(\.\d{1,2})?", t) else None


def read_water(text: str) -> float:
    """Parses a water amount into milliliters, accepting liters, and validates it."""
    t = text.strip().lower()
    if not t:
        return DEFAULT_WATER
    if m := re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*l(?:iters?|itres?|itros?)?", t):
        return _validate("water", float(m[1].replace(",", ".")) * 1000)
    value = _number(t)
    if value is None:
        raise InvalidValue("I don't follow: write it in milliliters, like “/water 500”.")
    return _validate("water", value)


def read_sleep(text: str) -> float:
    """“7.5”, “7.5 h”, “7h30” or “23:30-7:00” (from bedtime to wake-up time)."""
    t = text.strip().lower().replace(" ", "")
    if m := re.fullmatch(r"(\d{1,2})[:.h](\d{2})-(\d{1,2})[:.h](\d{2})", t):
        a = int(m[1]) * 60 + int(m[2])
        b = int(m[3]) * 60 + int(m[4])
        if int(m[1]) > 23 or int(m[3]) > 23 or int(m[2]) > 59 or int(m[4]) > 59:
            raise InvalidValue("Those times don't exist. Write it like “/sleep 23:30-7:00”.")
        minutes = (b - a) % (24 * 60)
        return _validate("sleep", round(minutes / 60, 2))
    if m := re.fullmatch(r"(\d{1,2})h(\d{1,2})(?:m(?:in)?)?", t):
        return _validate("sleep", round(int(m[1]) + int(m[2]) / 60, 2))
    value = _number(t)
    if value is None:
        raise InvalidValue("I don't follow: write it like “/sleep 7.5” or “/sleep 23:30-7:00”.")
    return _validate("sleep", value)


def read_steps(text: str) -> float:
    """Parses a step count as a whole number and validates it."""
    value = _number(text)
    if value is None or value != int(value):
        raise InvalidValue("Write them as a number, for example “/steps 8000”.")
    return _validate("steps", value)


def read_weight(text: str) -> float:
    """Parses a weight in kilos and validates it."""
    value = _number(text)
    if value is None:
        raise InvalidValue("Write it in kilos, for example “/weight 61.5”.")
    return _validate("weight", value)


READERS = {"water": read_water, "sleep": read_sleep, "steps": read_steps, "weight": read_weight}


def _validate(kind: str, value: float) -> float:
    minimum, maximum = RANGES[kind]
    if not minimum <= value <= maximum:
        names = {
            "water": f"between {minimum} and {maximum} ml",
            "sleep": f"between {minimum} and {maximum} hours",
            "steps": f"between {minimum} and {maximum:,}",
            "weight": f"between {minimum} and {maximum} kg",
        }
        raise InvalidValue(f"That doesn't look right: it has to be {names[kind]}.")
    return value


async def note(db: Any, kind: str, value: float, date_: date, origin: str) -> int:
    """Saves a habit value for a date after validating it, and returns its id."""
    if kind not in KINDS:
        raise ValueError(kind)
    return await db.run(
        "INSERT INTO health_habits(date, kind, value, created, origin) VALUES (?, ?, ?, ?, ?)",
        (date_.isoformat(), kind, _validate(kind, value), now(), origin),
    )


async def of_day(db: Any, day: date) -> dict[str, float | None]:
    """The day's values: the water summed and the latest of the rest."""
    output: dict[str, float | None] = {
        "water": await db.value(
            "SELECT SUM(value) FROM health_habits WHERE kind='water' AND date=?",
            (day.isoformat(),),
        )
    }
    for kind in ("sleep", "steps", "weight"):
        output[kind] = await db.value(
            "SELECT value FROM health_habits WHERE kind=? AND date=? ORDER BY id DESC LIMIT 1",
            (kind, day.isoformat()),
        )
    return output


async def by_day(db: Any, kind: str, since: date, until: date) -> dict[date, float]:
    """The value of each day with something noted (the water summed; the rest, the latest)."""
    if kind == "water":
        rows = await db.everything(
            "SELECT date, SUM(value) AS value FROM health_habits WHERE kind='water' AND date >= ? "
            "AND date <= ? GROUP BY date",
            (since.isoformat(), until.isoformat()),
        )
    else:
        rows = await db.everything(
            "SELECT h.date, h.value FROM health_habits h WHERE h.kind=? AND h.date >= ? AND "
            "h.date <= ? AND h.id = (SELECT MAX(id) FROM health_habits x WHERE x.kind=h.kind "
            "AND x.date=h.date)",
            (kind, since.isoformat(), until.isoformat()),
        )
    return {date.fromisoformat(f["date"]): f["value"] for f in rows}


async def week_mean(db: Any, today: date) -> dict[str, float | None]:
    """The average of the days with something noted in the last 7 days."""
    since = today - timedelta(days=6)
    output: dict[str, float | None] = {}
    for kind in KINDS:
        values = list((await by_day(db, kind, since, today)).values())
        output[kind] = sum(values) / len(values) if values else None
    return output


async def last_weight(db: Any, until: date | None = None) -> float | None:
    """Returns the latest logged weight, optionally up to a date."""
    if until is None:
        return await db.value(
            "SELECT value FROM health_habits WHERE kind='weight' ORDER BY date DESC, id DESC LIMIT 1"
        )
    return await db.value(
        "SELECT value FROM health_habits WHERE kind='weight' AND date <= ? ORDER BY date DESC, "
        "id DESC LIMIT 1",
        (until.isoformat(),),
    )


async def weight_evolution(db: Any, limit: int = 60) -> list[dict[str, Any]]:
    """Returns the weight history, one entry per day, oldest first."""
    rows = await db.everything(
        "SELECT h.date, h.value FROM health_habits h WHERE h.kind='weight' AND h.id = (SELECT "
        "MAX(id) FROM health_habits x WHERE x.kind='weight' AND x.date=h.date) ORDER BY h.date "
        "DESC LIMIT ?",
        (limit,),
    )
    return list(reversed(rows))


def hours(value: float) -> str:
    """7.5 h as “7 h 30 min”."""
    total = round(value * 60)
    h, m = divmod(total, 60)
    return f"{h} h" + (f" {m:02d} min" if m else "")
