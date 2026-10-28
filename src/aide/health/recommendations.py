"""Your health profile and the recommendations, without a model.

The profile (sex, date of birth, height, goal, activity level and water goal) is set
with `/health profile` or in the panel and is saved in its own table, never in
profile.md, which the agent reads. The day's expenditure comes from Mifflin-St Jeor with
the latest weight noted and an activity factor: the one from the steps and gym of the
last week if there is data (3 days or more with steps), and otherwise the one the user
stated. The calorie goal subtracts or adds depending on their goal, protein goes in
grams per kilo, and it is compared with what was eaten today and during the week. These
are approximate figures, and they always come with “Not medical advice”.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

from aide.core.db import now
from aide.health import NO_DOCTOR, habits, number

SEXES = {"female": "Female", "male": "Male"}
GOALS = {"lose": "Lose weight", "maintain": "Maintain", "gain": "Build muscle"}
ACTIVITIES = {
    "sedentary": ("Sedentary", 1.2),
    "light": ("Light", 1.375),
    "moderate": ("Moderate", 1.55),
    "high": ("High", 1.725),
    "very_high": ("Very high", 1.9),
}
LEVELS = list(ACTIVITIES)
# Other ways of writing each value (also in Spanish).
SYNONYMS = {
    "woman": "female",
    "f": "female",
    "mujer": "female",
    "man": "male",
    "m": "male",
    "hombre": "male",
    "lose weight": "lose",
    "perder": "lose",
    "keep": "maintain",
    "mantener": "maintain",
    "build muscle": "gain",
    "ganar": "gain",
    "sedentaria": "sedentary",
    "light": "light",
    "moderada": "moderate",
    "alta": "high",
    "very high": "very_high",
    "muy alta": "very_high",
}
# Calories subtracted from or added to expenditure, and grams of protein per kilo, depending on the
# goal.
KCAL_SETTING = {"lose": -400, "maintain": 0, "gain": 250}
PROTEIN_G_KG = {"lose": 1.6, "maintain": 1.4, "gain": 1.8}
# It never recommends eating less than this.
MINIMAL_KCAL = {"female": 1200, "male": 1500}
FIELDS = ("sex", "birth", "height", "goal", "activity", "water")
MIN_STEP_DAYS = 3


class InvalidProfile(ValueError):
    """What was written is not valid; the message is for the user."""


async def profile(db: Any) -> dict[str, str]:
    return {f["key"]: f["value"] for f in await db.everything("SELECT * FROM health_profile")}


def validate(field: str, value: str, today: date) -> str:
    """The clean value of a profile field, or InvalidProfile."""
    value = " ".join(value.strip().lower().replace("_", " ").split())
    value = SYNONYMS.get(value, value).replace(" ", "_")
    if field == "sex" and value in SEXES:
        return value
    if field == "goal" and value in GOALS:
        return value
    if field == "activity" and value in ACTIVITIES:
        return value
    if field == "height":
        try:
            cm = float(value.replace(",", ".").removesuffix("cm").strip("_ "))
        except ValueError:
            cm = 0
        if 0 < cm < 3:
            cm *= 100  # “1.65” in meters
        if 120 <= cm <= 220:
            return str(round(cm))
        raise InvalidProfile("Your height, in centimeters: for example “/health height 165”.")
    if field == "birth":
        day = _complete_date(value)
        if day is not None and 12 <= age(day, today) <= 100:
            return day.isoformat()
        raise InvalidProfile("Your date of birth, like “/health born 1995-03-14”.")
    if field == "water":
        try:
            ml = float(value.replace(",", ".").removesuffix("ml").strip("_ "))
        except ValueError:
            ml = 0
        if 0 < ml < 10:
            ml *= 1000  # in liters
        if 500 <= ml <= 6000:
            return str(round(ml))
        raise InvalidProfile("Water per day, in milliliters: for example “/health water 2000”.")
    raise InvalidProfile("I don't understand that value.")


def _complete_date(value: str) -> date | None:
    """A date with its year: YYYY-MM-DD, DD-MM-YYYY, DD.MM.YYYY or DD/MM/YYYY."""
    for format_ in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(value.strip(), format_).date()
        except ValueError:
            continue
    return None


async def put(db: Any, field: str, value: str, today: date) -> str:
    """Saves a health profile field after validating it and returns the clean value."""
    clean = validate(field, value, today)
    await db.run(
        "INSERT INTO health_profile(key, value, updated) VALUES (?, ?, ?) ON CONFLICT(key) "
        "DO UPDATE SET value=excluded.value, updated=excluded.updated",
        (field, clean, now()),
    )
    return clean


def age(birth: date, today: date) -> int:
    return today.year - birth.year - ((today.month, today.day) < (birth.month, birth.day))


def mifflin(sex: str, weight: float, height: float, years: int) -> float:
    """Resting expenditure (Mifflin-St Jeor)."""
    base = 10 * weight + 6.25 * height - 5 * years
    return base + (5 if sex == "male" else -161)


def measured_level(mean_steps: float | None, steps_days: int, sessions: int) -> str | None:
    """The activity level of the last week from steps (on average) and gym sessions; None if
    there are no steps for enough days."""
    if mean_steps is None or steps_days < MIN_STEP_DAYS:
        return None
    if mean_steps < 5000:
        i = 0
    elif mean_steps < 7500:
        i = 1
    elif mean_steps < 10000:
        i = 2
    else:
        i = 3
    if sessions >= 3:
        i += 1
    return LEVELS[min(i, len(LEVELS) - 1)]


@dataclass
class Goals:
    """The daily goals: rest and spend energy, calories, protein, water, level and weight."""

    rest: float
    spend: float
    kcal: float
    protein: float
    water: float
    level: str
    measured: bool
    weight: float


async def calculate(db: Any, today: date) -> tuple[Goals | None, list[str]]:
    """The day's goals, or None and what is missing to calculate them."""
    from aide.health import gym

    p = await profile(db)
    weight = await habits.last_weight(db)
    missing = []
    if "sex" not in p:
        missing.append("your sex")
    if "birth" not in p:
        missing.append("your date of birth")
    if "height" not in p:
        missing.append("your height")
    if weight is None:
        missing.append("your weight (with /weight)")
    if missing:
        return None, missing
    years = age(date.fromisoformat(p["birth"]), today)
    rest = mifflin(p["sex"], float(weight), float(p["height"]), years)
    since = today - timedelta(days=6)
    steps = await habits.by_day(db, "steps", since, today)
    sessions = (await gym.week(db, today))["sessions"]
    mean = sum(steps.values()) / len(steps) if steps else None
    measured = measured_level(mean, len(steps), sessions)
    level = measured or p.get("activity") or "light"
    spend = rest * ACTIVITIES[level][1]
    goal = p.get("goal") or "maintain"
    kcal = max(spend + KCAL_SETTING[goal], MINIMAL_KCAL.get(p["sex"], 1200))
    return (
        Goals(
            rest=rest,
            spend=spend,
            kcal=round(kcal / 10) * 10,
            protein=round(float(weight) * PROTEIN_G_KG[goal]),
            water=float(p.get("water") or habits.WATER_GOAL),
            level=level,
            measured=measured is not None,
            weight=float(weight),
        ),
        [],
    )


async def eaten(db: Any, since: date, until: date) -> dict[date, dict[str, float]]:
    """What was eaten each day (from the meals already reviewed)."""
    rows = await db.everything(
        "SELECT date, SUM(kcal) AS kcal, SUM(protein) AS protein, SUM(carbs) AS carbs, "
        "SUM(fats) AS fats, COUNT(*) AS meals FROM health_meals WHERE state='items' AND "
        "date >= ? AND date <= ? GROUP BY date",
        (since.isoformat(), until.isoformat()),
    )
    return {date.fromisoformat(f.pop("date")): f for f in rows}


async def summary(db: Any, today: date) -> dict[str, Any]:
    """Today and the week against the goals: what /health, the panel and “Today” show."""
    goals, missing = await calculate(db, today)
    days = await eaten(db, today - timedelta(days=6), today)
    from_today = days.get(today, {"kcal": 0.0, "protein": 0.0, "carbs": 0.0, "fats": 0.0})
    week = [d for day, d in days.items() if day != today]
    return {
        "goals": goals,
        "missing": missing,
        "today": from_today,
        "habits": await habits.of_day(db, today),
        "week_days": len(days),
        "kcal_mean": sum(d["kcal"] or 0 for d in days.values()) / len(days) if days else None,
        "protein_mean": (
            sum(d["protein"] or 0 for d in days.values()) / len(days) if days else None
        ),
        "complete_days": len(week),
    }


def text(r: dict[str, Any], with_habits: bool = True) -> str:
    """The summary of the day and the week for /health (in the panel, without the habits, which
    have their own tab)."""
    o: Goals | None = r["goals"]
    h = r["habits"]
    lines = []
    kcal = r["today"]["kcal"] or 0
    protein = r["today"]["protein"] or 0
    if o is not None:
        lines.append(
            f"Today you're at {number(kcal)} of about {number(o.kcal)} kcal and {number(protein)} "
            f"of {number(o.protein)} g of protein."
        )
    else:
        lines.append(f"Today you're at {number(kcal)} kcal and {number(protein)} g of protein.")
    parts = []
    if h.get("water"):
        meta = o.water if o else habits.WATER_GOAL
        parts.append(f"water {number(h['water'] / 1000, 1)} of {number(meta / 1000, 1)} l")
    if h.get("steps") is not None:
        parts.append(f"{number(h['steps'])} steps")
    if h.get("sleep") is not None:
        parts.append(f"you slept {habits.hours(h['sleep'])}")
    if parts and with_habits:
        phrase = ", ".join(parts)
        lines.append(phrase[0].upper() + phrase[1:] + ".")
    if r["kcal_mean"] is not None and r["week_days"] > 1:
        phrase = f"This week, {number(r['kcal_mean'])} kcal a day on average"
        if o is not None:
            difference = r["kcal_mean"] - o.kcal
            if abs(difference) < 100:
                phrase += ", on target"
            else:
                phrase += f", {number(abs(difference))} {'over' if difference > 0 else 'under'}"
        lines.append(phrase + ".")
    if o is not None:
        level = ACTIVITIES[o.level][0].lower()
        from_where = "from your steps and the gym" if o.measured else "the one you told me"
        lines.append(
            f"You burn about {number(o.spend)} kcal a day (activity {level}, {from_where})."
        )
    else:
        lines.append(
            "To work out your target I still need "
            + _items(r["missing"])
            + ". Send /health profile."
        )
    lines.append(NO_DOCTOR)
    return "\n".join(lines)


def _items(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def profile_text(p: dict[str, str], today: date) -> str:
    """Describes the user's health profile and what is still missing."""
    lines = ["Your health profile (I only use it for the numbers; the agent never sees it)."]
    lines.append(f"{SEXES[p['sex']]}." if p.get("sex") in SEXES else "Sex not set.")
    if p.get("birth"):
        lines.append(f"You're {age(date.fromisoformat(p['birth']), today)}.")
    else:
        lines.append("No date of birth: “/health born 1995-03-14”.")
    lines.append(
        f"Height {p['height']} cm." if p.get("height") else "No height: “/health height 165”."
    )
    lines.append(f"Goal: {GOALS.get(p.get('goal', ''), 'not set').lower()}.")
    lines.append(f"Activity: {ACTIVITIES.get(p.get('activity', ''), ('not set', 0))[0].lower()}.")
    water = float(p.get("water") or habits.WATER_GOAL)
    lines.append(f"Water per day: {number(water / 1000, 1)} l.")
    return "\n".join(lines)
