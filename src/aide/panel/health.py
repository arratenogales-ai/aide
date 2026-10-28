"""The panel's “Health” section: Cycle, Gym, Meals and Habits.

The views only read the database; the forms carry CSRF, like the others, and what they
note goes through the same functions as Telegram. Meal photos are served through
`/health/photos/<meal>/<n>`: the path comes from the database and only a file that is
directly in data/health/photos is valid (with the path resolved, like the downloads),
never anything else.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import FileResponse, HTMLResponse, PlainTextResponse, Response

from aide.health import NO_DOCTOR, cycle, gym, habits, meals, month_day, number, today_of
from aide.health import recommendations as reco

if TYPE_CHECKING:
    from aide.panel.app import Panel

VIEWS = ("cycle", "gym", "meals", "habits")
VIEW_NAMES = {"cycle": "Cycle", "gym": "Gym", "meals": "Meals", "habits": "Habits"}
SHORT_DAYS = ("M", "T", "W", "T", "F", "S", "S")
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
EXPIRED_FORM = "Form expired: reload the page"
# The name of each cycle phase, for a calendar day's card and the cycle line.
PHASES = {
    "period": "Period",
    "expected": "Expected",
    "fertile": "Fertile",
    "ovulation": "Ovulation",
}
# What each gram weighs in kcal, for a meal's protein, carbs and fat bar.
KCAL_GRAM = (("protein", "protein", 4), ("carbs", "carbs", 4), ("fats", "fat", 9))
HABIT_NAMES = {"water": "Water", "sleep": "Sleep", "steps": "Steps", "weight": "Weight"}


def destination(view: str, **extra: str) -> str:
    return "/health?" + urlencode({"view": view, **{k: v for k, v in extra.items() if v}})


def _short_text(iso: str) -> str:
    """“Oct 24” from a YYYY-MM-DD."""
    return month_day(date.fromisoformat(iso))


# --- charts (SVG with attributes computed here: no inline styles) ------------------------------


def svg_line(
    points: list[tuple[str, float]], unit: str = "", width: int = 340, height: int = 140
) -> dict[str, Any] | None:
    """A line with its points: the coordinates, the faint grid (maximum, middle and minimum),
    the area below, the highlighted last point and the text of each point for the card that
    appears on hover or tap. None if there are not at least two points."""
    if len(points) < 2:
        return None
    x_margin, margin_and, down = 34, 14, 24
    values = [v for _, v in points]
    minimum, maximum = min(values), max(values)
    if maximum - minimum < 1e-9:
        minimum, maximum = minimum - 1, maximum + 1
    slack = (maximum - minimum) * 0.1
    minimum, maximum = minimum - slack, maximum + slack
    useful_x = width - x_margin - 12
    useful_and = height - margin_and - down

    def chart_height(value: float) -> float:
        return round(margin_and + useful_and * (1 - (value - minimum) / (maximum - minimum)), 1)

    suffix = f" {unit}" if unit else ""
    coordinates = []
    for i, (label, value) in enumerate(points):
        x = x_margin + useful_x * i / (len(points) - 1)
        coordinates.append(
            {
                "x": round(x, 1),
                "y": chart_height(value),
                "label": label,
                "v": value,
                "tooltip": f"{label} · {_short(value)}{suffix}",
            }
        )
    max_height, min_low = max(values), min(values)
    base = height - down
    line = " ".join(f"{c['x']},{c['y']}" for c in coordinates)
    grid = [
        {"y": chart_height(v), "text": _short(v)}
        for v in sorted({max_height, (max_height + min_low) / 2, min_low}, reverse=True)
    ]
    return {
        "width": width,
        "height": height,
        "points": line,
        "area": f"M{coordinates[0]['x']},{base} L{line.replace(' ', ' L')} "
        f"L{coordinates[-1]['x']},{base} Z",
        "marks": coordinates,
        "end": coordinates[-1],
        "text_end": _short(coordinates[-1]["v"]),
        "grid": grid,
        "and_max": _short(max_height),
        "and_min": _short(min_low),
        "and_up": chart_height(max_height),
        "and_down": chart_height(min_low),
        "base": base,
        "x_left": x_margin,
        "x_right": width - 12,
        "first": coordinates[0]["label"],
        "last": coordinates[-1]["label"],
    }


def svg_bars(
    days: list[dict[str, Any]], goal: float | None, width: int = 336, height: int = 150
) -> dict[str, Any]:
    """The kcal of each day of the week as bars with rounded top corners, the goal line and the
    text of each bar for its card."""
    up, down = 20, 26
    base = height - down
    step = width / max(len(days), 1)
    thickness = min(26.0, step * 0.55)
    maximum = max([d.get("kcal") or 0 for d in days] + [goal or 0]) or 1
    bars = []
    for i, d in enumerate(days):
        kcal = d.get("kcal") or 0
        h = round((base - up) * kcal / maximum, 1)
        x = round(i * step + (step - thickness) / 2, 1)
        center = round(i * step + step / 2, 1)
        r = min(5.0, h / 2, thickness / 2)
        y = round(base - h, 1)
        route = (
            f"M{x},{base} V{y + r} Q{x},{y} {x + r},{y} H{round(x + thickness - r, 1)} "
            f"Q{round(x + thickness, 1)},{y} {round(x + thickness, 1)},{y + r} V{base} Z"
            if h >= 1
            else ""
        )
        bars.append(
            {
                "route": route,
                "x": x,
                "width": round(thickness, 1),
                "center": center,
                "y": y,
                "name": d["name"],
                "kcal": kcal,
                "text": number(kcal) if kcal else "",
                "tooltip": f"{d['name']} · {number(kcal)} kcal"
                if kcal
                else f"{d['name']} · nothing",
            }
        )
    meta = None
    if goal:
        meta = {
            "y": round(base - (base - up) * goal / maximum, 1),
            "text": number(goal),
        }
    return {"width": width, "height": height, "base": base, "bars": bars, "meta": meta}


def macros_bar(c: dict[str, Any]) -> list[dict[str, Any]]:
    """A meal's stacked bar: protein, carbs and fat according to the kcal of each, in a width
    of 100 with a gap of 1.5 between parts."""
    parts = [(kind, (c.get(field) or 0) * kcal) for field, kind, kcal in KCAL_GRAM]
    parts = [(kind, v) for kind, v in parts if v > 0]
    total = sum(v for _, v in parts)
    if total <= 0:
        return []
    util = 100 - 1.5 * (len(parts) - 1)
    output, x = [], 0.0
    for kind, value in parts:
        w = round(util * value / total, 2)
        output.append({"kind": kind, "x": round(x, 2), "w": w})
        x += w + 1.5
    return output


def _short(value: float) -> str:
    return f"{value:.1f}".rstrip("0").rstrip(".")


def calendar_month(
    year: int, month: int, e: cycle.Estimate, noted: set[date], today: date
) -> list[list[dict[str, Any]]]:
    """The weeks of the month (Monday to Sunday), each day with what has to be drawn. The days
    of the neighboring month that fall in the grid also carry their phase, softer."""
    from aide.life import WEEK_DAYS

    forecast = cycle.forecast_days(e)
    fertile = cycle.fertile_days(e)
    weeks = []
    for week in calendar.Calendar(firstweekday=0).monthdatescalendar(year, month):
        row = []
        for day in week:
            phase = ""
            if day in noted:
                phase = "period"
            elif day in forecast:
                phase = "expected"
            elif day == e.ovulation:
                phase = "ovulation"
            elif day in fertile:
                phase = "fertile"
            kinds = [phase] if phase else []
            if day == today:
                kinds.append("today")
            if day.month != month:
                kinds.append("outside")
            tappable = day <= today and day.month == month
            long_date = f"{WEEK_DAYS[day.weekday()]}, {MONTHS[day.month - 1]} {day.day}"
            row.append(
                {
                    "day": day.isoformat(),
                    "number": day.day,
                    "kinds": " ".join(kinds),
                    "tappable": tappable,
                    "period": day in noted,
                    "phase": PHASES.get(phase, ""),
                    "date": ("Today, " if day == today else "") + long_date,
                    "tooltip": f"{MONTHS[day.month - 1][:3]} {day.day}"
                    + (f" · {PHASES[phase].lower()}" if phase else ""),
                    "action": ("Remove period" if day in noted else "Log period")
                    if tappable
                    else "",
                }
            )
        weeks.append(row)
    return weeks


def cycle_line(e: cycle.Estimate, noted: set[date], today: date) -> dict[str, Any] | None:
    """The current cycle in a row of days, from its first day to the next period: the period,
    the fertile days, ovulation and today. If the period is late, the extra days go
    separately."""
    if e.last is None or e.next_run is None or e.fertile is None:
        return None
    long = max((e.next_run - e.last).days, 1)
    day_today = (today - e.last).days + 1
    total = min(max(long, day_today), 60)
    fertile = cycle.fertile_days(e)
    names = {**PHASES, "delay": "Late"}
    cells = []
    for i in range(total):
        day = e.last + timedelta(days=i)
        if day in noted:
            phase = "period"
        elif day == e.ovulation:
            phase = "ovulation"
        elif day in fertile:
            phase = "fertile"
        elif i >= long:
            phase = "delay"
        else:
            phase = ""
        name = names.get(phase, "")
        cells.append(
            {
                "kind": " ".join(x for x in (phase, "today" if day == today else "") if x),
                "tooltip": f"Day {i + 1} · {month_day(day)}"
                + (f" · {name.lower()}" if name else ""),
            }
        )
    a, b = e.fertile
    period = sum(1 for i in range(total) if e.last + timedelta(days=i) in noted)
    return {
        "day": day_today,
        "long": long,
        "cells": cells,
        "period": period,
        "fertile": ((a - e.last).days + 1, (b - e.last).days + 1),
        "ovulation": (e.ovulation - e.last).days + 1 if e.ovulation else None,
        "delay": max(0, day_today - long),
    }


def _month(text: str, today: date) -> tuple[int, int]:
    try:
        year, month = (int(x) for x in text.split("-"))
        date(year, month, 1)
        return year, month
    except (ValueError, TypeError):
        return today.year, today.month


def _other_month(year: int, month: int, step: int) -> str:
    total = year * 12 + month - 1 + step
    return f"{total // 12}-{total % 12 + 1:02d}"


def _percent(value: float | None, meta: float | None) -> int:
    if not value or not meta:
        return 0
    return max(0, min(100, round(value / meta * 100)))


# --- data for each tab -------------------------------------------------------------------------


async def cycle_data(panel: Panel, text_month: str) -> dict[str, Any]:
    """Collects the cycle view data: estimate, calendar month and period history."""
    db = panel.engine.db
    today = today_of(panel.engine)
    noted = set(await cycle.days(db))
    e = cycle.estimate(list(noted))
    year, month = _month(text_month, today)
    periods = cycle.streaks(list(noted))
    history = []
    for (a, b), next_step in zip(periods, [*periods[1:], None], strict=False):
        history.append(
            {
                "start": f"{month_day(a)}, {a.year}",
                "days": (b - a).days + 1,
                "cycle": (next_step[0] - a).days if next_step else None,
            }
        )
    next_step = _month(_other_month(year, month, 1), today)
    months = [
        {
            "name": f"{MONTHS[m - 1]} {a}",
            "weeks": calendar_month(a, m, e, noted, today),
        }
        for a, m in ((year, month), next_step)
    ]
    return {
        "estimate": e,
        "lines": cycle.lines(e, today),
        "missing": (e.next_run - today).days if e.next_run else None,
        "months": months,
        "line": cycle_line(e, noted, today),
        "short_days": SHORT_DAYS,
        "previous_month": _other_month(year, month, -1),
        "next_month": _other_month(year, month, 1),
        "month": f"{year}-{month:02d}",
        "is_today": (year, month) == (today.year, today.month),
        "history": list(reversed(history))[:12],
    }


async def gym_data(panel: Panel, exercise: str) -> dict[str, Any]:
    """Collects the gym view data: weekly summary, exercise chart and recent sets."""
    db = panel.engine.db
    today = today_of(panel.engine)
    s = await gym.week(db, today)
    facts = await gym.done_exercises(db)
    chosen = exercise if exercise in facts else (facts[0] if facts else "")
    evolution = await gym.evolution(db, chosen) if chosen else []
    kind = evolution[-1]["kind"] if evolution else "rm"
    chart = svg_line(
        [(_short_text(f["date"]), f["value"]) for f in evolution if f["kind"] == kind],
        "kg" if kind == "rm" else "reps",
    )
    maximum = max((g["series"] for g in s["by_group"]), default=0)
    last = await db.everything(
        "SELECT * FROM health_gym WHERE date >= ? ORDER BY date DESC, id DESC LIMIT 40",
        ((today - timedelta(days=27)).isoformat(),),
    )
    for f in last:
        f["day"] = _short_text(f["date"])
    return {
        "week": s,
        "series_max": maximum,
        "new": [r for r in s["records"] if not r["first"]],
        "record_text": gym.record_text,
        "weight": gym.format_weight,
        "facts": facts,
        "chosen": chosen,
        "chart": chart,
        "unit": "kg estimated 1RM" if kind == "rm" else "reps",
        "last": last,
        "today": today.isoformat(),
    }


async def meals_data(panel: Panel) -> dict[str, Any]:
    """Collects the meals view data: today's meals, calories and the weekly bars."""
    db = panel.engine.db
    today = today_of(panel.engine)
    r = await reco.summary(db, today)
    o = r["goals"]
    from_today = await meals.of_day(db, today)
    week = await meals.week(db, today)
    maximum = max([d.get("kcal") or 0 for d in week["days"]] + [o.kcal if o else 0])
    for d in week["days"]:
        d["percent"] = _percent(d.get("kcal"), maximum)
        d["name"] = f"{SHORT_DAYS[d['date'].weekday()]} {d['date'].day}"
    for c in from_today:
        c["macros"] = macros_bar(c) if c["state"] == "items" else []
    return {
        "bars": svg_bars(week["days"], o.kcal if o else None),
        "summary": r,
        "goals": o,
        "meals": from_today,
        "kind_name": meals.KIND_NAME,
        "week": week,
        "kcal_by_percent": _percent(r["today"]["kcal"], o.kcal if o else None),
        "protein_by_percent": _percent(r["today"]["protein"], o.protein if o else None),
        "text": reco.text(r, with_habits=False).splitlines()[:-1],
        "no_doctor": NO_DOCTOR,
    }


async def habits_data(panel: Panel) -> dict[str, Any]:
    """Collects the habits view data: sleep, steps, water and the weight chart."""
    db = panel.engine.db
    today = today_of(panel.engine)
    day = await habits.of_day(db, today)
    mean = await habits.week_mean(db, today)
    p = await reco.profile(db)
    meta_water = float(p.get("water") or habits.WATER_GOAL)
    evolution = await habits.weight_evolution(db, 60)
    chart = svg_line([(_short_text(f["date"]), f["value"]) for f in evolution], "kg")
    last = await db.everything(
        "SELECT * FROM health_habits WHERE date >= ? ORDER BY date DESC, id DESC LIMIT 30",
        ((today - timedelta(days=6)).isoformat(),),
    )
    for f in last:
        f["day"] = _short_text(f["date"])
    return {
        "day": day,
        "mean": mean,
        "meta_water": meta_water,
        "water_by_percent": _percent(day["water"], meta_water),
        "meta_steps": habits.GOAL_STEPS,
        "steps_by_percent": _percent(day["steps"], habits.GOAL_STEPS),
        "chart": chart,
        "last_weight": evolution[-1]["value"] if evolution else None,
        "last": last,
        "hours": habits.hours,
        "profile": p,
        "sexes": reco.SEXES,
        "goals": reco.GOALS,
        "activities": {c: n for c, (n, _f) in reco.ACTIVITIES.items()},
        "today": today.isoformat(),
        "names": HABIT_NAMES,
    }


async def figures(panel: Panel) -> dict[str, Any]:
    """The four gauges at the top, the same in all tabs (those of “Today”)."""
    from aide.health import summary

    return await summary.card_today(panel.engine.db, today_of(panel.engine))


# --- paths -------------------------------------------------------------------------------------


def configure(panel: Panel) -> None:
    """Registers the health pages and their routes in the panel app."""
    app = panel.app

    @app.get("/health", response_class=HTMLResponse)
    async def health_page(request: Request) -> Response:
        view = request.query_params.get("view", "cycle")
        if view not in VIEWS:
            view = "cycle"
        if view == "cycle":
            d = await cycle_data(panel, request.query_params.get("month", ""))
        elif view == "gym":
            d = await gym_data(panel, request.query_params.get("exercise", ""))
        elif view == "meals":
            d = await meals_data(panel)
        else:
            d = await habits_data(panel)
        return await panel.page(
            request,
            "health.html",
            "Health",
            "/health",
            view=view,
            views=[(v, VIEW_NAMES[v]) for v in VIEWS],
            d=d,
            c=await figures(panel),
        )

    @app.post("/health/cycle/day")
    async def touch_day(request: Request) -> Response:
        data = await panel.form(request)
        if data is None:
            return PlainTextResponse(EXPIRED_FORM, status_code=403)
        today = today_of(panel.engine)
        try:
            day = date.fromisoformat(str(data.get("day") or ""))
        except ValueError:
            return panel.go_back(destination("cycle"), "That day doesn't exist.", "error")
        month = f"{day:%Y-%m}"
        if day > today:
            return panel.go_back(
                destination("cycle", month=month), "A future day can't be logged.", "error"
            )
        remains = await cycle.toggle(panel.engine.db, day, "panel")
        notice = f"Logged {month_day(day)}." if remains else f"Removed {month_day(day)}."
        return panel.go_back(destination("cycle", month=month), notice)

    @app.post("/health/gym")
    async def note_gym(request: Request) -> Response:
        data = await panel.form(request)
        if data is None:
            return PlainTextResponse(EXPIRED_FORM, status_code=403)
        today = today_of(panel.engine)
        date_ = _form_date(str(data.get("date") or ""), today)
        if date_ is None:
            return panel.go_back(destination("gym"), "That date isn't valid.", "error")
        text = str(data.get("text") or "")[:2000]
        lines = [x for x in text.splitlines() if x.strip()]
        read = [gym.read_line(x) for x in lines]
        bad = [x for x, y in zip(lines, read, strict=False) if y is None or y.exercise is None]
        if not lines or bad:
            that = "; ".join(f"“{x.strip()[:40]}”" for x in bad[:3]) or "anything"
            return panel.go_back(
                destination("gym"),
                f"I don't understand {that}. Write one line per exercise, like “squat 4x8 60”.",
                "error",
            )
        n = await gym.note(panel.engine.db, [y for y in read if y], date_, "panel")
        return panel.go_back(destination("gym"), f"Logged {n} exercise{'s' if n != 1 else ''}.")

    @app.post("/health/{table}/{row_id}/remove")
    async def remove(request: Request, table: str, row_id: int) -> Response:
        data = await panel.form(request)
        if data is None:
            return PlainTextResponse(EXPIRED_FORM, status_code=403)
        if table == "meals":
            done = await meals.delete(panel.engine, row_id)
            return panel.go_back(
                destination("meals"), "Meal deleted." if done else "It was already gone."
            )
        name = {"gym": "health_gym", "habits": "health_habits"}.get(table)
        if name is None:
            return PlainTextResponse("Not found", status_code=404)
        done = await panel.engine.db.change(f"DELETE FROM {name} WHERE id=?", (row_id,))
        return panel.go_back(destination(table), "Removed." if done else "It was already gone.")

    @app.post("/health/habits")
    async def note_habit(request: Request) -> Response:
        data = await panel.form(request)
        if data is None:
            return PlainTextResponse(EXPIRED_FORM, status_code=403)
        kind = str(data.get("kind") or "")
        if kind not in habits.KINDS:
            return PlainTextResponse("Unknown type", status_code=400)
        today = today_of(panel.engine)
        date_ = _form_date(str(data.get("date") or ""), today)
        if date_ is None:
            return panel.go_back(destination("habits"), "That date isn't valid.", "error")
        try:
            value = habits.READERS[kind](str(data.get("value") or ""))
        except habits.InvalidValue as e:
            return panel.go_back(destination("habits"), str(e), "error")
        await habits.note(panel.engine.db, kind, value, date_, "panel")
        return panel.go_back(destination("habits"), "Logged.")

    @app.post("/health/profile")
    async def save_profile(request: Request) -> Response:
        data = await panel.form(request)
        if data is None:
            return PlainTextResponse(EXPIRED_FORM, status_code=403)
        today = today_of(panel.engine)
        errors = []
        for field in reco.FIELDS:
            value = str(data.get(field) or "").strip()
            if not value:
                continue
            try:
                await reco.put(panel.engine.db, field, value, today)
            except reco.InvalidProfile as e:
                errors.append(str(e))
        if errors:
            return panel.go_back(destination("habits"), " ".join(errors), "error", anchor="profile")
        return panel.go_back(destination("habits"), "Profile saved.", anchor="profile")

    @app.get("/health/photos/{meal_id}/{n}")
    async def photo(meal_id: int, n: int) -> Response:
        c = await meals.get(panel.engine.db, meal_id)
        path = None
        if c is not None and 0 <= n < len(c["photos"]):
            path = meals.photo_path(panel.config.paths.photos_health, c["photos"][n])
        if path is None:
            return PlainTextResponse("Photo not available", status_code=404)
        reply = FileResponse(path, content_disposition_type="inline")
        reply.headers["Cache-Control"] = "private, max-age=3600"
        return reply


def _form_date(text: str, today: date) -> date | None:
    """A form's date (from the calendar, YYYY-MM-DD), today if empty; never in the future or
    more than a year ago."""
    if not text.strip():
        return today
    try:
        day = date.fromisoformat(text.strip())
    except ValueError:
        return None
    return day if today - timedelta(days=366) <= day <= today else None
