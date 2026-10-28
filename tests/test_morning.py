"""The morning summary: by code, instead of the rain notice and only on days with
something. No network or model: the weather and Google are fake."""

from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from aide import life, morning, weather
from aide.bot.channel import FakeChannel
from aide.core.engine import Engine
from aide.paperwork import registry, reminders

ZONE = ZoneInfo("Europe/Berlin")
RAIN = weather.rain
TODAY = date(2026, 10, 26)  # Monday
WEATHER_CONFIG = {
    "locations": [
        {"name": "Town A", "latitude": 46.0, "longitude": 7.0, "since": "2000-01-01"},
        {"name": "Town B", "latitude": 46.5, "longitude": 7.5, "since": "2027-03-01"},
    ],
    "time_zone": "Europe/Berlin",
    "start_hour": 7,
    "end_hour": 22,
    "probability_threshold": 50,
    "mm_threshold": 0.3,
}


def at_time(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime.combine(day, time(hour, minute), ZONE)


def weather_reply(day: date, rain: dict[int, tuple[float, float]]) -> dict[str, Any]:
    hours = [f"{day.isoformat()}T{h:02d}:00" for h in range(24)]
    return {
        "hourly": {
            "time": hours,
            "precipitation_probability": [rain.get(h, (10, 0))[0] for h in range(24)],
            "precipitation": [rain.get(h, (10, 0))[1] for h in range(24)],
        }
    }


@pytest.fixture
async def engine(config_tmp):
    m = Engine(config_tmp, channel=FakeChannel(print_out=False))
    await m.start(workers=False)
    m.config.settings.morning_summary.active = True
    cfg = m.config.paths.agent / m.config.settings.morning_summary.weather
    cfg.parent.mkdir(parents=True, exist_ok=True)
    cfg.write_text(json.dumps(WEATHER_CONFIG), encoding="utf-8")
    yield m
    await m.stop()


async def todays_weather(m: Engine) -> weather.Weather:
    return await weather.rain(
        m.config.paths.agent / m.config.settings.morning_summary.weather, TODAY
    )


def fake_time(monkeypatch, rain: dict[int, tuple[float, float]] | None = None, fails=False):
    requested: list[str] = []

    def request(url: str) -> dict[str, Any]:
        requested.append(url)
        if fails:
            raise OSError("no network")
        day = date.fromisoformat(url.split("start_date=")[1][:10])
        return weather_reply(day, rain or {})

    async def no_wait(_s: float) -> None:
        return None

    monkeypatch.setattr(weather, "_request", request)
    monkeypatch.setattr(weather.asyncio, "sleep", no_wait)
    return requested


# --- the weather ----------------------------------------------------------------------------


def test_rain_like_the_usual_notice():
    r = weather.analyze(weather_reply(TODAY, {14: (80, 1.2), 16: (60, 0.5)}), TODAY, WEATHER_CONFIG)
    assert r == {"since": 14, "until": 16, "prob_max": 80, "mm_total": 1.7}
    assert weather.phrase("Town A", r) == (
        "☔ It's going to rain in Town A today between 14:00 and 16:00, with a chance of up to "
        "80% and about 1.7 mm in total. Take an umbrella."
    )
    assert weather.analyze(weather_reply(TODAY, {3: (90, 5)}), TODAY, WEATHER_CONFIG) is None
    # Odd responses: missing data counts as 0; lists of different length or without the day,
    # failure.
    with_nulls = weather_reply(TODAY, {})
    with_nulls["hourly"]["precipitation"][14] = None
    with_nulls["hourly"]["precipitation_probability"][14] = None
    assert weather.analyze(with_nulls, TODAY, WEATHER_CONFIG) is None
    short = weather_reply(TODAY, {})
    short["hourly"]["precipitation"] = short["hourly"]["precipitation"][:5]
    with pytest.raises(ValueError):
        weather.analyze(short, TODAY, WEATHER_CONFIG)
    with pytest.raises(ValueError):
        weather.analyze(weather_reply(TODAY - timedelta(days=1), {}), TODAY, WEATHER_CONFIG)
    # Like the usual notice: the probability or the amount is enough.
    only_probability = weather.analyze(weather_reply(TODAY, {11: (60, 0)}), TODAY, WEATHER_CONFIG)
    only_quantity = weather.analyze(weather_reply(TODAY, {12: (20, 0.5)}), TODAY, WEATHER_CONFIG)
    assert only_probability["since"] == 11 and only_quantity["since"] == 12
    with pytest.raises(ValueError):
        weather.analyze({"hourly": {}}, TODAY, WEATHER_CONFIG)


def test_the_place_changes_with_the_move():
    assert weather.site(WEATHER_CONFIG, date(2027, 2, 28))["name"] == "Town A"
    assert weather.site(WEATHER_CONFIG, date(2027, 3, 1))["name"] == "Town B"


async def test_without_its_configuration_or_with_a_failure_it_says_so(tmp_path, monkeypatch):
    assert await weather.rain(tmp_path / "no-esta.json", TODAY) == weather.Weather(failure=True)
    broken = tmp_path / "rota.json"
    no_latitude = {
        **WEATHER_CONFIG,
        "locations": [{"name": "X", "longitude": 7, "since": "2000-01-01"}],
    }
    for content in ("not json", json.dumps({"locations": []}), json.dumps(no_latitude)):
        broken.write_text(content, encoding="utf-8")
        assert await weather.rain(broken, TODAY) == weather.Weather(failure=True), content
    future = tmp_path / "futura.json"
    future_only = {**WEATHER_CONFIG, "locations": [WEATHER_CONFIG["locations"][1]]}
    future.write_text(json.dumps(future_only), encoding="utf-8")
    assert await weather.rain(future, TODAY) == weather.Weather(failure=True)  # no place for today
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps(WEATHER_CONFIG), encoding="utf-8")
    requested = fake_time(monkeypatch, fails=True)
    assert await weather.rain(cfg, TODAY) == weather.Weather(failure=True)
    assert len(requested) == 2  # one retry
    fake_time(monkeypatch, {9: (70, 0.4)})
    t = await weather.rain(cfg, TODAY)
    assert t.phrase.startswith("☔ It's going to rain in Town A today at 09:00") and not t.failure


# --- the summary ----------------------------------------------------------------------------


async def document(m: Engine, days: list[tuple[int, str]]) -> int:
    document_id = await registry.create(
        m.db,
        {"title": "Home insurance", "kind": "insurance", "file": "2026/x.pdf"},
        [
            {
                "kind": "expiry",
                "date": (TODAY + timedelta(days=n)).isoformat(),
                "description": d,
            }
            for n, d in days
        ],
    )
    await registry.save(m.db, document_id)
    return document_id


async def calendar(m: Engine, events: list[dict[str, Any]], moment: datetime) -> None:
    await m.db.kv_put(
        life.CALENDAR_KEY,
        {"updated": moment.astimezone(UTC).isoformat(timespec="seconds"), "events": events},
    )


async def test_a_day_with_everything(engine, monkeypatch):
    fake_time(monkeypatch, {15: (70, 0.8)})
    moment = at_time(TODAY, 7, 45)
    await calendar(
        engine,
        [
            {
                "title": "Dentist",
                "place": "Downtown",
                "start": "2026-10-26T10:00:00+01:00",
                "end": "2026-10-26T11:00:00+01:00",
                "all_day": False,
            }
        ],
        moment,
    )
    await document(engine, [(3, "the insurance expires"), (30, "the permit must be renewed")])
    text, marks = await morning.compose(engine, moment, await todays_weather(engine))
    assert text.startswith("Good morning. Today is Monday, October 26.\n- ☔ It's going to rain")
    assert "- At 10:00, Dentist (Downtown)." in text
    assert "- On Oct 29, 2026 (in 3 days) the insurance expires (Home insurance)." in text
    assert "(in 30 days) the permit must be renewed" in text
    assert len(marks) == 2  # the insurance 7-day notice and the permit 30-day one
    assert "—" not in text


async def test_only_days_with_something(engine, monkeypatch):
    fake_time(monkeypatch)  # no llueve
    moment = at_time(TODAY, 7, 40)
    await calendar(engine, [], moment)
    assert await morning.review(engine, moment) is False
    assert engine.channel.texts() == []
    assert await engine.db.kv_read(morning.DAY_KEY) == TODAY.isoformat()  # today is already checked
    # What stops no task (a memory proposal) is not enough to send it...
    await engine.db.run(
        "INSERT INTO memory_proposals(done, task_id, state, created) "
        "VALUES ('something', NULL, 'pending', '2026-10-25T10:00:00+00:00')"
    )
    text, _ = await morning.compose(engine, moment, await todays_weather(engine))
    assert text is None
    # ...but it does go in if there is something else.
    fake_time(monkeypatch, {12: (90, 2.0)})
    text, _ = await morning.compose(engine, moment, await todays_weather(engine))
    assert "- Waiting for you on Telegram: 1 memory suggestion." in text


async def test_the_hour_the_limit_and_once_a_day(engine, monkeypatch):
    fake_time(monkeypatch, {12: (90, 2.0)})
    await calendar(engine, [], at_time(TODAY, 7))
    assert await morning.review(engine, at_time(TODAY, 7, 29)) is False
    assert await morning.review(engine, at_time(TODAY, 11, 0)) is False
    assert engine.channel.texts() == []
    assert await morning.review(engine, at_time(TODAY, 7, 30)) is True
    assert "It's going to rain" in engine.channel.texts()[-1]
    assert await morning.review(engine, at_time(TODAY, 8)) is False  # once a day
    assert len(engine.channel.texts()) == 1
    assert await morning.review(engine, at_time(TODAY + timedelta(days=1), 7, 35)) is True


async def test_without_connection_it_is_tried_later(engine, monkeypatch):
    fake_time(monkeypatch, {12: (90, 2.0)})
    await calendar(engine, [], at_time(TODAY, 7))
    original = engine.notify

    async def no_network(*a: Any, **k: Any) -> None:
        return None

    monkeypatch.setattr(engine, "notify", no_network)
    assert await morning.review(engine, at_time(TODAY, 7, 30)) is False
    assert await engine.db.kv_read(morning.DAY_KEY) is None
    monkeypatch.setattr(engine, "notify", original)
    assert await morning.review(engine, at_time(TODAY, 9)) is True


async def test_document_notices_go_in_the_summary_and_are_not_repeated(engine, monkeypatch):
    fake_time(monkeypatch)
    await calendar(engine, [], at_time(TODAY, 7))
    await document(engine, [(7, "the insurance expires")])
    assert await morning.review(engine, at_time(TODAY, 7, 30)) is True
    assert "(in 7 days) the insurance expires" in engine.channel.texts()[-1]
    # At 9:00 the documents notice has nothing left to send.
    assert await reminders.review(engine, at_time(TODAY, 9)) == 0
    assert len(engine.channel.texts()) == 1


async def test_an_old_calendar_is_read_earlier(engine, monkeypatch):
    fake_time(monkeypatch)
    read: list[datetime] = []

    async def refresh(core, moment=None):
        read.append(moment)
        await calendar(
            engine,
            [
                {
                    "title": "Sam's birthday",
                    "place": "",
                    "start": "2026-10-26",
                    "end": "2026-10-27",
                    "all_day": True,
                }
            ],
            moment,
        )
        return 1

    monkeypatch.setattr(life, "refresh_calendar", refresh)
    await calendar(engine, [], at_time(TODAY - timedelta(days=1), 22))
    assert await morning.review(engine, at_time(TODAY, 7, 30)) is True
    assert len(read) == 1
    assert "- All day, Sam's birthday." in engine.channel.texts()[-1]


async def test_what_awaits_your_answer(engine):
    assert await morning.waiting(engine.db) is None
    task = await engine.tasks.create("Write to the town hall")
    await engine.db.run(
        "INSERT INTO approvals(task_id, kind, tool, entry, detail, level, state, "
        "created) VALUES (?, 'email', 'mcp__aide__gmail_enviar', '{}', 'Send', 2, 'pending', "
        "'2026-10-26T06:00:00+00:00')",
        (task.id,),
    )
    assert await morning.waiting(engine.db) == "Waiting for you on Telegram: 1 approval."
    # Alone, it does not send the summary: approvals already have their reminders.
    await calendar(engine, [], at_time(TODAY, 8))
    text, _ = await morning.compose(engine, at_time(TODAY, 8), weather.Weather())
    assert text is None


async def test_off_does_nothing(engine, monkeypatch):
    fake_time(monkeypatch, {12: (90, 2.0)})
    engine.config.settings.morning_summary.active = False
    assert await morning.review(engine, at_time(TODAY, 8)) is False
    assert engine.channel.texts() == []


def test_configuration_checks_the_hours():
    from pydantic import ValidationError

    from aide.common.config import MorningSummary

    with pytest.raises(ValidationError):
        MorningSummary(hour="11:00", until="07:30")
    with pytest.raises(ValidationError):
        MorningSummary(hour="7:30")
    assert MorningSummary(hour="07:00", until="10:00").until == "10:00"


def test_the_event_text():
    assert morning.event_line({"hour": "ongoing", "title": "Trip", "until": "until tomorrow"}) == (
        "Ongoing: Trip, until tomorrow."
    )


def test_the_weather_route_is_the_rain_notice_one(config_tmp):
    path = config_tmp.paths.agent / config_tmp.settings.morning_summary.weather
    assert path == config_tmp.paths.agent / "projects" / "rain-alert" / "config.json"
    assert isinstance(path, Path)


async def test_engine_launches_it_in_the_background_and_never_two_at_once(engine, monkeypatch):
    import asyncio

    calls = []
    drop = asyncio.Event()

    async def review(m, moment=None):
        calls.append(m)
        await drop.wait()
        return False

    monkeypatch.setattr(morning, "review", review)
    await engine.check_all()
    await asyncio.sleep(0)
    await engine.check_all()  # the first is still running: another is not launched
    await asyncio.sleep(0)
    assert calls == [engine] and engine._summary_in_running
    drop.set()
    await asyncio.gather(*list(engine._background), return_exceptions=True)
    assert not engine._summary_in_running
    # Off in settings, it is not even launched.
    engine.config.settings.morning_summary.active = False
    await engine.check_all()
    await asyncio.sleep(0)
    assert len(calls) == 1


# --- review of the morning summary -----------------------------------------------


async def test_the_day_of_the_time_change(engine, monkeypatch):
    fake_time(monkeypatch, {12: (90, 2.0)})
    sunday = date(2026, 10, 25)  # at 3:00 the clock was set back
    await calendar(engine, [], at_time(sunday, 7))
    assert await morning.review(engine, at_time(sunday, 7, 29)) is False
    assert await morning.review(engine, at_time(sunday, 7, 30)) is True


async def test_document_notices_wait_for_the_summary(engine, monkeypatch):
    """If the 9:00 notice goes first (the Mac woke up late), it waits for the summary so as not
    to repeat the same thing; if the summary does not go out all morning, it goes after
    11:00."""
    fake_time(monkeypatch)
    await document(engine, [(7, "the insurance expires")])
    assert await reminders.review(engine, at_time(TODAY, 9, 5)) == 0
    await calendar(engine, [], at_time(TODAY, 9))
    assert await morning.review(engine, at_time(TODAY, 9, 10)) is True
    assert await reminders.review(engine, at_time(TODAY, 10)) == 0
    assert len(engine.channel.texts()) == 1
    # Another day, no summary all morning (no connection): the notice goes out after 11:00.
    other = TODAY + timedelta(days=7)  # 7 days before the insurance of another document
    await document(engine, [(14, "the permit expires")])
    assert await reminders.review(engine, at_time(other, 10, 30)) == 0
    # The insurance expires that day and the permit has 7 days left: both notices, after 11:00.
    assert await reminders.review(engine, at_time(other, 11, 5)) == 2
    assert "Today the insurance expires" in engine.channel.texts()[-1]


async def test_a_failure_is_retried_after_15_minutes(engine, monkeypatch):
    calls = []

    async def broken(*a: Any, **k: Any) -> weather.Weather:
        calls.append(1)
        raise RuntimeError("something odd")

    monkeypatch.setattr(weather, "rain", broken)
    await calendar(engine, [], at_time(TODAY, 7))
    assert await morning.review(engine, at_time(TODAY, 7, 30)) is False
    assert await morning.review(engine, at_time(TODAY, 7, 31)) is False
    assert len(calls) == 1  # it is not retried every 30 seconds
    monkeypatch.setattr(weather, "rain", RAIN)
    fake_time(monkeypatch, {12: (90, 2.0)})
    assert await morning.review(engine, at_time(TODAY, 7, 46)) is True


async def test_if_the_weather_fails_it_waits_before_saying_so(engine, monkeypatch):
    fake_time(monkeypatch, fails=True)
    await calendar(engine, [], at_time(TODAY, 7))
    assert await morning.review(engine, at_time(TODAY, 7, 30)) is False
    assert engine.channel.texts() == []
    # Half an hour before the limit it is sent anyway, saying it could not be checked.
    assert await morning.review(engine, at_time(TODAY, 10, 31)) is True
    assert "I couldn't check today's weather. Look at the forecast" in engine.channel.texts()[-1]


async def test_a_failing_part_does_not_bring_down_the_rest(engine, monkeypatch):
    async def broken(*a: Any, **k: Any) -> Any:
        raise ValueError("odd date")

    monkeypatch.setattr(life, "next_documents", broken)
    await calendar(engine, [], at_time(TODAY, 7))
    text, marks = await morning.compose(
        engine, at_time(TODAY, 8), weather.Weather(phrase="☔ Rain.")
    )
    assert text.endswith("- ☔ Rain.") and marks == []


async def test_a_calendar_that_cannot_be_read_does_not_stop_the_summary(engine, monkeypatch):
    fake_time(monkeypatch, {12: (90, 2.0)})

    async def broken(*a: Any, **k: Any) -> Any:
        raise TimeoutError("Google is not responding")

    monkeypatch.setattr(life, "refresh_calendar", broken)
    assert await morning.review(engine, at_time(TODAY, 7, 30)) is True
    assert "It's going to rain" in engine.channel.texts()[-1]


async def test_without_connection_its_content_does_not_stay_in_the_log(engine, monkeypatch, caplog):
    import logging

    fake_time(monkeypatch, {12: (90, 2.0)})
    await calendar(engine, [], at_time(TODAY, 7))

    async def no_network() -> bool:
        return False

    monkeypatch.setattr(engine.connection, "there_is", no_network)
    with caplog.at_level(logging.INFO):
        assert await morning.review(engine, at_time(TODAY, 7, 30)) is False
    assert "offline: notice not sent" in caplog.text and "Town" not in caplog.text


async def test_many_documents_do_not_lengthen_the_message_or_get_lost(engine, monkeypatch):
    fake_time(monkeypatch)
    await calendar(engine, [], at_time(TODAY, 7))
    await document(engine, [(7, f"thing {n} expires") for n in range(20)])
    text, marks = await morning.compose(engine, at_time(TODAY, 8), weather.Weather())
    assert len(re.findall(r"thing \d+ expires", text)) == morning.MAX_DOCUMENTS
    assert len(marks) == morning.MAX_DOCUMENTS  # the others are notified afterwards
