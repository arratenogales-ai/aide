"""The gym, all by code.

Each line is an exercise: “squat 4x8 60”, “bench press 3x10 40 kg”, “pull-ups 3x8” (no
weight), “row 3 sets of 12 with 30 kg” or in pounds (“curl 3x10 25 lb”). The name is
looked up in a list of exercises with their synonyms in several languages (English,
Spanish, French, German and Italian) and their muscle group; what it does not know, it
asks about with buttons for the closest matches, and if it matches none, it asks the
user to write it another way. None of this goes through the model.

The weekly summary counts sessions (days with exercise), sets, volume per muscle group
(sets × repetitions × kg) and the week's records with the Epley estimated 1RM (weight ×
(1 + repetitions / 30)); for exercises without weight, the record is in repetitions.
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Any

from aide.core.db import now
from aide.health import number, plural

GROUPS = ("legs", "glutes", "back", "chest", "shoulders", "biceps", "triceps", "core")

# Each exercise: its muscle group and its other names (English, Spanish, French, German and
# Italian).
EXERCISES: dict[str, tuple[str, tuple[str, ...]]] = {
    "squat": (
        "legs",
        ("squats", "back squat", "sentadilla", "sentadillas", "kniebeuge", "accosciata"),
    ),
    "front squat": ("legs", ("sentadilla frontal", "frontkniebeuge")),
    "bulgarian split squat": (
        "legs",
        ("split squat", "bulgarian squat", "sentadilla búlgara", "búlgaras", "bulgara"),
    ),
    "goblet squat": ("legs", ("goblet", "sentadilla goblet")),
    "leg press": ("legs", ("prensa", "prensa de piernas", "presse à cuisses", "beinpresse")),
    "lunges": (
        "legs",
        ("lunge", "walking lunges", "zancadas", "zancada", "estocadas", "fentes", "affondi"),
    ),
    "leg extension": (
        "legs",
        ("quad extension", "extensión de cuádriceps", "extensión de piernas", "beinstrecker"),
    ),
    "leg curl": ("legs", ("hamstring curl", "curl femoral", "femoral", "isquios", "beinbeuger")),
    "calf raises": ("legs", ("calf raise", "calves", "gemelos", "elevación de talones")),
    "deadlift": (
        "back",
        ("conventional deadlift", "peso muerto", "soulevé de terre", "kreuzheben", "stacco"),
    ),
    "romanian deadlift": (
        "legs",
        ("rdl", "stiff leg deadlift", "peso muerto rumano", "rumänisches kreuzheben"),
    ),
    "hip thrust": ("glutes", ("hip thrusts", "empuje de cadera")),
    "glute bridge": ("glutes", ("bridge", "puente de glúteo", "puente", "pont fessier")),
    "hip abduction": (
        "glutes",
        ("abductor", "abductors", "glute kickback", "abductores", "abducción"),
    ),
    "hip adduction": ("legs", ("adductor", "adductors", "aductores", "aducción")),
    "bench press": (
        "chest",
        (
            "bench",
            "flat bench",
            "chest press",
            "press banca",
            "press de banca",
            "banca",
            "développé couché",
            "bankdrücken",
            "panca piana",
        ),
    ),
    "incline bench press": (
        "chest",
        ("incline press", "incline bench", "press inclinado", "press banca inclinado"),
    ),
    "chest fly": ("chest", ("flyes", "flys", "fly", "pec deck", "aperturas", "contractor")),
    "push-ups": (
        "chest",
        ("push ups", "push up", "pushups", "flexiones", "lagartijas", "pompes", "liegestütze"),
    ),
    "dips": ("triceps", ("parallel bar dips", "fondos", "fondos en paralelas")),
    "overhead press": (
        "shoulders",
        (
            "ohp",
            "shoulder press",
            "military press",
            "press militar",
            "press de hombros",
            "schulterdrücken",
        ),
    ),
    "lateral raises": (
        "shoulders",
        ("lateral raise", "side raises", "elevaciones laterales", "laterales", "seitheben"),
    ),
    "face pull": ("shoulders", ("face pulls", "tirón a la cara")),
    "pull-ups": (
        "back",
        (
            "pull ups",
            "pull up",
            "pullups",
            "chin ups",
            "chin up",
            "chinups",
            "dominadas",
            "tractions",
            "klimmzüge",
            "trazioni",
        ),
    ),
    "lat pulldown": (
        "back",
        ("pulldown", "lat pull down", "jalón al pecho", "jalón", "polea al pecho", "latzug"),
    ),
    "barbell row": ("back", ("row", "bent over row", "remo", "remo con barra", "rudern")),
    "dumbbell row": ("back", ("one arm row", "remo con mancuerna", "remo a una mano")),
    "cable row": ("back", ("seated cable row", "seated row", "remo en polea", "remo sentado")),
    "biceps curl": (
        "biceps",
        (
            "curl",
            "bicep curl",
            "barbell curl",
            "dumbbell curl",
            "biceps",
            "curl de bíceps",
            "curl con barra",
            "curl con mancuernas",
            "bíceps",
        ),
    ),
    "hammer curl": ("biceps", ("hammer", "curl martillo", "martillo")),
    "triceps extension": (
        "triceps",
        (
            "triceps",
            "tricep extension",
            "skull crushers",
            "pushdown",
            "triceps pushdown",
            "tríceps",
            "extensión de tríceps",
            "press francés",
            "tríceps en polea",
        ),
    ),
    "plank": ("core", ("planks", "plancha", "planchas", "gainage")),
    "crunches": ("core", ("crunch", "sit ups", "sit-ups", "abs", "abdominales")),
    "leg raises": ("core", ("leg raise", "hanging leg raises", "elevación de piernas")),
}
STOP_WORDS = {
    "of",
    "the",
    "with",
    "on",
    "an",
    "and",
    "de",
    "del",
    "con",
    "en",
    "al",
    "a",
    "la",
    "el",
    "los",
    "las",
    "y",
}
POUND = 0.45359237
MAX_SERIES, MAX_REPS, MAX_WEIGHT = 20, 200, 500.0
# What leaves the exercise as “sets x repetitions”: 4x8, 4 x 8, 4×8, 4*8, “4 sets of 8” or “4 series
# de 8”.
RE_SERIES = re.compile(
    r"(?<![\d.,])(\d{1,2})\s*(?:[x×*]|sets?\s+of|series?\s+de)\s*(\d{1,3})(?![\d])"
    r"(?:\s*(?:reps?|repetitions|repeticiones)\b)?",
    re.IGNORECASE,
)
WEIGHT_UNITS = r"kg|kilos?|lbs?|pounds?|libras?"
RE_WEIGHT = re.compile(
    rf"(?:@|[x×*]|\bwith\b|\bat\b|\bcon\b|\ba\b)?\s*\+?(\d{{1,3}}(?:[.,]\d{{1,2}})?)\s*"
    rf"({WEIGHT_UNITS})?\b",
    re.IGNORECASE,
)


def key(text: str) -> str:
    """The name for looking it up: without accents or capitals, without stop words and in the
    singular."""
    t = unicodedata.normalize("NFKD", text.lower())
    t = "".join(c for c in t if not unicodedata.combining(c))
    words = []
    for p in re.sub(r"[^a-z0-9]+", " ", t).split():
        if p in STOP_WORDS:
            continue
        if len(p) > 3 and p.endswith("s") and not p.endswith("ss"):
            p = p[:-1]
        words.append(p)
    return " ".join(words)


def _index() -> dict[str, str]:
    index: dict[str, str] = {}
    for name, (_exercise_group, other) in EXERCISES.items():
        for n in (name, *other):
            index.setdefault(key(n), name)
    return index


INDEX = _index()


def search_exercise(name: str) -> str | None:
    """The exercise in the list with that name or synonym; if it is not whole, the longest
    synonym it contains as words (“dumbbell bench press” is bench press)."""
    k = key(name)
    if not k:
        return None
    if k in INDEX:
        return INDEX[k]
    surrounded = f" {k} "
    candidates = [s for s in INDEX if f" {s} " in surrounded]
    if candidates:
        return INDEX[max(candidates, key=len)]
    return None


def similar(name: str, how_many: int = 3) -> list[str]:
    """The exercises that look most alike, to ask about."""
    k = key(name)
    seen: list[str] = []
    for s in difflib.get_close_matches(k, list(INDEX), n=8, cutoff=0.6):
        if INDEX[s] not in seen:
            seen.append(INDEX[s])
    return seen[:how_many]


@dataclass
class Line:
    """One logged gym exercise line: name, sets, reps and weight."""

    text: str
    name: str
    series: int
    reps: int
    weight: float | None
    exercise: str | None

    @property
    def exercise_group(self) -> str | None:
        return EXERCISES[self.exercise][0] if self.exercise else None


def _to_kilos(value: str, unit: str | None) -> float:
    weight = float(value.replace(",", "."))
    if unit and unit.lower().startswith(("lb", "pound", "libra")):
        weight = round(weight * POUND, 1)
    return weight


def read_line(text: str) -> Line | None:
    """A line with “sets x repetitions”, or None if it is not one. The exercise stays None if
    it is not known (and is asked about)."""
    t = " ".join(text.strip().strip("-•*·").split())
    m = RE_SERIES.search(t)
    if m is None:
        return None
    series, reps = int(m[1]), int(m[2])
    if not (1 <= series <= MAX_SERIES and 1 <= reps <= MAX_REPS):
        return None
    name, rest = t[: m.start()].strip(" :,-"), t[m.end() :].strip()
    weight: float | None = None
    if rest:
        p = RE_WEIGHT.fullmatch(rest.strip(" ,;"))
        if p is None:
            return None
        weight = _to_kilos(p[1], p[2])
    else:
        # The weight first: “squat 60 kg 4x8”.
        p = re.search(rf"\s(\d{{1,3}}(?:[.,]\d{{1,2}})?)\s*({WEIGHT_UNITS})$", name, re.I)
        if p is not None:
            weight = _to_kilos(p[1], p[2])
            name = name[: p.start()].strip()
    if not name or (weight is not None and not 0 < weight <= MAX_WEIGHT):
        return None
    return Line(
        text=t,
        name=name,
        series=series,
        reps=reps,
        weight=weight,
        exercise=search_exercise(name),
    )


def read(text: str) -> list[Line] | None:
    """The lines of a gym message, or None if it is not one: all its lines must have “sets x
    repetitions” (a normal message is not mistaken for a session)."""
    lines = [x for x in re.split(r"[\n;]+", text) if x.strip()]
    if not lines:
        return None
    already_read = [read_line(x) for x in lines]
    if any(x is None for x in already_read):
        return None
    return [x for x in already_read if x is not None]


def one_rm(weight: float | None, reps: int) -> float | None:
    """The estimated 1RM with the Epley formula (with 1 repetition, the weight itself)."""
    if not weight:
        return None
    return weight if reps == 1 else round(weight * (1 + reps / 30), 1)


async def note(db: Any, lines: list[Line], date_: date, origin: str) -> int:
    """Saves the lines with a known exercise. Returns how many."""
    done = 0
    for x in lines:
        if x.exercise is None:
            continue
        await db.run(
            "INSERT INTO health_gym(date, created, exercise, exercise_group, series, reps, weight, "
            "text, origin) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                date_.isoformat(),
                now(),
                x.exercise,
                x.exercise_group,
                x.series,
                x.reps,
                x.weight,
                x.text[:200],
                origin,
            ),
        )
        done += 1
    return done


def describe(x: Line) -> str:
    weight = f" at {format_weight(x.weight)}" if x.weight else ""
    return f"{x.exercise} {x.series}x{x.reps}{weight}"


def format_weight(weight: float | None) -> str:
    """Formats a weight in kg, or bodyweight when there is none."""
    if weight is None:
        return "bodyweight"
    text = f"{weight:.1f}".rstrip("0").rstrip(".")
    return f"{text} kg"


# --- summaries ---------------------------------------------------------------------------------


def monday(day: date) -> date:
    return day - timedelta(days=day.weekday())


async def rows(db: Any, since: date, until: date) -> list[dict[str, Any]]:
    """Returns the gym rows between two dates, in order."""
    return await db.everything(
        "SELECT * FROM health_gym WHERE date >= ? AND date <= ? ORDER BY date, id",
        (since.isoformat(), until.isoformat()),
    )


def _best(f: dict[str, Any]) -> tuple[float, str]:
    """What counts for a record: the estimated 1RM, or the repetitions without weight."""
    rm = one_rm(f["weight"], f["reps"])
    return (rm, "rm") if rm is not None else (float(f["reps"]), "reps")


async def week(db: Any, today: date, days: int = 7) -> dict[str, Any]:
    """The last `days` days: sessions, sets, volume per group and records (compared with
    everything before)."""
    since = today - timedelta(days=days - 1)
    current = await rows(db, since, today)
    by_group: dict[str, dict[str, float]] = {}
    for f in current:
        g = by_group.setdefault(f["exercise_group"], {"series": 0, "volume": 0.0})
        g["series"] += f["series"]
        g["volume"] += f["series"] * f["reps"] * (f["weight"] or 0)
    records = []
    for exercise in sorted({f["exercise"] for f in current}):
        best = max((f for f in current if f["exercise"] == exercise), key=lambda f: _best(f))
        value, kind = _best(best)
        priors = await db.everything(
            "SELECT weight, reps FROM health_gym WHERE exercise=? AND date < ?",
            (exercise, since.isoformat()),
        )
        previous_items = [_best(a) for a in priors]
        previous = max((v for v, t in previous_items if t == kind), default=None)
        if previous is None or value > previous:
            records.append(
                {
                    "exercise": exercise,
                    "kind": kind,
                    "value": value,
                    "weight": best["weight"],
                    "reps": best["reps"],
                    "first": previous is None,
                }
            )
    return {
        "since": since,
        "sessions": len({f["date"] for f in current}),
        "series": sum(f["series"] for f in current),
        "volume": sum(g["volume"] for g in by_group.values()),
        "by_group": [{"exercise_group": g, **by_group[g]} for g in GROUPS if g in by_group],
        "records": records,
        "rows": current,
    }


def record_text(r: dict[str, Any]) -> str:
    """Describes a gym record as text, either an estimated 1RM or a rep count."""
    if r["kind"] == "rm":
        return (
            f"{r['exercise']} (estimated 1RM {format_weight(r['value'])}, "
            f"{r['reps']} at {format_weight(r['weight'])})"
        )
    return f"{r['exercise']} ({r['reps']} reps)"


def week_text(s: dict[str, Any]) -> str:
    """Summarizes the week at the gym: sessions, sets, muscle groups and new records."""
    if not s["sessions"]:
        return "Nothing logged at the gym this week."
    lines = [f"This week, {plural(s['sessions'], 'session')} and {plural(s['series'], 'set')}."]
    groups = [
        f"{g['exercise_group']} {plural(int(g['series']), 'set')}"
        + (f" ({volume_number(g['volume'])})" if g["volume"] else "")
        for g in s["by_group"]
    ]
    if groups:
        lines.append("By muscle group: " + ", ".join(groups) + ".")
    new = [r for r in s["records"] if not r["first"]]
    if new:
        lines.append("New records: " + "; ".join(record_text(r) for r in new[:4]) + ".")
    return "\n".join(lines)


def volume_number(kg: float) -> str:
    return f"{number(kg)} kg"


async def evolution(db: Any, exercise: str, limit: int = 30) -> list[dict[str, Any]]:
    """Per day, the best of an exercise (estimated 1RM or repetitions), from oldest to newest,
    with at most the last `limit` days."""
    by_day: dict[str, tuple[float, str]] = {}
    for f in await db.everything(
        "SELECT date, weight, reps FROM health_gym WHERE exercise=? ORDER BY date",
        (exercise,),
    ):
        value = _best(f)
        if f["date"] not in by_day or value > by_day[f["date"]]:
            by_day[f["date"]] = value
    days = sorted(by_day)[-limit:]
    return [{"date": d, "value": by_day[d][0], "kind": by_day[d][1]} for d in days]


async def done_exercises(db: Any) -> list[str]:
    """Returns the exercises done, most recent first."""
    return [
        f["exercise"]
        for f in await db.everything(
            "SELECT exercise, MAX(date) AS last FROM health_gym GROUP BY exercise "
            "ORDER BY last DESC"
        )
    ]
