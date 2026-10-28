"""The menstrual cycle, all by code.

Period days are noted (`/period today`, `/period yesterday`, a date, `/period end` for
the last day, or by tapping the days in the panel calendar). A cycle starts on the first
day of each streak: two period days separated by a gap of one or two days are the same
period.

The estimate: the average length of the last 6 cycles without the atypical ones (outside
21 to 45 days or more than 7 days from the median), the next period is the last start
plus that average, ovulation 14 days before the next one and the fertile days from 5
days before to 1 after ovulation. With fewer than 3 cycles it says so, and if they vary
a lot, it also says so. It is an approximate estimate, not medical advice.

The Telegram alert goes out 2 days before the expected period, only once per cycle, from
9:00 and when online, like the ones for your documents.
"""

from __future__ import annotations

import logging
import re
import statistics
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aide.core.db import now
from aide.health import SHORT_MONTHS, day_name, month_day, notify, plural, today_of

log = logging.getLogger(__name__)

# A gap of up to this many days between two period days is still the same period.
SAME_SLOT_PERIOD = 2
MIN_CYCLE, MAX_CYCLE = 21, 45
# A cycle more than this many days from the median is atypical (a skipped month, one noted wrongly).
FAR_FROM_MEDIAN = 7
MEAN_CYCLES = 6
MIN_CYCLES = 3
# If the cycles used range from X to Y days and the difference exceeds this, a warning is given.
LARGE_VARIATION = 7
DEFAULT_CYCLE = 28
LUTEAL_DAYS = 14
FERTILE_BEFORE, FERTILE_AFTER = 5, 1
# The period length drawn in the forecast, if there are no complete periods noted.
DEFAULT_DURATION = 5
# The alert, 2 days before and from 9:00 (like the ones for documents).
NOTICE_DAYS = 2
NOTICE_HOUR = 9
NOTICE_KEY = "health:period_notice"
# A period longer than this many days is not filled in with /period end (it is something else:
# better by hand).
MAX_DURATION = 12
# What closes a period: “/period end” (and in Spanish, “fin”).
END_WORDS = ("end", "ended", "over", "done", "fin", "final")


@dataclass
class Estimate:
    """An estimate of the user's cycle from past start dates, with the next dates."""

    starts: list[date]
    # The cycle lengths that count for the average (without atypical ones, the last 6).
    used: list[int] = field(default_factory=list)
    outliers: list[int] = field(default_factory=list)
    mean: float | None = None
    next_run: date | None = None
    ovulation: date | None = None
    fertile: tuple[date, date] | None = None
    duration: int = DEFAULT_DURATION
    few: bool = True
    variable: bool = False
    by_default: bool = False

    @property
    def last(self) -> date | None:
        return self.starts[-1] if self.starts else None


def streaks(days: list[date]) -> list[tuple[date, date]]:
    """Each period (first and last day) from the noted days."""
    output: list[tuple[date, date]] = []
    for day in sorted(set(days)):
        if output and (day - output[-1][1]).days <= SAME_SLOT_PERIOD + 1:
            output[-1] = (output[-1][0], day)
        else:
            output.append((day, day))
    return output


def durations(starts: list[date]) -> list[int]:
    return [(b - a).days for a, b in zip(starts, starts[1:], strict=False)]


def filter_items(lengths: list[int]) -> tuple[list[int], list[int]]:
    """(the ones that count, the atypical ones): outside 21 to 45 days or far from the median."""
    in_range = [x for x in lengths if MIN_CYCLE <= x <= MAX_CYCLE]
    if not in_range:
        return [], list(lengths)
    medium = statistics.median(in_range)
    good = [
        x for x in lengths if MIN_CYCLE <= x <= MAX_CYCLE and abs(x - medium) <= FAR_FROM_MEDIAN
    ]
    return good, [x for x in lengths if x not in good]


def estimate(days: list[date]) -> Estimate:
    """The cycle estimate from the period days."""
    periods = streaks(days)
    starts = [a for a, _ in periods]
    e = Estimate(starts=starts)
    if not starts:
        return e
    good, e.outliers = filter_items(durations(starts))
    e.used = good[-MEAN_CYCLES:]
    e.few = len(e.used) < MIN_CYCLES
    if e.used:
        e.mean = sum(e.used) / len(e.used)
        e.variable = max(e.used) - min(e.used) > LARGE_VARIATION
    else:
        e.mean = float(DEFAULT_CYCLE)
        e.by_default = True
    # The periods already finished (not the last one, which may still be going): their typical
    # length.
    complete = [(b - a).days + 1 for a, b in periods[:-1]]
    if complete:
        e.duration = round(statistics.median(complete[-MEAN_CYCLES:]))
    e.next_run = starts[-1] + timedelta(days=round(e.mean))
    e.ovulation = e.next_run - timedelta(days=LUTEAL_DAYS)
    e.fertile = (
        e.ovulation - timedelta(days=FERTILE_BEFORE),
        e.ovulation + timedelta(days=FERTILE_AFTER),
    )
    return e


# --- what the user writes ----------------------------------------------------------------------


def _month_of(name: str) -> int | None:
    short = name[:3].lower()
    for i, m in enumerate(SHORT_MONTHS, 1):
        if m.lower() == short:
            return i
    return None


def date_of(text: str, today: date) -> date | None:
    """“today”, “yesterday”, “24-10”, “24/10/2026”, “24.10.26”, “2026-10-24”, “Oct 24” or “24
    Oct” (the day goes before the month; also “hoy” and “ayer”). None if it is not
    understood or is in the future."""
    t = text.strip().lower().rstrip(".")
    if t in ("", "today", "hoy"):
        return today
    if t in ("yesterday", "ayer"):
        return today - timedelta(days=1)
    if t in ("anteayer", "the day before yesterday"):
        return today - timedelta(days=2)
    no_year = False
    if m := re.fullmatch(r"(\d{4})-(\d{1,2})-(\d{1,2})", t):
        year, month, d = int(m[1]), int(m[2]), int(m[3])
    elif m := re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})(?:[-/.](\d{2}|\d{4}))?", t):
        d, month = int(m[1]), int(m[2])
        no_year = not m[3]
        year = int(m[3]) if m[3] else today.year
        if m[3] and len(m[3]) == 2:
            year += 2000
    elif (m := re.fullmatch(r"([a-z]{3,9})\.?\s+(\d{1,2})(?:st|nd|rd|th)?", t)) and _month_of(m[1]):
        month, d, year, no_year = _month_of(m[1]) or 0, int(m[2]), today.year, True
    elif (m := re.fullmatch(r"(\d{1,2})(?:st|nd|rd|th)?\s+([a-z]{3,9})\.?", t)) and _month_of(m[2]):
        month, d, year, no_year = _month_of(m[2]) or 0, int(m[1]), today.year, True
    else:
        return None
    try:
        day = date(year, month, d)
        if no_year and day > today:
            # “12-12” in January is last December.
            day = date(year - 1, month, d)
    except ValueError:
        return None
    if day > today or day < today - timedelta(days=3 * 366):
        return None
    return day


async def days(db: Any) -> list[date]:
    """Returns the recorded period days from the database."""
    return [
        date.fromisoformat(f["day"]) for f in await db.everything("SELECT day FROM health_period")
    ]


async def put(db: Any, day: date, origin: str) -> bool:
    """Notes a period day. True if it was not there."""
    return bool(
        await db.change(
            "INSERT OR IGNORE INTO health_period(day, created, origin) VALUES (?, ?, ?)",
            (day.isoformat(), now(), origin),
        )
    )


async def remove(db: Any, day: date) -> bool:
    return bool(await db.change("DELETE FROM health_period WHERE day=?", (day.isoformat(),)))


async def toggle(db: Any, day: date, origin: str) -> bool:
    """Tapping a calendar day: notes it if it was not there and removes it if it was. True if
    it ends up noted."""
    if await put(db, day, origin):
        return True
    await remove(db, day)
    return False


async def end(db: Any, last: date, origin: str) -> int | None:
    """`/period end`: fills in from the first day of the last period up to `last`. Returns
    the days the period has, or None if there is no started period that fits."""
    periods = streaks(await days(db))
    previous = [r for r in periods if r[0] <= last]
    if not previous:
        return None
    start = previous[-1][0]
    duration = (last - start).days + 1
    if duration > MAX_DURATION:
        return None
    for i in range(duration):
        await put(db, start + timedelta(days=i), origin)
    # Anything extra that was noted in that same period, after the last day, is removed.
    await db.change(
        "DELETE FROM health_period WHERE day > ? AND day <= ?",
        (last.isoformat(), previous[-1][1].isoformat()),
    )
    return duration


# --- texts -------------------------------------------------------------------------------------


def lines(e: Estimate, today: date) -> list[str]:
    """The estimate in short sentences."""
    if not e.starts:
        return ["I don't have any period logged yet. Send me “/period today” on the first day."]
    assert e.next_run is not None and e.ovulation is not None and e.fertile is not None
    output: list[str] = []
    missing = (e.next_run - today).days
    if missing >= 0:
        timing = "today" if missing == 0 else f"in {plural(missing, 'day')}"
        output.append(f"Your next period: around {day_name(e.next_run, today)} ({timing}).")
    else:
        output.append(
            f"It was due around {day_name(e.next_run, today)}: {plural(-missing, 'day')} late going "
            "by your cycles."
        )
    a, b = e.fertile
    if b >= today:
        output.append(
            f"Ovulation around {month_day(e.ovulation)}; fertile days {month_day(a)} to {month_day(b)}."
        )
    if e.by_default:
        output.append("I don't have a full cycle yet, so I'm counting 28 days.")
    else:
        n = len(e.used)
        output.append(
            f"Your cycles last about {round(e.mean or 0)} days "
            f"({'the last one' if n == 1 else f'average of {n}'})."
        )
        if e.few:
            output.append("With fewer than 3 cycles the estimate isn't reliable.")
    if e.variable:
        output.append(
            f"Your cycles vary a lot (from {min(e.used)} to {max(e.used)} days): the date may move."
        )
    return output


def text(e: Estimate, today: date) -> str:
    return "\n".join(lines(e, today))


def forecast_days(e: Estimate) -> set[date]:
    """The days of the expected next period, to draw them on the calendar."""
    if e.next_run is None:
        return set()
    return {e.next_run + timedelta(days=i) for i in range(e.duration)}


def fertile_days(e: Estimate) -> set[date]:
    """Returns the set of days in the estimated fertile window."""
    if e.fertile is None:
        return set()
    a, b = e.fertile
    return {a + timedelta(days=i) for i in range((b - a).days + 1)}


# --- loose questions and notice ----------------------------------------------------------------

# “When is my next period?”, “when do I ovulate” (and in Spanish, “¿cuándo me viene la regla?”):
# they are answered by code, never with the model.
RE_QUESTION = re.compile(
    r"^\W*(?:when\b.{0,40}\b(?:period|ovulat\w*|fertile)\b"
    r"|cu[aá]ndo\b.{0,40}\b(?:regla|periodo|per[ií]odo|ovulo|ovulaci[oó]n|f[eé]rtil(?:es)?)\b)",
    re.IGNORECASE | re.DOTALL,
)


def is_question(text_: str) -> bool:
    return len(text_) <= 100 and bool(RE_QUESTION.search(text_))


async def reply(engine: Any) -> None:
    e = estimate(await days(engine.db))
    await notify(engine, text(e, today_of(engine)))


async def review_notice(engine: Any, moment: datetime | None = None) -> bool:
    """The alert 2 days before the expected period: once per cycle, from 9:00 and when online.
    If the Mac was off, it goes out on return as long as the period has not yet arrived."""
    moment = moment or datetime.now(UTC)
    local = moment.astimezone(ZoneInfo(engine.config.time_zone))
    if local.hour < NOTICE_HOUR:
        return False
    today = local.date()
    e = estimate(await days(engine.db))
    if e.next_run is None or e.last is None:
        return False
    missing = (e.next_run - today).days
    if not 0 <= missing <= NOTICE_DAYS:
        return False
    # Once per cycle: the alert is noted with the start of the last period.
    if await engine.db.kv_read(NOTICE_KEY) == e.last.isoformat():
        return False
    timing = "today" if missing == 0 else ("tomorrow" if missing == 1 else f"in {missing} days")
    message = f"Going by your cycles, your period may start {day_name(e.next_run, today)}"
    message += "." if missing <= 1 else f" ({timing})."
    if not await notify(engine, message):
        return False  # offline: it is tried again on the next check
    await engine.db.kv_put(NOTICE_KEY, e.last.isoformat())
    log.info("cycle notice sent")
    return True
