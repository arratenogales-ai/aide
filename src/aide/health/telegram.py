"""Health in Telegram: commands, buttons and what the engine passes to it before
triage. None of this creates tasks or calls the model, except looking at a meal photo.

Commands: /period, /gym, /meals, /water, /sleep, /steps, /weight and /health. Buttons:
`sal:<kind>:<value>` (quick habits and profile), `gim:<key>:<n>` (which exercise it
was), `com:mir|del:<id>` (a meal) and `pec:<kind>:<key>` (the meal of a photo with no
caption).
"""

from __future__ import annotations

import logging
import secrets
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aide.bot.channel import Button, Entry
from aide.health import (
    cycle,
    day_name,
    gym,
    habits,
    meals,
    month_day,
    notify,
    number,
    plural,
    today_of,
)
from aide.health import recommendations as reco

if TYPE_CHECKING:
    from aide.core.engine import Engine

log = logging.getLogger(__name__)

GYM_KEY = "health:gim:"
WATER_BUTTONS = [[Button("+250 ml", "health:water:250"), Button("+500 ml", "health:water:500")]]
SLEEP_BUTTONS = [
    [Button(f"{h} h", f"health:sleep:{h}") for h in ("6", "6.5", "7")],
    [Button(f"{h} h", f"health:sleep:{h}") for h in ("7.5", "8", "9")],
]
GYM_HELP = (
    "Send me one exercise per line, for example “squat 4x8 60”, “bench press 3x10 40 kg” or "
    "“pull-ups 3x8”."
)
SAVED_PROFILE = "Saved to your health profile."
# What is written after /health to change the profile, and the field it is.
PROFILE_FIELDS = {
    "height": "height",
    "born": "birth",
    "birthday": "birth",
    "birth": "birth",
    "water": "water",
    "sex": "sex",
    "goal": "goal",
    "activity": "activity",
}


# --- commands ----------------------------------------------------------------------------------


async def period(engine: Engine, entry: Entry) -> None:
    """/period: the estimate; “/period today”, “yesterday” or a date notes the first day;
    “/period end” (with a date or without one, which is today) the last."""
    today = today_of(engine)
    arguments = entry.arguments.strip().lower()
    db = engine.db
    if not arguments:
        await notify(engine, cycle.text(cycle.estimate(await cycle.days(db)), today))
        return
    if arguments.split()[0] in cycle.END_WORDS:
        day = cycle.date_of(" ".join(arguments.split()[1:]), today)
        if day is None:
            await notify(engine, "I don't understand that date. Write it like “/period end 28-10”.")
            return
        duration = await cycle.end(db, day, "telegram")
        if duration is None:
            await notify(
                engine,
                "I can't find a period that started before that day (or it would last more than "
                f"{cycle.MAX_DURATION} days). Log the first day with “/period 24-10”.",
            )
            return
        await notify(engine, f"Logged: this period lasted {plural(duration, 'day')}.")
        return
    day = cycle.date_of(arguments, today)
    if day is None:
        await notify(
            engine,
            "I don't understand that date. Send “/period today”, “/period yesterday” or "
            "“/period 24-10” (day first; it can't be in the future).",
        )
        return
    new = await cycle.put(db, day, "telegram")
    e = cycle.estimate(await cycle.days(db))
    head = (
        f"Logged: period {day_name(day, today)}."
        if new
        else f"I already had your period logged {day_name(day, today)}."
    )
    await notify(engine, head + "\n" + cycle.text(e, today))


async def gym_cmd(engine: Engine, entry: Entry) -> None:
    """/gym with lines: notes them. With nothing: the week's summary."""
    if entry.arguments.strip():
        if not await note_gym(engine, entry.arguments):
            await notify(engine, GYM_HELP)
        return
    s = await gym.week(engine.db, today_of(engine))
    text = gym.week_text(s)
    if not s["sessions"]:
        text += "\n" + GYM_HELP
    await notify(engine, text)


async def meals_cmd(engine: Engine, entry: Entry) -> None:
    """/meals: today's; “/meals week”, those of the last 7 days."""
    today = today_of(engine)
    goals, _ = await reco.calculate(engine.db, today)
    goal = goals.kcal if goals else None
    if entry.arguments.strip().lower().startswith(("week", "semana")):
        await notify(engine, meals.week_text(await meals.week(engine.db, today), goal))
        return
    await notify(engine, meals.day_text(await meals.of_day(engine.db, today), goal))


async def _habit(engine: Engine, kind: str, text: str, origin: str = "telegram") -> None:
    today = today_of(engine)
    try:
        value = habits.READERS[kind](text)
    except habits.InvalidValue as e:
        await notify(engine, str(e))
        return
    await habits.note(engine.db, kind, value, today, origin)
    day = await habits.of_day(engine.db, today)
    p = await reco.profile(engine.db)
    if kind == "water":
        meta = float(p.get("water") or habits.WATER_GOAL)
        total = day["water"] or 0
        await notify(
            engine,
            f"+{number(value)} ml. Today you're at {number(total / 1000, 2)} of "
            f"{number(meta / 1000, 1)} l.",
            buttons=WATER_BUTTONS,
        )
    elif kind == "sleep":
        mean = (await habits.week_mean(engine.db, today))["sleep"]
        await notify(
            engine,
            f"Logged: you slept {habits.hours(value)}. This week's average, "
            f"{habits.hours(mean or value)}.",
        )
    elif kind == "steps":
        mean = (await habits.week_mean(engine.db, today))["steps"]
        await notify(
            engine,
            f"Logged: {number(value)} steps today. This week's average, {number(mean or value)}.",
        )
    else:
        await notify(engine, weight_text(value, await habits.weight_evolution(engine.db, 60)))


def weight_text(value: float, evolution: list[dict[str, Any]], head: str = "Logged:") -> str:
    """Describes a logged weight and how it changed since the first entry."""
    text = f"{head} {number(value, 1)} kg."
    if len(evolution) >= 2:
        first = evolution[0]
        since = month_day(date.fromisoformat(first["date"]))
        difference = value - first["value"]
        if abs(difference) >= 0.1:
            text += (
                f" Since {since}, {'+' if difference > 0 else '−'}{number(abs(difference), 1)} kg."
            )
        else:
            text += f" Same as on {since}."
    return text


async def water(engine: Engine, entry: Entry) -> None:
    await _habit(engine, "water", entry.arguments)


async def sleep(engine: Engine, entry: Entry) -> None:
    """Handles the sleep command, asking for the hours or logging them."""
    if not entry.arguments.strip():
        await notify(engine, "How long did you sleep?", buttons=SLEEP_BUTTONS)
        return
    await _habit(engine, "sleep", entry.arguments)


async def steps(engine: Engine, entry: Entry) -> None:
    """Handles the steps command, showing today's steps or logging them."""
    if not entry.arguments.strip():
        today = today_of(engine)
        day = await habits.of_day(engine.db, today)
        mean = (await habits.week_mean(engine.db, today))["steps"]
        parts = [f"Today, {number(day['steps'])} steps." if day["steps"] is not None else ""]
        if mean is not None:
            parts.append(f"This week's average, {number(mean)}.")
        parts.append("Log them with “/steps 8000”.")
        await notify(engine, " ".join(p for p in parts if p))
        return
    await _habit(engine, "steps", entry.arguments)


async def weight(engine: Engine, entry: Entry) -> None:
    """Handles the weight command, showing the latest weight or logging it."""
    if not entry.arguments.strip():
        evolution = await habits.weight_evolution(engine.db, 60)
        if not evolution:
            await notify(engine, "I don't have your weight yet. Log it with “/weight 61.5”.")
            return
        last = evolution[-1]
        await notify(engine, weight_text(last["value"], evolution, "Your latest weight,"))
        return
    await _habit(engine, "weight", entry.arguments)


def profile_buttons() -> list[list[Button]]:
    """Returns the buttons to set sex, goal, activity and water target in the profile."""
    return [
        [Button(n, f"health:sex:{c}") for c, n in reco.SEXES.items()],
        [Button(n, f"health:goal:{c}") for c, n in reco.GOALS.items()],
        [Button(n, f"health:activity:{c}") for c, (n, _f) in list(reco.ACTIVITIES.items())[:3]],
        [Button(n, f"health:activity:{c}") for c, (n, _f) in list(reco.ACTIVITIES.items())[3:]],
        [
            Button(f"Water {x} l", f"health:water_goal:{int(float(x) * 1000)}")
            for x in ("1.5", "2", "2.5")
        ],
    ]


async def health(engine: Engine, entry: Entry) -> None:
    """/health: today and the week against your goals. “/health profile” shows the profile with
    its buttons, and “/health height 165” or “/health born 1995-03-14” change it."""
    today = today_of(engine)
    parts = entry.arguments.strip().split(maxsplit=1)
    if not parts:
        r = await reco.summary(engine.db, today)
        await notify(
            engine, reco.text(r), buttons=[[Button("My health profile", "health:profile:ver")]]
        )
        return
    field = parts[0].lower()
    if field in ("profile", "perfil"):
        await notify(
            engine,
            reco.profile_text(await reco.profile(engine.db), today)
            + "\nTap to change it. Height and date of birth, by writing them: “/health height "
            "165”, “/health born 1995-03-14”.",
            buttons=profile_buttons(),
        )
        return
    if field in PROFILE_FIELDS and len(parts) == 2:
        try:
            await reco.put(engine.db, PROFILE_FIELDS[field], parts[1], today)
        except reco.InvalidProfile as e:
            await notify(engine, str(e))
            return
        await notify(engine, SAVED_PROFILE)
        return
    await notify(
        engine,
        "Send /health to see your day, “/health profile” for your profile, or “/health height "
        "165”.",
    )


HANDLERS = {
    "period": period,
    "gym": gym_cmd,
    "meals": meals_cmd,
    "water": water,
    "sleep": sleep,
    "steps": steps,
    "weight": weight,
    "health": health,
}


# --- the gym by text ---------------------------------------------------------------------------


async def note_gym(engine: Engine, text: str) -> bool:
    """A message with exercises: notes what it knows and asks about what it does not. False if
    it is not one."""
    lines = gym.read(text)
    if lines is None:
        return False
    today = today_of(engine)
    done = await gym.note(engine.db, lines, today, "telegram")
    known = [x for x in lines if x.exercise]
    if done:
        await notify(
            engine,
            "Logged: " + "; ".join(gym.describe(x) for x in known) + ".",
        )
    for x in lines:
        if x.exercise is None:
            await _ask_exercise(engine, x)
    return True


async def _ask_exercise(engine: Engine, x: gym.Line) -> None:
    options = gym.similar(x.name)
    if not options:
        await notify(
            engine,
            f"I don't know “{x.name}”. Write it another way (for example “bench press 3x10 40”).",
        )
        return
    key = secrets.token_hex(4)
    await engine.db.kv_put(
        GYM_KEY + key,
        {
            "series": x.series,
            "reps": x.reps,
            "weight": x.weight,
            "text": x.text,
            "date": today_of(engine).isoformat(),
            "options": options,
        },
    )
    await notify(
        engine,
        f"What is “{x.name}”?",
        buttons=[[Button(o, f"gym:{key}:{i}")] for i, o in enumerate(options)]
        + [[Button("None of these", f"gym:{key}:no")]],
    )


async def _gym_button(engine: Engine, key: str, choice: str) -> None:
    async with engine.db.lock(GYM_KEY + key):
        data = await engine.db.kv_read(GYM_KEY + key)
        if not data:
            await notify(engine, "I had already logged (or dropped) that one.")
            return
        await engine.db.kv_delete(GYM_KEY + key)
        if choice == "no" or not choice.isdigit() or int(choice) >= len(data["options"]):
            await notify(engine, "Not logged. " + GYM_HELP)
            return
        exercise = data["options"][int(choice)]
        x = gym.Line(
            text=data["text"],
            name=exercise,
            series=int(data["series"]),
            reps=int(data["reps"]),
            weight=data["weight"],
            exercise=exercise,
        )
        await gym.note(engine.db, [x], date.fromisoformat(data["date"]), "telegram")
        await notify(engine, f"Logged: {gym.describe(x)}.")


# --- what the engine passes before triage ------------------------------------------------------


async def free_text(engine: Engine, text: str) -> bool:
    """A message that is about the gym or a question about the cycle: it is handled by code and
    never reaches triage or the model. Returns whether it was one."""
    if cycle.is_question(text):
        await cycle.reply(engine)
        return True
    return await note_gym(engine, text)


def kind_buttons(key: str) -> list[list[Button]]:
    """Which meal a photo that arrived without a caption is."""
    kinds = list(meals.KINDS)
    return [
        [Button(meals.KIND_NAME[t], f"mealtype:{t}:{key}") for t in kinds[:2]],
        [Button(meals.KIND_NAME[t], f"mealtype:{t}:{key}") for t in kinds[2:]],
    ]


async def button(engine: Engine, datum: str) -> bool:
    """The health buttons. Returns whether it was one."""
    parts = datum.split(":")
    if len(parts) != 3:
        return False
    kind, a, b = parts
    if kind == "meal" and a in ("mir", "of") and b.isdigit():
        await meals.button(engine, a, int(b))
        return True
    if kind == "gym":
        await _gym_button(engine, a, b)
        return True
    if kind == "mealtype" and a in meals.KINDS:
        photo = engine.pending_photo
        if photo is None or photo.key != b:
            await notify(engine, "That photo is no longer waiting: send it to me again.")
            return True
        engine.pending_photo = None
        await meals.new(engine, a, photo.paths)
        return True
    if kind != "health":
        return False
    today = today_of(engine)
    if a in ("water", "sleep"):
        await _habit(engine, a, b)
    elif a == "profile":
        await health(engine, Entry("command", 0, command="health", arguments="profile"))
    elif a in ("sex", "goal", "activity", "water_goal"):
        field = "water" if a == "water_goal" else a
        try:
            await reco.put(engine.db, field, b, today)
        except reco.InvalidProfile as e:
            await notify(engine, str(e))
            return True
        await notify(engine, SAVED_PROFILE)
    else:
        log.warning("unknown health button: %s", datum)
    return True


async def photo(engine: Engine, text: str, files: list[Any]) -> bool:
    """A photo with the meal at the start of the caption."""
    return await meals.since_telegram(engine, text, [Path(a) for a in files])
