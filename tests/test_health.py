"""Health: cycle, gym, meals, habits, recommendations, panel and report, and that
no health data reaches the agent or the model except the photo of a meal. No network or
model (the photo's is the fake client)."""

from __future__ import annotations

import os
import re
import time
import zipfile
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from aide.bot.channel import Entry, FakeChannel
from aide.bot.commands import run_command
from aide.core.engine import Engine
from aide.health import cycle, gym, habits, meals, month_day, today_of
from aide.health import recommendations as reco
from aide.panel.app import COOKIE, Panel
from aide.simulate import SIMULATED_USER
from tests.fake_sdk import End, FakeClient

TOKEN = "token-de-prueba-salud"
D = date.fromisoformat
# No sentence about contraceptives in the interface or the messages.
NO_NOTICE = "contracept"


@pytest.fixture
async def engine(config_tmp):
    m = Engine(config_tmp, channel=FakeChannel(print_out=False))
    await m.start(workers=False)
    m.commands = run_command

    async def no_triage(*a, **k):
        raise AssertionError("a health message reached the triage")

    m.triage = no_triage
    yield m
    await m.stop()


@pytest.fixture
async def panel(engine):
    p = Panel(engine, token=TOKEN)
    transport = httpx.ASGITransport(app=p.app)
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://127.0.0.1:{p.port}", follow_redirects=False
    ) as client:
        r = await client.get(f"/enter?t={TOKEN}&go=/")
        assert COOKIE in r.cookies
        yield engine, p, client


async def text(m: Engine, t: str, **k) -> None:
    await m.receive(Entry("text", SIMULATED_USER, text=t, **k))


async def command(m: Engine, name: str, arguments: str = "") -> None:
    await m.receive(Entry("command", SIMULATED_USER, command=name, arguments=arguments, text=""))


async def button(m: Engine, datum: str) -> None:
    await m.receive(Entry("button", SIMULATED_USER, button_datum=datum))


def last(m: Engine) -> str:
    return m.channel.messages[-1].plain_text


def telegram_photo(m: Engine, name: str = "foto.jpg", side: int = 1600) -> Path:
    """As the bot leaves it: in the Telegram cache, in the message folder."""
    from PIL import Image

    folder = m.config.paths.agent_cache / "telegram" / str(time.monotonic_ns())
    folder.mkdir(parents=True)
    path = folder / name
    Image.new("RGB", (side, side * 3 // 4), (200, 120, 80)).save(path, "JPEG")
    return path


READ = {
    "is_meal": True,
    "foods": [
        {
            "name": "Fried egg",
            "quantity": 1,
            "unit": "unit",
            "grams": 50,
            "kcal": 90,
            "protein": 6.5,
            "carbs": 0.5,
            "fats": 7,
            "trust": "high",
        },
        {
            "name": "Wholemeal bread",
            "quantity": 2,
            "unit": "slice",
            "grams": 60,
            "kcal": 150,
            "protein": 6,
            "carbs": 26,
            "fats": 2,
            "trust": "medium",
        },
        {
            "name": "White rice",
            "quantity": 100,
            "unit": "g",
            "grams": 100,
            # 9,000 kcal does not match the macros: the macros take precedence.
            "kcal": 9000,
            "protein": 2.7,
            "carbs": 28,
            "fats": 0.3,
            "trust": "low",
        },
    ],
}


def photos_model(monkeypatch, output=READ):
    """The fake client answers with `output` and records what it saw."""

    def script(prompt, options, registry):
        return [End(structured=output, model="claude-sonnet-5-5")]

    monkeypatch.setattr(FakeClient, "script", staticmethod(script))
    return FakeClient.registry


# --- cycle ---------------------------------------------------------------------------------------


def _periods(starts: list[str], duration: int = 5) -> list[date]:
    return [D(i) + timedelta(days=k) for i in starts for k in range(duration)]


def test_cycle_estimate():
    # Cycles of 28, 30, 29, 60 (a month not logged: atypical), 28, 27 and 29 days.
    starts = [
        "2026-01-01",
        "2026-01-29",
        "2026-02-28",
        "2026-03-29",
        "2026-05-28",
        "2026-06-25",
        "2026-07-22",
        "2026-08-20",
    ]
    e = cycle.estimate(_periods(starts))
    assert e.outliers == [60]
    # The last 6 valid ones: 28, 30, 29, 28, 27, 29 -> 28.5.
    assert e.used == [28, 30, 29, 28, 27, 29]
    assert e.mean == pytest.approx(28.5)
    assert e.next_run == D("2026-08-20") + timedelta(days=28)  # round(28.5) == 28 (al par)
    assert e.ovulation == e.next_run - timedelta(days=14)
    assert e.fertile == (e.ovulation - timedelta(days=5), e.ovulation + timedelta(days=1))
    assert not e.few and not e.variable and e.duration == 5


def test_cycle_with_little_data_or_that_varies():
    assert cycle.estimate([]).next_run is None
    one = cycle.estimate(_periods(["2026-10-01"]))
    assert one.by_default and one.next_run == D("2026-10-29")
    two = cycle.estimate(_periods(["2026-09-01", "2026-09-30"]))
    assert two.few and two.used == [29]
    today = D("2026-10-05")
    lines = cycle.text(two, today)
    assert "fewer than 3 cycles" in lines and NO_NOTICE not in lines.lower()
    assert NO_NOTICE not in cycle.text(cycle.estimate([]), today).lower()
    # 22 and 35 days are in range, but vary a lot (more than 7 days): a warning is given.
    varies = cycle.estimate(_periods(["2026-06-01", "2026-06-23", "2026-07-28", "2026-08-25"]))
    assert varies.variable and "vary a lot" in cycle.text(varies, today)
    # Very far from the median (more than 7 days): out even if it is between 21 and 45.
    assert cycle.filter_items([28, 28, 29, 44]) == ([28, 28, 29], [44])
    assert cycle.filter_items([15, 50]) == ([], [15, 50])
    # Only the last 6 cycles count: the first two, of 32 days, no longer.
    long = [32, 32, 28, 28, 28, 28, 28, 28]
    starts = [D("2026-01-01")]
    for x in long:
        starts.append(starts[-1] + timedelta(days=x))
    six = cycle.estimate([d + timedelta(days=k) for d in starts for k in range(4)])
    assert six.used == [28] * 6 and six.mean == 28


def test_streaks_and_dates():
    days = [D("2026-10-01"), D("2026-10-02"), D("2026-10-04"), D("2026-10-29")]
    # A one-day gap is still the same period.
    assert cycle.streaks(days) == [
        (D("2026-10-01"), D("2026-10-04")),
        (D("2026-10-29"), D("2026-10-29")),
    ]
    today = D("2026-10-28")
    assert cycle.date_of("today", today) == today and cycle.date_of("today", today) == today
    assert cycle.date_of("yesterday", today) == D("2026-10-27")
    assert cycle.date_of("yesterday", today) == D("2026-10-27")
    assert cycle.date_of("24-10", today) == D("2026-10-24")
    assert cycle.date_of("24/10/2026", today) == D("2026-10-24")
    assert cycle.date_of("2026-10-24", today) == D("2026-10-24")
    assert cycle.date_of("Oct 24", today) == D("2026-10-24")
    assert cycle.date_of("24th October", today) == D("2026-10-24")
    assert cycle.date_of("12-12", D("2027-01-05")) == D("2026-12-12")
    assert cycle.date_of("30-10-2026", today) is None  # future
    assert cycle.date_of("31-02", today) is None
    assert cycle.date_of("tomorrow", today) is None


async def test_period_by_telegram_and_its_end(engine):
    today = today_of(engine)
    await command(engine, "period", "today")
    assert "Logged" in last(engine) and NO_NOTICE not in last(engine).lower()
    assert await cycle.days(engine.db) == [today]
    await command(engine, "period", "end")
    assert "lasted 1 day" in last(engine)
    await command(engine, "period", "30-02")
    assert "I don't understand" in last(engine)
    await command(engine, "period")
    assert "next period" in last(engine) and NO_NOTICE not in last(engine).lower()
    # Several days of a period with /period end.
    start = today - timedelta(days=40)
    await cycle.put(engine.db, start, "telegram")
    assert await cycle.end(engine.db, start + timedelta(days=4), "telegram") == 5
    assert len(await cycle.days(engine.db)) == 6
    assert await engine.db.value("SELECT COUNT(*) FROM tasks") == 0


async def test_the_question_about_the_period_is_answered_by_code(engine):
    await text(engine, "When is my next period?")
    assert "period logged" in last(engine) and NO_NOTICE not in last(engine).lower()
    await text(engine, "¿Cuándo me viene la regla?")
    assert "period logged" in last(engine)
    assert not cycle.is_question("research ovulation tests in general")
    assert not cycle.is_question("remove the period from the essay title")


async def test_cycle_notice_two_days_before_and_once(engine):
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(engine.config.time_zone)
    for start in ("2026-08-01", "2026-08-29", "2026-09-26"):
        for k in range(4):
            await cycle.put(engine.db, D(start) + timedelta(days=k), "telegram")
    # Cycles of 28: the next one on 24-10. The notice on 22-10 from 9:00.
    assert not await cycle.review_notice(engine, datetime(2026, 10, 22, 8, 30, tzinfo=zone))
    assert not await cycle.review_notice(engine, datetime(2026, 10, 21, 10, 0, tzinfo=zone))
    moment = datetime(2026, 10, 22, 9, 5, tzinfo=zone)
    assert await cycle.review_notice(engine, moment)
    assert "Oct 24" in last(engine) and NO_NOTICE not in last(engine).lower()
    assert not await cycle.review_notice(engine, moment + timedelta(hours=3))
    # Without a connection it neither goes out nor counts as given.
    await engine.db.kv_delete(cycle.NOTICE_KEY)
    engine.connection.mark_offline()

    async def no_network(self):
        return False

    from aide.core.connection import Connection

    original = Connection._test
    Connection._test = no_network
    try:
        assert not await cycle.review_notice(engine, moment)
        assert await engine.db.kv_read(cycle.NOTICE_KEY) is None
    finally:
        Connection._test = original


# --- gym -----------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "exercise", "series", "reps", "weight"),
    [
        ("squat 4x8 60", "squat", 4, 8, 60.0),
        ("bench press 3x10 40 kg", "bench press", 3, 10, 40.0),
        ("Pull-ups 3x8", "pull-ups", 3, 8, None),
        ("bench press 5 x 5 @ 70", "bench press", 5, 5, 70.0),
        ("row 3 sets of 12 with 30 kg", "barbell row", 3, 12, 30.0),
        ("curl 3x10 22.5", "biceps curl", 3, 10, 22.5),
        ("squat 3x5 135 lb", "squat", 3, 5, 61.2),
        ("squat 60 kg 4x8", "squat", 4, 8, 60.0),
        ("Romanian deadlift 3x8 50", "romanian deadlift", 3, 8, 50.0),
        ("- hip thrust 4x10x80", "hip thrust", 4, 10, 80.0),
        ("dumbbell bench press 3x12 16", "bench press", 3, 12, 16.0),
        # Synonyms in other languages.
        ("sentadilla 4x8 60", "squat", 4, 8, 60.0),
        ("press banca 3x10 40 kg", "bench press", 3, 10, 40.0),
        ("remo 3 series de 12 con 30 kg", "barbell row", 3, 12, 30.0),
        ("Kreuzheben 5x5 80", "deadlift", 5, 5, 80.0),
        ("développé couché 4x6 45", "bench press", 4, 6, 45.0),
        ("dominadas 3x8", "pull-ups", 3, 8, None),
    ],
)
def test_gym_parser(line, exercise, series, reps, weight):
    x = gym.read_line(line)
    assert x is not None
    assert (x.exercise, x.series, x.reps, x.weight) == (exercise, series, reps, weight)


def test_what_is_not_gym_is_not_taken_for_a_session():
    for t in (
        "I did 3x10 squats yesterday",
        "find me a flat in Rivertown",
        "squat 4x8 60\nhow are you?",
        "squat 40x8 60",
        "squat 4x8 900",
        # An extra digit is not 200 repetitions at 5 kg.
        "bench press 3x2005",
    ):
        assert gym.read(t) is None, t
    several = gym.read("squat 4x8 60\nbench press 3x10 40\nxyzzy 3x8")
    assert [x.exercise for x in several] == ["squat", "bench press", None]
    assert "squat" in gym.similar("sqaut")
    assert gym.one_rm(100, 5) == pytest.approx(116.7)
    assert gym.one_rm(100, 1) == 100
    assert gym.one_rm(None, 8) is None
    # Each synonym leads to an exercise in the list, and each exercise has its group.
    assert set(gym.INDEX.values()) == set(gym.EXERCISES)
    assert {g for g, _ in gym.EXERCISES.values()} <= set(gym.GROUPS)


async def test_gym_by_telegram_asks_what_it_does_not_know(engine):
    await text(engine, "squat 4x8 60\nbench press 3x10 40\nbulgrian split sqat 3x10 20")
    texts = engine.channel.texts()
    assert any("Logged: squat 4x8 at 60 kg; bench press 3x10 at 40 kg" in t for t in texts)
    question = engine.channel.last_with_buttons("gym:")
    assert question is not None and "bulgrian split sqat" in question.plain_text
    chosen = next(b for f in question.buttons for b in f if b.text == "bulgarian split squat")
    await button(engine, chosen.datum)
    assert "Logged: bulgarian split squat 3x10 at 20 kg" in last(engine)
    await button(engine, chosen.datum)  # pressing the same button twice does not record twice
    assert await engine.db.value("SELECT COUNT(*) FROM health_gym") == 3
    await text(engine, "zzzqqq 3x8")
    assert "I don't know “zzzqqq”" in last(engine)
    assert await engine.db.value("SELECT COUNT(*) FROM tasks") == 0


async def test_weekly_summary_and_records(engine):
    db = engine.db
    today = today_of(engine)
    before = today - timedelta(days=10)
    await gym.note(db, gym.read("squat 4x8 60\npull-ups 3x6"), before, "x")
    await gym.note(db, gym.read("squat 4x8 65\nbench press 3x10 40"), today, "x")
    await gym.note(db, gym.read("pull-ups 3x8"), today - timedelta(days=1), "x")
    s = await gym.week(db, today)
    assert s["sessions"] == 2 and s["series"] == 4 + 3 + 3
    groups = {g["exercise_group"]: g for g in s["by_group"]}
    assert groups["legs"]["volume"] == 4 * 8 * 65 and groups["back"]["volume"] == 0
    records = {r["exercise"]: r for r in s["records"]}
    assert records["squat"]["value"] == pytest.approx(65 * (1 + 8 / 30), abs=0.1)
    assert not records["squat"]["first"] and records["bench press"]["first"]
    assert records["pull-ups"]["kind"] == "reps" and records["pull-ups"]["reps"] == 8
    text_ = gym.week_text(s)
    assert "2 sessions" in text_ and "New records:" in text_
    evolution = await gym.evolution(db, "squat")
    assert [f["value"] for f in evolution] == [gym.one_rm(60, 8), gym.one_rm(65, 8)]
    await command(engine, "gym")
    assert "2 sessions" in last(engine)


# --- habits and recommendations ------------------------------------------------------------------


def test_what_is_written_about_habits():
    assert habits.read_water("") == 250 and habits.read_water("500") == 500
    assert habits.read_water("1.5 l") == 1500 and habits.read_water("0,5 liters") == 500
    assert habits.read_sleep("7.5") == 7.5 and habits.read_sleep("7h30") == 7.5
    assert habits.read_sleep("23:30-7:00") == 7.5 and habits.read_sleep("1:00-8:15") == 7.25
    assert habits.read_steps("8,000") == 8000 and habits.read_steps("8.000") == 8000
    assert habits.read_weight("61.5") == 61.5 and habits.read_weight("61,5 kg") == 61.5
    for reader, bad in (
        (habits.read_weight, "600"),
        (habits.read_sleep, "25:00-7:00"),
        (habits.read_steps, "lots"),
        (habits.read_water, "²"),
    ):
        with pytest.raises(habits.InvalidValue):
            reader(bad)


async def test_habits_by_telegram_with_buttons(engine):
    await command(engine, "water")
    assert "Today you're at 0.25" in last(engine)
    await button(engine, "health:water:500")
    assert "Today you're at 0.75" in last(engine)
    await command(engine, "sleep")
    assert engine.channel.last_with_buttons("health:sleep:") is not None
    await button(engine, "health:sleep:7.5")
    assert "7 h 30 min" in last(engine)
    await command(engine, "sleep", "23:00-6:30")
    await command(engine, "steps", "8000")
    await command(engine, "weight", "62")
    today = today_of(engine)
    d = await habits.of_day(engine.db, today)
    assert d == {"water": 750, "sleep": 7.5, "steps": 8000, "weight": 62}
    await command(engine, "weight", "1000")
    assert "That doesn't look right" in last(engine)
    await command(engine, "weight")
    assert "Your latest weight, 62.0 kg." in last(engine)
    assert await engine.db.value("SELECT COUNT(*) FROM tasks") == 0


def test_mifflin_and_the_level_from_steps():
    # Woman, 60 kg, 165 cm, 30 years: 10·60 + 6.25·165 − 5·30 − 161 = 1,320.25.
    assert reco.mifflin("female", 60, 165, 30) == pytest.approx(1320.25)
    assert reco.mifflin("male", 80, 180, 40) == pytest.approx(1730)
    assert reco.measured_level(4000, 5, 0) == "sedentary"
    assert reco.measured_level(8000, 5, 0) == "moderate"
    assert reco.measured_level(8000, 5, 3) == "high"
    assert reco.measured_level(12000, 7, 4) == "very_high"
    assert reco.measured_level(12000, 2, 4) is None  # few days with steps


async def test_profile_and_goals(engine):
    db = engine.db
    today = today_of(engine)
    goals, missing = await reco.calculate(db, today)
    assert goals is None and "your weight (with /weight)" in missing
    birth = date(today.year - 30, 1, 1)
    await reco.put(db, "sex", "female", today)
    await reco.put(db, "birth", birth.isoformat(), today)
    await reco.put(db, "height", "1.65", today)
    await reco.put(db, "goal", "lose", today)
    await reco.put(db, "activity", "light", today)
    # Also in Spanish.
    assert reco.validate("sex", "Mujer", today) == "female"
    assert reco.validate("activity", "very high", today) == "very_high"
    with pytest.raises(reco.InvalidProfile):
        await reco.put(db, "height", "3000", today)
    await habits.note(db, "weight", 60, today, "x")
    o, _ = await reco.calculate(db, today)
    assert o is not None and not o.measured and o.level == "light"
    assert o.rest == pytest.approx(1320.25)
    assert o.kcal == round((1320.25 * 1.375 - 400) / 10) * 10
    assert o.protein == 96
    # With 3 or more days of steps, the measured value takes precedence.
    for k in range(4):
        await habits.note(db, "steps", 11000, today - timedelta(days=k), "x")
    o, _ = await reco.calculate(db, today)
    assert o.measured and o.level == "high"
    # Never below the minimum.
    await reco.put(db, "height", "150", today)
    await habits.note(db, "weight", 40, today, "x")
    for k in range(4):
        await habits.note(db, "steps", 1000, today - timedelta(days=k), "x")
    o, _ = await reco.calculate(db, today)
    assert o.kcal == 1200
    await command(engine, "health")
    assert "Not medical advice." in last(engine)
    await command(engine, "health", "profile")
    assert engine.channel.last_with_buttons("health:goal:") is not None
    await button(engine, "health:goal:gain")
    assert (await reco.profile(db))["goal"] == "gain"
    await command(engine, "health", "height 168")
    assert "Saved to your health profile." in last(engine)
    assert (await reco.profile(db))["height"] == "168"
    # The health profile does not go to profile.md, which the agent reads.
    assert "gain" not in engine.config.paths.profile.read_text(encoding="utf-8").lower()


# --- meals ---------------------------------------------------------------------------------------


def test_meal_footer():
    assert meals.caption("dinner") == ("dinner", "")
    assert meals.caption("🍳 Breakfast: toast") == ("breakfast", "toast")
    assert meals.caption("snack") == ("snack", "")
    assert meals.caption("Lunch") == ("lunch", "")
    # Also in Spanish.
    assert meals.caption("cena") == ("dinner", "")
    assert meals.caption("merienda") == ("snack", "")
    assert meals.caption("document insurance") is None
    assert meals.caption("lunchbox ideas") is None


def test_what_the_model_reads_is_checked():
    clean = meals.clean_foods(READ["foods"] + [{"name": ""}, "basura"])
    assert len(clean) == 3
    rice = clean[2]
    assert rice["kcal"] == pytest.approx(4 * 2.7 + 4 * 28 + 9 * 0.3, abs=0.1)
    rare = meals.clean_foods(
        [
            {
                "name": "x",
                "quantity": -3,
                "unit": "",
                "grams": 1e9,
                "kcal": "lots",
                "protein": 1000,
                "carbs": -1,
                "fats": float("nan"),
                "trust": "total",
            }
        ]
    )[0]
    assert rare["protein"] == meals.MAX_MACRO and rare["carbs"] == 0
    assert rare["grams"] == meals.MAX_GRAMS and rare["trust"] == "medium"
    assert rare["kcal"] == 1200  # without valid kcal the macros take precedence: 4 · 300


def test_corrections_by_code():
    items = meals.clean_foods(READ["foods"])
    two = meals.correct_by_code(items, "it was 2 eggs")
    assert two[0]["quantity"] == 2 and two[0]["kcal"] == pytest.approx(180)
    no_bread = meals.correct_by_code(items, "remove the bread")
    assert [a["name"] for a in no_bread] == ["Fried egg", "White rice"]
    assert meals.correct_by_code(items, "no bread") == no_bread
    rice = meals.correct_by_code(items, "150 g of rice")
    assert rice[2]["grams"] == 150 and rice[2]["quantity"] == 150
    assert rice[2]["kcal"] == pytest.approx(items[2]["kcal"] * 1.5, abs=0.2)
    # “150 g” without a name is only valid with a single food.
    assert meals.correct_by_code(items, "150 g") is None
    assert meals.correct_by_code(items[:1], "100 g")[0]["grams"] == 100
    assert meals.correct_by_code(items, "add a yogurt") is None
    assert meals.correct_by_code(items, "remove the cake") is None
    # In Spanish, as in the footer.
    assert meals.correct_by_code(items, "quita el bread") == no_bread
    assert meals.parts_of("it was 2 eggs and remove the bread, 150 g of rice") == [
        "it was 2 eggs",
        "remove the bread",
        "150 g of rice",
    ]


async def test_a_food_photo_goes_to_the_model_alone(engine, monkeypatch):
    registry = photos_model(monkeypatch)
    # Health data the model must never see.
    today = today_of(engine)
    await reco.put(engine.db, "sex", "female", today)
    await habits.note(engine.db, "weight", 61.5, today, "x")
    await cycle.put(engine.db, today, "x")
    photo = telegram_photo(engine)
    await engine.receive(Entry("image", SIMULATED_USER, text="dinner", files=[photo], message_id=5))
    # The photo is no longer in the Telegram cache (which the agent can read), but in
    # data/health.
    assert not photo.exists()
    photos = list(engine.config.paths.photos_health.iterdir())
    assert len(photos) == 1 and "dinner" not in photos[0].name
    assert photos[0].stat().st_mode & 0o777 == 0o600
    assert engine.config.paths.photos_health.stat().st_mode & 0o777 == 0o700
    # What the model saw: the image (at most 1,024 pixels) and a minimal prompt.
    [blocks] = registry.blocks
    images = [b for b in blocks if b["type"] == "image"]
    texts = [b["text"] for b in blocks if b["type"] == "text"]
    assert len(images) == 1 and texts == ["What is in this meal?"]
    import base64
    import io

    from PIL import Image

    view = Image.open(io.BytesIO(base64.b64decode(images[0]["source"]["data"])))
    assert max(view.size) <= meals.MODEL_SIDE
    [options] = registry.options
    assert options.model == "claude-sonnet-5-5" and options.tools == []
    assert options.env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(meals.MAX_OUTPUT)
    assert options.max_budget_usd == meals.MAX_USD
    assert "no-session-persistence" in options.extra_args
    for datum in ("61", "female", "period", "weight", "cycle"):
        assert datum not in options.system_prompt and all(datum not in t for t in texts)
    # What comes back: checked and summed, with the usage in “usage”.
    c = (await meals.of_day(engine.db, today))[0]
    assert c["state"] == "items" and c["kind"] == "dinner"
    assert c["kcal"] == pytest.approx(90 + 150 + 4 * 2.7 + 4 * 28 + 9 * 0.3, abs=0.2)
    message = engine.channel.messages[-1]
    assert "Dinner, about" in message.plain_text and "Not medical advice." in message.plain_text
    assert NO_NOTICE not in message.plain_text.lower()
    assert await engine.db.value("SELECT COUNT(*) FROM usage WHERE origin='meals'") == 1
    assert await engine.db.value("SELECT COUNT(*) FROM tasks") == 0
    assert await engine.db.value("SELECT COUNT(*) FROM memory_proposals") == 0
    # Correction by replying to the message, by code: no other model call.
    await text(engine, "it was 2 eggs and remove the bread", replies_to=c["message_id"])
    c = (await meals.of_day(engine.db, today))[0]
    assert [a["name"] for a in c["foods"]] == ["Fried egg", "White rice"]
    assert c["foods"][0]["quantity"] == 2
    assert len(registry.blocks) == 1 and len(registry.options) == 1
    assert engine.channel.messages[-1].kind == "edit"


async def test_the_correction_the_model_needs_carries_only_the_list(engine, monkeypatch):
    registry = photos_model(monkeypatch)
    await engine.receive(
        Entry("image", SIMULATED_USER, text="lunch", files=[telegram_photo(engine)])
    )
    c = (await meals.of_day(engine.db, today_of(engine)))[0]
    with_yogurt = {
        "is_meal": True,
        "foods": [*READ["foods"], {**READ["foods"][0], "name": "Yogurt"}],
    }
    photos_model(monkeypatch, with_yogurt)
    await text(engine, "add a yogurt", replies_to=c["message_id"])
    query = registry.queries[-1]
    assert query.startswith("Foods: [") and "Correction: add a yogurt" in query
    assert len(registry.blocks) == 1  # the correction carries no image
    c = (await meals.of_day(engine.db, today_of(engine)))[0]
    assert c["foods"][-1]["name"] == "Yogurt"


async def test_a_photo_without_footer_asks_if_it_is_food(engine, monkeypatch):
    photos_model(monkeypatch)
    await engine.receive(Entry("image", SIMULATED_USER, files=[telegram_photo(engine)]))
    question = engine.channel.last_with_buttons("pending:")
    assert "It's food" in [b.text for f in question.buttons for b in f]
    datum = next(b.datum for f in question.buttons for b in f if b.text == "It's food")
    await button(engine, datum)
    kinds = engine.channel.last_with_buttons("mealtype:")
    assert [b.text for f in kinds.buttons for b in f] == ["Breakfast", "Lunch", "Dinner", "Snack"]
    await button(engine, next(b.datum for f in kinds.buttons for b in f if b.text == "Snack"))
    c = (await meals.of_day(engine.db, today_of(engine)))[0]
    assert c["kind"] == "snack" and c["state"] == "items"
    # The same button again does nothing.
    await button(engine, next(b.datum for f in kinds.buttons for b in f if b.text == "Snack"))
    assert "no longer waiting" in last(engine)
    # A PDF without a footer does not offer “It's food”.
    pdf = engine.config.paths.agent_cache / "telegram" / "9" / "a.pdf"
    pdf.parent.mkdir(parents=True)
    pdf.write_bytes(b"%PDF-1.4")
    await engine.receive(Entry("document", SIMULATED_USER, files=[pdf]))
    question = engine.channel.last_with_buttons("pending:")
    assert "It's food" not in [b.text for f in question.buttons for b in f]


async def _nothing():
    return None


async def test_at_the_plan_limit_no_call_is_made_and_it_stays_pending(engine, monkeypatch):
    registry = photos_model(monkeypatch)

    async def limit(moment=None):
        return "seven_day", datetime.now(UTC) + timedelta(hours=3)

    monkeypatch.setattr(engine.usage, "reached_limit", limit)
    await engine.receive(
        Entry("image", SIMULATED_USER, text="breakfast", files=[telegram_photo(engine)])
    )
    assert registry.options == []
    assert "weekly limit" in last(engine) and meals.BUTTON_LOOK in last(engine)
    c = (await meals.of_day(engine.db, today_of(engine)))[0]
    assert c["state"] == "pending"
    monkeypatch.setattr(engine.usage, "reached_limit", lambda moment=None: _nothing())
    await button(engine, f"meal:mir:{c['id']}")
    assert (await meals.get(engine.db, c["id"]))["state"] == "items"
    await button(engine, f"meal:of:{c['id']}")
    assert await meals.get(engine.db, c["id"]) is None
    assert list(engine.config.paths.photos_health.iterdir()) == []


async def test_a_failing_model_leaves_the_meal_pending(engine, monkeypatch):
    def script(prompt, options, registry):
        return [End(subtype="error_during_execution", is_error=True)]

    monkeypatch.setattr(FakeClient, "script", staticmethod(script))
    await engine.receive(
        Entry("image", SIMULATED_USER, text="dinner", files=[telegram_photo(engine)])
    )
    assert "the model didn't answer" in last(engine)
    assert (await meals.of_day(engine.db, today_of(engine)))[0]["state"] == "pending"


def test_photo_route_only_gives_files_from_its_folder(tmp_path):
    folder = tmp_path / "data" / "health" / "photos"
    folder.mkdir(parents=True)
    (folder / "buena.jpg").write_bytes(b"x")
    (folder / "sub").mkdir()
    (folder / "sub" / "otra.jpg").write_bytes(b"x")
    secret = tmp_path / "data" / "agent.db"
    secret.write_bytes(b"x")
    os.symlink(secret, folder / "enlace.jpg")
    assert meals.photo_path(folder, "buena.jpg") == (folder / "buena.jpg").resolve()
    for bad in ("../../agent.db", "sub/otra.jpg", "sub", "enlace.jpg", "", None, "no.jpg", ".."):
        assert meals.photo_path(folder, bad) is None, bad


async def test_cleanup_deletes_old_photos_and_the_backup_carries_them(engine, monkeypatch):
    from aide.maintenance import backups, cleanup

    photos_model(monkeypatch)
    await engine.receive(
        Entry("image", SIMULATED_USER, text="dinner", files=[telegram_photo(engine)])
    )
    await engine.receive(
        Entry("image", SIMULATED_USER, text="lunch", files=[telegram_photo(engine)])
    )
    old, new = sorted(engine.config.paths.photos_health.iterdir())
    ago = time.time() - 31 * 86400
    os.utime(old, (ago, ago))
    monkeypatch.setattr(cleanup, "CLAUDE_PROJECTS", engine.config.paths.root / "no-existe")
    s = await cleanup.clean(engine, test=True)
    assert s.data["health_photos"] == 1 and old.exists()
    file, _, _ = backups.create_backup(engine.config.paths, datetime(2026, 10, 25, 19, 0), 4)
    with zipfile.ZipFile(file) as z:
        names = z.namelist()
    assert f"data/health/photos/{old.name}" in names and "data/agent.db" in names
    assert file.stat().st_mode & 0o777 == 0o600
    s = await cleanup.clean(engine)
    assert s.data["health_photos"] == 1 and not old.exists() and new.exists()
    # Neither the photo name nor the meals appear in what the model reads for the report.
    assert all("meal" not in line and old.name not in line for line in s.lines)
    photos = [c["photos"] for c in await meals.of_day(engine.db, today_of(engine))]
    assert sorted(len(f) for f in photos) == [0, 1]


# --- report, “Today” and what the agent does not see -----------------------------------------------


async def test_health_part_of_the_report(engine):
    from aide.health import summary
    from aide.maintenance import report

    today = today_of(engine)
    for start in (today - timedelta(days=50), today - timedelta(days=22)):
        await cycle.put(engine.db, start, "x")
    await gym.note(engine.db, gym.read("squat 4x8 60"), today, "x")
    await habits.note(engine.db, "water", 1500, today, "x")
    text_ = await summary.report_text(engine, datetime.now(UTC), 2)
    assert "At the gym, 1 session" in text_ and "Next period estimated around" in text_
    assert "I deleted 2 photos" in text_ and text_.endswith("Not medical advice.")
    # Of the cycle, only the estimated date.
    assert "ovulation" not in text_.lower() and "fertile" not in text_.lower()
    assert len(text_) <= summary.MAX_REPORT
    message, _ = report.compose(
        [], "text from the model", [], datetime.now(UTC), False, None, "", text_
    )
    assert "Your health" in message and "Next period" in message


async def test_report_model_does_not_see_health(engine, monkeypatch, tmp_path):
    from aide.maintenance import cleanup, report, security, updates
    from aide.maintenance.section import Section

    monkeypatch.setattr(security, "PLIST", tmp_path / "no.plist")
    monkeypatch.setattr(cleanup, "CLAUDE_PROJECTS", tmp_path / "pc")
    monkeypatch.setattr(security, "audit_dependencies", lambda root: ([], None))
    monkeypatch.setattr(security, "process_listeners", lambda port, pid=None: ["127.0.0.1"])

    async def no_updates(m):
        return Section("updates", "Updates")

    monkeypatch.setattr(updates, "review", no_updates)
    today = today_of(engine)
    await cycle.put(engine.db, today - timedelta(days=20), "x")
    await gym.note(engine.db, gym.read("squat 4x8 60"), today, "x")
    seen = []

    async def draft(m, data):
        seen.append(data)
        return {"report": "All good.", "improvements": []}

    monkeypatch.setattr(report, "draft", draft)
    message = await report.run(engine, test=True)
    assert "Your health" in message and "squat" not in str(seen[0])
    assert "period" not in str(seen[0]).lower() and "health" not in str(seen[0]).lower()


async def test_nothing_about_health_in_agent_tools_or_prompts():
    """What the agent or the quick answer can query does not touch the health tables or folder:
    not the MCP tools, nor memory, triage, prompts or report signals."""
    root = Path(__file__).resolve().parents[1] / "src" / "aide"
    watched = [
        *sorted((root / "tools").glob("*.py")),
        *sorted((root / "memory").glob("*.py")),
        root / "core" / "quick.py",
        root / "core" / "triage.py",
        root / "core" / "prompts.py",
        root / "core" / "orchestrator.py",
        root / "core" / "session.py",
        root / "maintenance" / "improvements.py",
        root / "maintenance" / "consumption.py",
    ]
    for path in watched:
        content = path.read_text(encoding="utf-8")
        assert not re.search(
            r"salud_(regla|gimnasio|comidas|habitos|perfil)|photos_health", content
        ), path
        assert "aide.health" not in content, path


async def test_today_card(engine):
    from aide.health import summary

    today = today_of(engine)
    empty = await summary.card_today(engine.db, today)
    assert not empty["something"] and empty["days_period"] is None
    await cycle.put(engine.db, today - timedelta(days=20), "x")
    await habits.note(engine.db, "water", 750, today, "x")
    await habits.note(engine.db, "steps", 5000, today, "x")
    t = await summary.card_today(engine.db, today)
    assert t["something"] and t["days_period"] == 8 and t["water"] == 750 and t["steps"] == 5000


# --- panel ---------------------------------------------------------------------------------------


async def test_health_section_of_the_panel(panel, monkeypatch):
    engine, p, client = panel
    today = today_of(engine)
    for view in ("cycle", "gym", "meals", "habits", "other"):
        r = await client.get("/health", params={"view": view})
        assert r.status_code == 200, view
        html = r.text
        assert " style=" not in html and "<style" not in html, view
        assert re.findall(r"<script(?![^>]*\bsrc=)", html) == [], view
        assert 'class="section-tabs pink"' in html
        assert 'class="section pink active" aria-current="page"' in html
        assert NO_NOTICE not in html.lower(), view
    # The calendar: tapping a day records it and tapping it again removes it, with CSRF.
    day = (today - timedelta(days=2)).isoformat()
    assert (await client.post("/health/cycle/day", data={"day": day})).status_code == 403
    r = await client.post("/health/cycle/day", data={"day": day, "csrf": p.csrf})
    assert r.status_code == 303 and await cycle.days(engine.db) == [D(day)]
    await client.post("/health/cycle/day", data={"day": day, "csrf": p.csrf})
    assert await cycle.days(engine.db) == []
    future = (today + timedelta(days=3)).isoformat()
    r = await client.post("/health/cycle/day", data={"day": future, "csrf": p.csrf})
    assert "future" in r.headers["location"] and await cycle.days(engine.db) == []
    r = await client.post(
        "/health/cycle/day",
        data={"day": day, "csrf": p.csrf},
        headers={"Origin": "http://evil.example"},
    )
    assert r.status_code == 403
    # The gym: what it does not understand is not recorded.
    r = await client.post("/health/gym", data={"text": "squat 4x8 60\nxyzzy 3x8", "csrf": p.csrf})
    assert "understand" in r.headers["location"] and "kind=error" in r.headers["location"]
    assert await engine.db.value("SELECT COUNT(*) FROM health_gym") == 0
    await client.post(
        "/health/gym",
        data={
            "text": "squat 4x8 60",
            "date": (today - timedelta(days=3)).isoformat(),
            "csrf": p.csrf,
        },
    )
    await client.post("/health/gym", data={"text": "squat 4x8 65", "csrf": p.csrf})
    html = (await client.get("/health?view=gym&exercise=squat")).text
    assert "<polyline" in html and "Progress" in html
    # Habits and profile.
    await client.post("/health/habits", data={"kind": "water", "value": "500", "csrf": p.csrf})
    await client.post("/health/habits", data={"kind": "weight", "value": "62", "csrf": p.csrf})
    r = await client.post("/health/habits", data={"kind": "weight", "value": "900", "csrf": p.csrf})
    assert "kind=error" in r.headers["location"]
    r = await client.post("/health/habits", data={"kind": "other", "value": "1", "csrf": p.csrf})
    assert r.status_code == 400
    await client.post(
        "/health/profile",
        data={
            "sex": "female",
            "height": "165",
            "birth": "1995-03-14",
            "goal": "maintain",
            "csrf": p.csrf,
        },
    )
    assert (await reco.profile(engine.db))["height"] == "165"
    assert (await habits.of_day(engine.db, today))["water"] == 500
    row = await engine.db.one("SELECT id FROM health_habits WHERE kind='water'")
    await client.post(f"/health/habits/{row['id']}/remove", data={"csrf": p.csrf})
    assert (await habits.of_day(engine.db, today))["water"] is None
    assert (await client.post("/health/other/1/remove", data={"csrf": p.csrf})).status_code == 404
    # Meals with their photo, which is only served from data/health/photos.
    photos_model(monkeypatch)
    await engine.receive(
        Entry("image", SIMULATED_USER, text="dinner", files=[telegram_photo(engine)])
    )
    c = (await meals.of_day(engine.db, today))[0]
    html = (await client.get("/health?view=meals")).text
    assert f'src="/health/photos/{c["id"]}/0"' in html and "Not medical advice." in html
    r = await client.get(f"/health/photos/{c['id']}/0")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert (await client.get(f"/health/photos/{c['id']}/1")).status_code == 404
    await engine.db.run(
        "UPDATE health_meals SET photos=? WHERE id=?", ('["../../agent.db", "../health"]', c["id"])
    )
    for n in (0, 1):
        assert (await client.get(f"/health/photos/{c['id']}/{n}")).status_code == 404
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=p.app), base_url=f"http://127.0.0.1:{p.port}"
    ) as anonymous:
        assert (await anonymous.get("/health")).status_code == 401
        assert (await anonymous.get(f"/health/photos/{c['id']}/0")).status_code == 401
    # The “Today” card.
    start = (await client.get("/")).text
    assert 'href="/health"' in start and "steps" in start


def test_calendar_paints_neighbor_month_days_and_the_cycle_line():
    """The days of the neighboring month that fall in the grid carry their phase (softer), and
    the cycle line goes from the first day of the last period to the next."""
    from aide.panel.health import calendar_month, cycle_line

    days = _periods(["2026-08-01", "2026-08-29", "2026-09-26"], 4)
    e = cycle.estimate(days)
    assert e.next_run == D("2026-10-24")
    today = D("2026-10-28")
    october = {d["day"]: d for s in calendar_month(2026, 10, e, set(days), today) for d in s}
    # October starts on a Thursday: Monday 28-9 and Tuesday 29-9 belong to September's period.
    assert (
        "period" in october["2026-09-28"]["kinds"] and "outside" in october["2026-09-28"]["kinds"]
    )
    assert not october["2026-09-28"]["tappable"] and october["2026-09-28"]["phase"] == "Period"
    november = {d["day"]: d for s in calendar_month(2026, 11, e, set(days), today) for d in s}
    # November starts on a Sunday: its grid shows 26 to 31-10, with the predicted one.
    assert "expected" in november["2026-10-26"]["kinds"]
    assert "outside" in november["2026-10-26"]["kinds"]
    assert october["2026-10-10"]["phase"] == "Ovulation"
    assert october["2026-10-08"]["phase"] == "Fertile"
    assert october["2026-10-28"]["date"] == "Today, Wednesday, October 28"
    assert october["2026-10-20"]["action"] == "Log period"
    assert october["2026-09-28"]["action"] == ""
    lc = cycle_line(e, set(days), today)
    assert lc["day"] == 33 and lc["long"] == 28 and lc["delay"] == 5 and lc["period"] == 4
    assert len(lc["cells"]) == 33 and "today" in lc["cells"][-1]["kind"]
    assert lc["cells"][0]["kind"] == "period" and lc["cells"][30]["kind"] == "delay"
    assert lc["cells"][30]["tooltip"].endswith("· late")
    assert cycle_line(cycle.estimate([]), set(), today) is None


async def test_cycle_shows_this_month_and_the_next(panel):
    """The predicted period is visible without changing month: the calendar shows this month
    and the next, and the arrows move them. The gym has its own icon, the dumbbell."""
    engine, p, client = panel
    from aide.panel.health import MONTHS

    today = today_of(engine)
    for back in (76, 48, 20):
        for k in range(4):
            await cycle.put(engine.db, today - timedelta(days=back - k), "x")
    next_run = today + timedelta(days=8)
    html = (await client.get("/health?view=cycle")).text
    names = re.findall(r'<h3 class="month-name">([^<]+)</h3>', html)
    next_step = date(today.year + today.month // 12, today.month % 12 + 1, 1)
    assert names == [
        f"{MONTHS[today.month - 1]} {today.year}",
        f"{MONTHS[next_step.month - 1]} {next_step.year}",
    ]
    assert f'data-tooltip="{month_day(next_run)} · expected"' in html
    assert 'class="cycle-line"' in html and "Day <b>21</b> of 28" in html
    assert 'data-action="Log period"' in html and "data-tab" in html
    # With the arrow, the next month becomes the first, and the “Today” button appears.
    other = (await client.get(f"/health?view=cycle&month={next_step:%Y-%m}")).text
    assert re.findall(r'<h3 class="month-name">([^<]+)</h3>', other)[0] == names[1]
    assert '<a class="button secondary" href="/health?view=cycle" data-tab>Today</a>' in other
    assert ">Today</a>" not in html.split('class="months"')[0].split('class="nav-month"')[1]
    # The gym icon is the dumbbell (only in the content).
    weighs = '<rect x="5" y="6.5" width="3" height="11" rx="1.2"/>'
    for view in ("cycle", "gym"):
        partial = (await client.get(f"/health?view={view}", headers={"X-Partial": "1"})).text
        assert weighs in partial and "<html" not in partial, view
    await gym.note(engine.db, [gym.read_line("squat 4x8 60")], today, "x")
    start = (await client.get("/", headers={"X-Partial": "1"})).text
    assert weighs in start and "gym session" in start


def test_health_commands_are_in_help_and_in_telegram():
    from aide.bot.commands import HANDLERS, HELP
    from aide.bot.telegram import COMMANDS

    names = {n for n, _ in COMMANDS}
    for c in ("period", "gym", "meals", "water", "sleep", "steps", "weight", "health"):
        assert c in HANDLERS and c in names and f"/{c}" in HELP, c
    assert NO_NOTICE not in HELP.lower()
