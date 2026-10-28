"""Your life in one view: the panel's “Today” page, the calendar read by code and
the week review of the Sunday report. No network or model."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import httpx
import pytest

from aide import life
from aide.bot.channel import FakeChannel
from aide.core.engine import Engine
from aide.core.states import TaskState
from aide.maintenance import report
from aide.panel.app import COOKIE, Panel
from aide.tools.google_auth import GoogleNotAuthorized, GoogleUnauthorized

ZONE = ZoneInfo("Europe/Berlin")
# A Sunday at noon, Munich time.
MOMENT = datetime(2026, 10, 25, 12, 0, tzinfo=ZONE)


@pytest.fixture
async def engine(config_tmp):
    m = Engine(config_tmp, channel=FakeChannel(print_out=False))
    await m.start(workers=False)
    yield m
    await m.stop()


class FakeGoogle:
    """Simulated calendar: returns the events it is given or raises an error."""

    def __init__(self, events: list[dict[str, Any]] | None = None, error: Exception | None = None):
        self.events = events or []
        self.error = error
        self.requested: list[dict[str, Any]] = []

    def service(self, name: str, version: str) -> Any:
        assert (name, version) == ("calendar", "v3")
        if self.error is not None:
            raise self.error
        fake = self

        class ListBlock:
            def __init__(self, **kw: Any) -> None:
                fake.requested.append(kw)

            def execute(self) -> dict[str, Any]:
                return {"items": fake.events}

        class Events:
            def list(self, **kw: Any) -> ListBlock:
                return ListBlock(**kw)

        return SimpleNamespace(events=lambda: Events())


def event(title: str, start: str, end: str | None = None, **extra: Any) -> dict[str, Any]:
    key = "date" if len(start) == 10 else "dateTime"
    return {
        "summary": title,
        "start": {key: start},
        "end": {key: end or start},
        **extra,
    }


# --- calendar -----------------------------------------------------------------------------


async def test_calendar_is_read_by_code_and_grouped_by_days(engine):
    google = FakeGoogle(
        [
            event("Dentist", "2026-10-25T16:30:00+01:00", location="Downtown"),
            event("Sam's birthday", "2026-10-26"),
            event("Trip", "2026-10-24", "2026-10-27"),  # spanning several days, continues today
            event("German class", "2026-10-28T18:00:00+01:00"),
            event("Canceled", "2026-10-26T10:00:00+01:00", status="cancelled"),
        ]
    )
    engine.core.services["google"] = google
    assert await life.refresh_calendar(engine.core, MOMENT) == 4
    [requested] = google.requested
    assert requested["time_min"].startswith("2026-10-25T00:00:00")
    assert requested["time_max"].startswith("2026-11-02T00:00:00") and requested["single_events"]
    c = await life.calendar(engine.core, MOMENT)
    assert [d["name"] for d in c["days"]] == ["Today", "Tomorrow", "Wednesday 28"]
    today = {e["title"]: e["hour"] for e in c["today"]}
    assert today == {"Dentist": "16:30", "Trip": "all day"}
    assert c["days"][1]["events"][0]["title"] == "Sam's birthday" and c["read"]
    assert c["error"] is None and c["updated"] == "12:00" and not c["old"]
    assert c["notice"] is None and c["note"] == "Read at 12:00."


async def test_if_google_fails_the_last_data_stays_and_a_notice_is_sent(engine):
    engine.core.services["google"] = FakeGoogle([event("Dentist", "2026-10-25T16:30:00+01:00")])
    await life.refresh_calendar(engine.core, MOMENT)
    engine.core.services["google"] = FakeGoogle(error=GoogleNotAuthorized("expired"))
    assert await life.refresh_calendar(engine.core, MOMENT) is None
    c = await life.calendar(engine.core, MOMENT)
    assert c["error"] == "google" and c["today"][0]["title"] == "Dentist"
    assert c["notice"].startswith("Google is asking you to renew the authorization")
    # Without Google configured, nothing is attempted.
    del engine.core.services["google"]
    assert await life.refresh_calendar(engine.core, MOMENT) is None


# --- the week -----------------------------------------------------------------------------


async def test_week_counted_by_code(engine):
    db = engine.db
    end = datetime.now(UTC) + timedelta(hours=1)
    inside = (end - timedelta(days=2)).isoformat(timespec="seconds")
    outside = (end - timedelta(days=9)).isoformat(timespec="seconds")
    done = await engine.tasks.create("Summarize the website", state=TaskState.RUNNING)
    await engine.tasks.change_state(done.id, TaskState.COMPLETED, "test")
    broken = await engine.tasks.create("Read the PDF", state=TaskState.RUNNING)
    await engine.tasks.change_state(broken.id, TaskState.FAILED, "test")
    await engine.tasks.update(broken.id, error="could not\nopen the file")
    # A task that finished 10 days ago and was touched this week does not count as from this
    # week.
    old = await engine.tasks.create("Something from before", state=TaskState.RUNNING)
    await engine.tasks.change_state(old.id, TaskState.COMPLETED, "test")
    await db.run(
        "UPDATE transitions SET hour=? WHERE task_id=?",
        ((end - timedelta(days=10)).isoformat(timespec="seconds"), old.id),
    )
    await engine.tasks.update(old.id, summary="touched today")
    for tokens, hour in ((300_000, inside), (100_000, outside)):
        await db.run(
            "INSERT INTO usage(task_id, hour, origin, model, input_tokens, output_tokens, "
            "cache_read, cache_creation, cost_usd, turns, web_searches) VALUES "
            "(NULL, ?, 'quick', 'claude-sonnet-5-5', ?, 0, 0, 0, 0, 1, 0)",
            (hour, tokens),
        )
    s = await life.week(engine.core, end)
    assert s["tasks"] == {"completed": 1, "failed": 1} and s["consumption"] == 300_000
    text = life.week_text(s)
    assert "I finished 1 task." in text
    assert f"1 task failed: task {broken.id} (could not open the file)." in text
    assert "Usage went up 200%" in text
    assert text.index("What went well") < text.index("To improve")


def test_a_quiet_week():
    s = {
        "tasks": {},
        "failed": [],
        "consumption": 0,
        "previous_consumption": 0,
        "expire": [],
    }
    assert life.week_text(s).startswith("A quiet week")


def test_report_carries_your_week_and_stays_within_the_cap():
    text, _ = report.compose(
        [],
        "x" * 5000,
        [],
        MOMENT,
        False,
        None,
        "What went well\n- I finished 2 tasks.",
    )
    assert "<b>Your week</b>\nWhat went well" in text
    assert text.index("Your week") < text.index("xxxx")
    assert len(text) <= report.MESSAGE_LIMIT + 10


# --- the panel: “Today” ---------------------------------------------------------------------


async def test_today_page_puts_everything_together(engine):
    db = engine.db
    engine.core.services["google"] = FakeGoogle(
        [event("Dentist", datetime.now(ZONE).replace(hour=23, minute=0).isoformat())]
    )
    await life.refresh_calendar(engine.core)
    await db.run(
        "INSERT INTO emails(id, thread, received, arrival, from_name, from_address, subject, "
        "start, bulk, category, decision, reason, reviewed) VALUES ('m1', 'h1', ?, 0, "
        "'Northbank', 'alerts@northbank.example', 'Your October statement', '', 0, 'bank', 'notice', "
        "'bank sender', ?)",
        (datetime.now(UTC).isoformat(), datetime.now(UTC).isoformat(timespec="seconds")),
    )
    p = Panel(engine, token="token-de-prueba")
    transport = httpx.ASGITransport(app=p.app)
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://127.0.0.1:{p.port}", follow_redirects=False
    ) as client:
        r = await client.get("/enter?t=token-de-prueba&go=/")
        assert COOKIE in r.cookies
        html = (await client.get("/")).text
    assert re.search(r"<h1>Good (morning|afternoon|evening)</h1>", html)  # sin USER_NAME
    assert "Dentist" in html and "23:00" in html
    assert "Your October statement" in html and "Northbank" in html
    assert "Jobs" not in html and "Shop" not in html and "application" not in html
    assert "Usage" not in html and "Plan limits" not in html
    assert "No dates from your documents" in html
    assert re.search(r'href="https://mail\.google\.com/mail/u/0/#all/h1"', html)


def test_day_name():
    today = date(2026, 10, 25)
    assert life.day_name(today, today) == "Today"
    assert life.day_name(date(2026, 10, 26), today) == "Tomorrow"
    assert life.day_name(date(2026, 10, 29), today) == "Thursday 29"


async def test_engine_reads_the_calendar_every_half_hour(engine):
    google = FakeGoogle([event("Dentist", "2026-10-25T16:30:00+01:00")])
    engine.core.services["google"] = google
    await engine.check_all()
    await asyncio.gather(*list(engine._background), return_exceptions=True)
    assert len(google.requested) == 1
    await engine.check_all()  # within half an hour, it is not read again
    await asyncio.gather(*list(engine._background), return_exceptions=True)
    assert len(google.requested) == 1
    engine._last_calendar -= 31 * 60
    await engine.check_all()
    await asyncio.gather(*list(engine._background), return_exceptions=True)
    assert len(google.requested) == 2


# --- review of the calendar ------------------------------------------------------


async def test_what_continues_from_yesterday_and_multi_day_items(engine):
    me = [{"email": "alex.doe@example.com", "self": True, "responseStatus": "declined"}]
    engine.core.services["google"] = FakeGoogle(
        [
            event("Trip", "2026-10-24T18:00:00+02:00", "2026-10-27T10:00:00+01:00"),
            event("Night shift", "2026-10-24T22:00:00+02:00", "2026-10-25T02:00:00+01:00"),
            event("Fair", "2026-10-26", "2026-10-29"),  # from the 26th to the 28th
            event("Dinner", "2026-10-25T20:00:00+01:00", "2026-10-26T00:00:00+01:00"),
            event("Concert", "2026-10-25T23:00:00+01:00", "2026-10-26T01:30:00+01:00"),
            event("Declined meeting", "2026-10-25T09:00:00+01:00", attendees=me),
            event("Already over", "2026-10-23T10:00:00+02:00", "2026-10-24T11:00:00+02:00"),
        ]
    )
    assert await life.refresh_calendar(engine.core, MOMENT) == 6
    c = await life.calendar(engine.core, MOMENT)
    today = {e["title"]: (e["hour"], e["until"]) for e in c["today"]}
    assert today == {
        "Trip": ("ongoing", "until Tuesday 27 at 10:00"),
        "Night shift": ("ongoing", "until 02:00"),
        "Dinner": ("20:00", None),
        "Concert": ("23:00", "until tomorrow at 01:30"),
    }
    [morning] = [d for d in c["days"] if d["name"] == "Tomorrow"]
    assert [(e["title"], e["hour"], e["until"]) for e in morning["events"]] == [
        ("Fair", "all day", "until Wednesday 28")
    ]


async def test_after_midnight_the_local_day_counts(engine):
    # 00:30 on Monday in Munich is still 23:30 on Sunday in UTC.
    moment = datetime(2026, 10, 25, 23, 30, tzinfo=UTC)
    assert life.local_today(engine.core, moment) == date(2026, 10, 26)
    assert life.day_in_phrase(date(2026, 10, 26), date(2026, 10, 26)) == "today"
    assert life.day_in_phrase(date(2026, 10, 27), date(2026, 10, 26)) == "tomorrow"
    assert life.day_in_phrase(date(2026, 10, 29), date(2026, 10, 26)) == "Thursday 29"


async def test_an_old_or_failing_calendar_is_reported(engine):
    engine.core.services["google"] = FakeGoogle([])
    await life.refresh_calendar(engine.core, MOMENT)
    c = await life.calendar(engine.core, MOMENT + timedelta(minutes=90))
    assert not c["old"] and c["notice"] is None
    c = await life.calendar(engine.core, MOMENT + timedelta(hours=3))
    assert c["old"] and c["notice"] == (
        "I haven't been able to read your calendar since 12:00: something may be missing."
    )
    c = await life.calendar(engine.core, MOMENT + timedelta(days=1))
    assert "since yesterday at 12:00" in c["notice"]
    c = await life.calendar(engine.core, MOMENT + timedelta(days=9))
    assert "since Oct 25 at 12:00" in c["notice"]
    # A failure that is not an authorization one is also reported, even if the reading is
    # recent.
    engine.core.services["google"] = FakeGoogle(error=TimeoutError("slow"))
    await life.refresh_calendar(engine.core, MOMENT + timedelta(minutes=30))
    c = await life.calendar(engine.core, MOMENT + timedelta(minutes=31))
    assert c["error"] == "TimeoutError" and "since 12:00" in c["notice"]
    # With no good reading: “not yet”.
    await engine.db.kv_delete(life.CALENDAR_KEY)
    await life.refresh_calendar(engine.core, MOMENT)
    c = await life.calendar(engine.core, MOMENT)
    assert not c["read"] and c["notice"].startswith("I haven't been able to read your calendar yet")
    # Google never authorized does not say “has expired”.
    engine.core.services["google"] = FakeGoogle(error=GoogleUnauthorized("never"))
    await life.refresh_calendar(engine.core, MOMENT)
    c = await life.calendar(engine.core, MOMENT)
    assert c["notice"].startswith("Google isn't authorized yet")


async def test_page_does_not_say_nothing_in_your_calendar_if_the_reading_is_old(engine):
    ago = (datetime.now(UTC) - timedelta(days=9)).isoformat(timespec="seconds")
    await engine.db.kv_put(life.CALENDAR_KEY, {"updated": ago, "events": []})
    html = await home_page(engine)
    assert "read your calendar since Oct" in html
    assert "Nothing on your calendar" not in html
    await engine.db.kv_put(
        life.CALENDAR_KEY,
        {"updated": datetime.now(UTC).isoformat(timespec="seconds"), "events": []},
    )
    html = await home_page(engine)
    assert "Nothing on your calendar this week." in html and "Read at" in html


async def test_a_failing_card_does_not_break_the_page(engine, monkeypatch):
    async def broken(*a: Any, **k: Any) -> Any:
        raise RuntimeError("email is failing")

    monkeypatch.setattr(life, "important_emails", broken)
    html = await home_page(engine)
    assert "<h1>Good" in html and "Coming due" in html


async def test_live_update_sees_the_calendar_and_the_day(engine):
    from aide.panel.views import Views

    v = Views(engine)
    before = await v.version()
    assert datetime.now(ZONE).date().isoformat() in before
    engine.core.services["google"] = FakeGoogle([])
    await life.refresh_calendar(engine.core)
    assert await v.version() != before


async def test_on_google_renewal_the_calendar_is_read_right_away(engine):
    google = FakeGoogle([])
    engine.core.services["google"] = google
    await engine.reauthorized_google("alex.doe@example.com")
    await asyncio.gather(*list(engine._background), return_exceptions=True)
    assert len(google.requested) == 1


async def test_the_week_starts_at_the_previous_report(engine):
    end = datetime(2026, 10, 26, 8, 0, tzinfo=UTC)  # the report came out late, on Monday
    previous = datetime(2026, 10, 18, 18, 0, tzinfo=UTC)
    await engine.db.kv_put("last_maintenance", previous.isoformat(timespec="seconds"))
    assert await life.since_last_report(engine.db, end) == previous
    await engine.db.kv_put("last_maintenance", "2026-09-01T18:00:00+00:00")
    assert await life.since_last_report(engine.db, end) == end - timedelta(days=7)


def test_plural_and_singular_texts():
    base = {
        "tasks": {},
        "failed": [],
        "consumption": 0,
        "previous_consumption": 0,
        "expire": [{"date": "2026-11-20", "title": None, "description": "the insurance expires"}],
    }
    one = life.week_text({**base, "tasks": {"completed": 1}})
    assert "I finished 1 task." in one
    assert "A document: the insurance expires, on Nov 20." in one and "None" not in one
    several = life.week_text({**base, "tasks": {"completed": 3, "failed": 2}})
    assert "I finished 3 tasks." in several and "2 tasks failed" in several


def test_a_long_report_does_not_lose_the_google_link():
    proposals = [
        {
            "id": n,
            "kind": "A",
            "title": f"Suggestion {n}",
            "reason": "Just because & for this. " * 20,
            "change": {"file": "agent/CLAUDE.md", "text": "a <long> rule " * 40},
        }
        for n in range(1, 6)
    ]
    week = "What went well\n" + "\n".join(f"- Thing {n} that went well & more." for n in range(80))
    text, buttons = report.compose(
        [],
        "Text from the model. " * 400,
        proposals,
        MOMENT,
        False,
        "http://127.0.0.1:8765/x",
        week,
    )
    assert len(text) <= report.MESSAGE_LIMIT
    assert "open this link on the Mac: http://127.0.0.1:8765/x" in text
    assert text.count("Thing ") < 80 and "Text from the model." in text
    # No HTML entity split by the truncation.
    assert not re.search(r"&(?!amp;|lt;|gt;|quot;|#)", text)
    assert len(buttons) == 5


async def home_page(engine) -> str:
    p = Panel(engine, token="token-de-prueba")
    transport = httpx.ASGITransport(app=p.app)
    async with httpx.AsyncClient(
        transport=transport, base_url=f"http://127.0.0.1:{p.port}", follow_redirects=False
    ) as client:
        await client.get("/enter?t=token-de-prueba&go=/")
        r = await client.get("/")
        assert r.status_code == 200
        return r.text


async def test_a_retried_task_counts_by_how_it_ended(engine):
    # It failed and was retried until completed: it counts as completed, not also as failed.
    retried = await engine.tasks.create("Find the timetable", state=TaskState.RUNNING)
    await engine.tasks.change_state(retried.id, TaskState.FAILED, "test")
    await engine.tasks.change_state(retried.id, TaskState.RUNNING, "retry")
    await engine.tasks.change_state(retried.id, TaskState.COMPLETED, "test")
    # Completed and then continued: it is still running, it does not count as finished.
    consecutive = await engine.tasks.create("Make the list", state=TaskState.RUNNING)
    await engine.tasks.change_state(consecutive.id, TaskState.COMPLETED, "test")
    await engine.tasks.change_state(consecutive.id, TaskState.RUNNING, "follow-up")
    s = await life.week(engine.core, datetime.now(UTC) + timedelta(hours=1))
    assert s["tasks"] == {"completed": 1} and s["failed"] == []
