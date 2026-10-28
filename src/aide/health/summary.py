"""Health content that appears outside its section: the “Today” card of the panel
and the part of the weekly report. By code, and the report's part is added to the
message after the model (which does not see it), as “Your health”. From the cycle, only
the estimated date of the next period goes.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from aide.health import NO_DOCTOR, cycle, gym, habits, meals, month_day, number, today_of
from aide.health import recommendations as reco

# The health part of the report does not exceed about 500 letters.
MAX_REPORT = 500
WEEK_LETTERS = "MTWTFSS"


async def card_today(db: Any, today: date) -> dict[str, Any]:
    """Days until the period (and the cycle day), today's kcal against the goal, water, steps
    and the gym sessions of the last 7 days."""
    e = cycle.estimate(await cycle.days(db))
    g = await gym.week(db, today)
    trained = {f["date"] for f in g["rows"]}
    gym_days = [
        {"letter": WEEK_LETTERS[d.weekday()], "done": d.isoformat() in trained}
        for d in (today - timedelta(days=6 - i) for i in range(7))
    ]
    goals, _ = await reco.calculate(db, today)
    eaten = (await reco.eaten(db, today, today)).get(today) or {}
    day = await habits.of_day(db, today)
    profile = await reco.profile(db)
    meta_water = goals.water if goals else float(profile.get("water") or habits.WATER_GOAL)
    return {
        "days_period": (e.next_run - today).days if e.next_run else None,
        "date_period": e.next_run,
        "day_cycle": (today - e.last).days + 1 if e.last else None,
        "long_cycle": (e.next_run - e.last).days if e.next_run and e.last else None,
        "kcal": eaten.get("kcal") or 0,
        "kcal_goal": goals.kcal if goals else None,
        "water": day["water"] or 0,
        "water_goal": meta_water,
        "steps": day["steps"],
        "goal_steps": habits.GOAL_STEPS,
        "sessions": g["sessions"],
        "gym_days": gym_days,
        "something": bool(
            e.starts or eaten or day["water"] or day["steps"] is not None or g["sessions"]
        ),
    }


async def report_text(
    engine: Any, moment: datetime, deleted_photos: int = 0, test: bool = False
) -> str:
    """“Your health” in the Sunday report: gym, meals and habits of the week."""
    db = engine.db
    today = today_of(engine, moment)
    lines: list[str] = []
    g = await gym.week(db, today)
    if g["sessions"]:
        phrase = (
            f"At the gym, {g['sessions']} {'session' if g['sessions'] == 1 else 'sessions'} and "
            f"{g['series']} {'set' if g['series'] == 1 else 'sets'}"
        )
        new = [r for r in g["records"] if not r["first"]]
        if new:
            phrase += f", with {len(new)} {'record' if len(new) == 1 else 'records'}"
        lines.append(phrase + ".")
    c = await meals.week(db, today)
    if c["with_something"]:
        goals, _ = await reco.calculate(db, today)
        phrase = f"You ate {number(c['kcal_mean'] or 0)} kcal a day on average"
        if goals:
            phrase += f" (your target, about {number(goals.kcal)})"
        lines.append(phrase + ".")
    m = await habits.week_mean(db, today)
    parts = []
    if m["water"]:
        parts.append(f"water {number(m['water'] / 1000, 1)} l")
    if m["sleep"]:
        parts.append(f"sleep {habits.hours(m['sleep'])}")
    if m["steps"] is not None:
        parts.append(f"{number(m['steps'])} steps")
    if parts:
        lines.append("Daily average: " + ", ".join(parts) + ".")
    weights = await habits.by_day(db, "weight", today - timedelta(days=6), today)
    if len(weights) >= 2:
        days = sorted(weights)
        difference = weights[days[-1]] - weights[days[0]]
        lines.append(
            f"You weigh {number(weights[days[-1]], 1)} kg ("
            + (
                f"{'+' if difference > 0 else '−'}{number(abs(difference), 1)} kg"
                if abs(difference) >= 0.1
                else "no change"
            )
            + " this week)."
        )
    e = cycle.estimate(await cycle.days(db))
    if e.next_run is not None and e.next_run >= today:
        lines.append(f"Next period estimated around {month_day(e.next_run)}.")
    if deleted_photos:
        verb = "I would delete" if test else "I deleted"
        how_many = f"{deleted_photos} {'photo' if deleted_photos == 1 else 'photos'}"
        lines.append(f"{verb} {how_many} of meals older than 30 days.")
    if not lines:
        return ""
    lines.append(NO_DOCTOR)
    text = "\n".join(lines)
    return text if len(text) <= MAX_REPORT else text[: MAX_REPORT - 1].rsplit(" ", 1)[0] + "…"
