"""F8: scheduler, resume after an interruption, idempotency, limits and login (simulated
SDK)."""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from aide.core import scheduler
from aide.core.db import to_date
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.core.scheduler import InvalidTiming, interpret, next_step
from aide.core.states import TaskState
from aide.simulate import Script, Simulator
from tests.fake_google import FakeAuthorization
from tests.fake_sdk import (
    End,
    FakeClient,
    Recorder,
    Tool,
    Wait,
    install_own,
    own,
)

BERLIN = ZoneInfo("Europe/Berlin")
USER = "alex.doe@example.com"


# --- scheduler: dates and policy ---------------------------------------------------------------


def test_when_and_next_run():
    monday = datetime(2026, 10, 19, 10, 0, tzinfo=BERLIN)
    weekly = interpret({"kind": "weekly", "days": ["wednesday", "monday"], "hour": "09:00"}, BERLIN)
    assert weekly.days == ("mon", "wed")
    assert next_step(weekly, monday, BERLIN) == datetime(2026, 10, 21, 9, 0, tzinfo=BERLIN)
    assert scheduler.describe(weekly) == "every Monday and Wednesday at 09:00"
    daily = interpret('{"kind": "daily", "hour": "08:30"}', BERLIN)
    assert next_step(daily, monday, BERLIN) == datetime(2026, 10, 20, 8, 30, tzinfo=BERLIN)
    monthly = interpret({"kind": "monthly", "day": 31, "hour": "07:00"}, BERLIN)
    assert (
        next_step(monthly, datetime(2026, 11, 1, tzinfo=BERLIN), BERLIN).month == 12
    )  # November does not have 31
    one = interpret({"kind": "one_time", "date": "2026-10-21T18:00"}, BERLIN)
    assert next_step(one, monday, BERLIN) == datetime(2026, 10, 21, 18, 0, tzinfo=BERLIN)
    assert next_step(one, datetime(2026, 10, 22, tzinfo=BERLIN), BERLIN) is None
    hours = interpret({"kind": "every_hours", "hours": 6}, BERLIN)
    assert next_step(hours, monday, BERLIN) == monday + timedelta(hours=6)
    # Clock change: the daily run keeps the same local time. (winter time starts on Sunday
    # 10-25-2026 at 03:00).
    saturday = next_step(daily, datetime(2026, 10, 23, 12, 0, tzinfo=BERLIN), BERLIN)
    sunday = next_step(daily, saturday, BERLIN)
    assert (saturday.day, saturday.hour, saturday.utcoffset()) == (24, 8, timedelta(hours=2))
    assert (sunday.day, sunday.hour, sunday.utcoffset()) == (25, 8, timedelta(hours=1))


@pytest.mark.parametrize(
    "data",
    [
        {"kind": "weekly", "days": ["lunesday"], "hour": "09:00"},
        {"kind": "daily", "hour": "25:00"},
        {"kind": "monthly", "day": 40, "hour": "09:00"},
        {"kind": "every_hours", "hours": 0},
        {"kind": "one_time", "date": "tomorrow"},
        {"kind": "cada_minuto"},
        "not json",
    ],
)
def test_invalid_when(data):
    with pytest.raises(InvalidTiming):
        interpret(data, BERLIN)


def test_policy_if_the_mac_was_off():
    window = timedelta(hours=12)
    to_its_hour, afternoon, very_afternoon = (
        timedelta(seconds=20),
        timedelta(hours=3),
        timedelta(hours=13),
    )
    for policy in scheduler.POLICIES:
        assert scheduler.run_with_delay(policy, to_its_hour, window)
    assert scheduler.run_with_delay("run_if_recent", afternoon, window)
    assert not scheduler.run_with_delay("run_if_recent", very_afternoon, window)
    assert scheduler.run_with_delay("run_always", very_afternoon, window)
    assert not scheduler.run_with_delay("skip", afternoon, window)


# --- with the engine ---------------------------------------------------------------------------


@pytest.fixture
async def environment(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    FakeClient.script = staticmethod(lambda p, o, r: [End("<delivery>Done.</delivery>")])
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    install_own(monkeypatch)
    engine = Engine(config_tmp)
    await engine.start()
    sim = Simulator(engine, Script(), print_out=False)
    yield engine, sim
    with contextlib.suppress(Exception):
        await engine.stop()


async def _scheduled_task(engine: Engine, timing: dict, policy: str, delay: timedelta) -> int:
    moment = datetime.now(UTC) - delay
    return await engine.db.run(
        "INSERT INTO scheduled_tasks(description, timing, policy, active, created, next_run, timing_text) "
        "VALUES (?, ?, ?, 1, ?, ?, ?)",
        (
            "Check whether any company has replied",
            interpret(timing, BERLIN).to_json(),
            policy,
            moment.isoformat(),
            moment.isoformat(timespec="seconds"),
            "test",
        ),
    )


async def test_schedule_list_and_cancel_with_approval(environment):
    engine, sim = environment
    timing = {"kind": "weekly", "days": ["monday"], "hour": "09:00"}
    FakeClient.script = lambda p, o, r: [
        own(
            "schedule_task",
            {"description": "Check the new job listings", "timing": timing},
        ),
        own("list_scheduled_tasks", {}),
        own("cancel_scheduled_task", {"id": 1}),
        End("<delivery>Scheduled.</delivery>"),
    ]
    sim.script.approve = deque(["yes", "no"])  # scheduling yes; cancelling no
    await sim.send("Every Monday at 9, check the new job listings")
    row = await engine.db.one("SELECT * FROM scheduled_tasks WHERE id=1")
    assert row["active"] == 1 and row["timing_text"] == "every Monday at 09:00"
    next_run = datetime.fromisoformat(row["next_run"]).astimezone(BERLIN)
    assert next_run.weekday() == 0 and (next_run.hour, next_run.minute) == (9, 0)
    requests = [m.plain_text for m in sim.channel.messages if "apr:" in "".join(m.buttons_data)]
    assert "When: every Monday at 09:00" in requests[0] and "Next run" in requests[0]
    assert "If the Mac is off" in requests[0]
    assert "Cancel scheduled task 1" in requests[1]
    # Without approval, the tool does not write even if called directly.
    r = await _own_direct("schedule_task", {"description": "x", "timing": timing})
    assert r.get("is_error") and "approval" in r["content"][0]["text"]


async def _own_direct(name: str, entry: dict) -> dict:
    from tests.fake_sdk import OWN

    return await OWN[name].handler(entry)


async def test_overdue_scheduled_task_runs_and_advances(environment):
    engine, sim = environment
    pid = await _scheduled_task(
        engine, {"kind": "daily", "hour": "09:00"}, "run_if_recent", timedelta(minutes=1)
    )
    [task_id] = await engine.check_scheduled_tasks()
    await sim.wait_rest()
    task = await engine.tasks.require(task_id)
    assert task.state == TaskState.COMPLETED and task.origin == "scheduled_task"
    assert "Check whether any company has replied" in task.request
    row = await engine.db.one("SELECT * FROM scheduled_tasks WHERE id=?", (pid,))
    assert row["last_task"] == task_id and row["active"] == 1
    assert datetime.fromisoformat(row["next_run"]) > datetime.now(UTC)
    assert await engine.check_scheduled_tasks() == []  # does not repeat


@pytest.mark.parametrize(
    "policy,delay,runs",
    [
        ("run_if_recent", timedelta(hours=3), True),
        ("run_if_recent", timedelta(hours=13), False),
        ("run_always", timedelta(days=2), True),
        ("skip", timedelta(hours=1), False),
    ],
)
async def test_scheduled_task_missed_with_the_mac_off(environment, policy, delay, runs):
    engine, sim = environment
    pid = await _scheduled_task(
        engine, {"kind": "one_time", "date": "2026-01-01T09:00"}, policy, delay
    )
    launched = await engine.check_scheduled_tasks()
    await sim.wait_rest()
    assert bool(launched) is runs
    row = await engine.db.one("SELECT * FROM scheduled_tasks WHERE id=?", (pid,))
    assert row["active"] == 0  # it was one-time
    if not runs:
        assert any("was skipped" in t for t in sim.channel.texts())


# --- program interruption and resume -------------------------------------------------------------


async def kill(engine: Engine) -> None:
    """Simulates a kill: running tasks are cut off without going through the clean shutdown."""
    for t in [*engine._workers, *engine._background]:
        t.cancel()
    for run in list(engine.in_progress.values()):
        if run.async_task is not None:
            run.async_task.cancel()
    await asyncio.sleep(0.1)
    await engine.awake.close()
    await engine.db.close()


async def test_interruption_midway_through_a_task_resumes_without_repeating(
    config_tmp, monkeypatch
):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    steps = []

    def script(prompt, options, registry):
        if "is resuming now" in prompt:
            return [End("<delivery>Finished after the cut.</delivery>")]
        return [
            Tool("Bash", {"command": "echo step 1"}, lambda e: steps.append(1) or "step 1"),
            Wait(30),  # the program is interrupted here
            Tool("Bash", {"command": "echo step 2"}, lambda e: steps.append(2) or "step 2"),
            End("<delivery>Should not get here.</delivery>"),
        ]

    FakeClient.script = script
    engine = Engine(config_tmp)
    await engine.start()
    sim = Simulator(engine, Script(), print_out=False)
    task = await engine.create_direct_task("Do two steps")
    for _ in range(50):
        if steps:
            break
        await asyncio.sleep(0.1)
    original_session = (await engine.tasks.require(task.id)).session_id
    assert original_session  # saved at the start of the turn, not at the end
    await kill(engine)

    new = Engine(config_tmp)
    await new.start()
    sim = Simulator(new, Script(), print_out=False)
    assert await new.recover_to_start() == [task.id]
    await sim.wait_rest()
    final = await new.tasks.require(task.id)
    assert final.state == TaskState.COMPLETED and steps == [1]
    assert FakeClient.registry.options[-1].resume == original_session
    assert any("back up and running" in t for t in sim.channel.texts())
    transitions = [t["until"] for t in await new.tasks.transitions(task.id)]
    assert "paused_shutdown" in transitions
    await new.stop()


async def test_interruption_right_after_sending_an_email_does_not_duplicate_it(
    config_tmp, monkeypatch
):
    from aide.core.idempotency import ActionLog

    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    install_own(monkeypatch)
    google = FakeAuthorization()
    sent = asyncio.Event()
    original = ActionLog.complete

    async def complete_and_die(self, key, result):
        if google.gmail.sent and not sent.is_set():
            sent.set()
            await asyncio.sleep(3600)  # the program dies before recording the send as done
        await original(self, key, result)

    monkeypatch.setattr(ActionLog, "complete", complete_and_die)

    def script(prompt, options, registry):
        if "is resuming now" in prompt:
            # After the interruption, the agent tries the send again (the worst that
            # could happen).
            return [
                own("gmail_send_draft", {"draft_id": "d2"}),
                End("<delivery>Done.</delivery>"),
            ]
        return [
            own("gmail_create_draft", {"to": [USER], "subject": "Test", "body": "Hello"}),
            own("gmail_send_draft", {"draft_id": "d2"}),
            End("<delivery>Sent.</delivery>"),
        ]

    FakeClient.script = script
    engine = Engine(config_tmp)
    await engine.start()
    engine.core.services["google"] = google
    sim = Simulator(engine, Script(approve=deque(["yes"])), print_out=False)
    task = await engine.create_direct_task("Send me a test email")
    await asyncio.wait_for(sent.wait(), timeout=10)
    assert len(google.gmail.sent) == 1
    await kill(engine)

    monkeypatch.setattr(ActionLog, "complete", original)
    new = Engine(config_tmp)
    await new.start()
    new.core.services["google"] = google
    sim = Simulator(new, Script(default_approve="yes"), print_out=False)
    await new.recover_to_start()
    await sim.wait_rest()
    assert len(google.gmail.sent) == 1  # it was not duplicated
    assert (await new.tasks.require(task.id)).state == TaskState.COMPLETED
    done = await new.core.actions.done_descriptions(task.id)
    assert any("send_email" in h for h in done)
    await new.stop()


async def test_recover_does_not_touch_this_startups_work(environment):
    engine, sim = environment
    task = await engine.tasks.create("message that just arrived", state=TaskState.RECEIVED)
    assert await engine.recover_to_start() == []
    assert (await engine.tasks.require(task.id)).state == TaskState.RECEIVED


# --- plan limits and login -----------------------------------------------------------------------


async def test_simulated_limit_pauses_and_resumes_by_itself(environment):
    engine, sim = environment
    reset = int(time.time()) + 3600

    def script(prompt, options, registry):
        if "is resuming now" in prompt:
            return [End("<delivery>Done after the limit.</delivery>")]
        return [
            End(
                "You've hit your session limit",
                is_error=True,
                error_assistant="rate_limit",
                limit={
                    "status": "rejected",
                    "resets_at": reset,
                    "rate_limit_type": "five_hour",
                },
            )
        ]

    FakeClient.script = script
    [task_id] = await sim.send("Do something long")
    task = await engine.tasks.require(task_id)
    assert task.state == TaskState.PAUSED_LIMIT
    assert abs(to_date(task.pause_until).timestamp() - reset) < 2
    assert await engine.check_limits() == []  # it is not time yet
    # The reset time arrives.
    past = (datetime.now(UTC) - timedelta(seconds=5)).isoformat(timespec="seconds")
    await engine.tasks.update(task_id, pause_until=past)
    assert await engine.check_limits() == [task_id]
    await sim.wait_rest()
    assert (await engine.tasks.require(task_id)).state == TaskState.COMPLETED
    assert any("plan limit has reset" in t for t in sim.channel.texts())


async def test_expired_login_resumes_on_return(environment):
    engine, sim = environment
    valid = {"ok": False}

    async def valid_login() -> bool:
        return valid["ok"]

    engine.valid_login = valid_login

    def script(prompt, options, registry):
        if "is resuming now" in prompt or "still there" in prompt:
            return [End("<delivery>Done after the login.</delivery>")]
        return [
            End(
                "Failed to authenticate: OAuth session expired",
                is_error=True,
                error_assistant="authentication_failed",
            )
        ]

    FakeClient.script = script
    [task_id] = await sim.send("Do something")
    assert (await engine.tasks.require(task_id)).state == TaskState.PAUSED_LOGIN
    assert await engine.check_login() == []
    valid["ok"] = True
    # When the user writes, the pauses are also checked.
    await sim.send("Are you still there?")
    await sim.wait_rest()
    assert (await engine.tasks.require(task_id)).state == TaskState.COMPLETED
    assert any("works again" in t for t in sim.channel.texts())


# --- background work and task closing -------------------------------------------------------------


async def test_turn_waits_for_background_work(environment):
    from tests.fake_sdk import Background

    engine, sim = environment
    done = []
    FakeClient.script = lambda p, o, r: [
        Background(
            "bash-1",
            "local_bash",
            0.5,
            [
                Tool("Bash", {"command": "echo step 2"}, lambda e: done.append(2) or "ok"),
                End("<delivery>Finished after the wait.</delivery>"),
            ],
        ),
        End("Waiting for the background command to finish."),
    ]
    [task_id] = await sim.send("Do something that waits")
    assert (await engine.tasks.require(task_id)).state == TaskState.COMPLETED
    assert done == [2]
    assert any("Finished after the wait" in t for t in sim.channel.texts())
    events = [e["kind"] for e in await engine.tasks.events(task_id)]
    assert "waiting_plain_second" in events and "missing_delivery" not in events


async def test_background_work_that_never_ends_has_a_cap(environment, monkeypatch):
    from aide.core import orchestrator
    from tests.fake_sdk import Background

    engine, sim = environment
    monkeypatch.setattr(orchestrator, "BASH_BACKGROUND_WAIT", 0.5)

    def script(prompt, options, registry):
        if "is missing the <delivery>" in prompt:
            return [End("<delivery>Server running on port 8000.</delivery>")]
        return [Background("servidor", "local_bash", 3600, notifies=False), End("Server started.")]

    FakeClient.script = script
    [task_id] = await sim.send("Start the website locally")
    assert (await engine.tasks.require(task_id)).state == TaskState.COMPLETED
    events = [e["kind"] for e in await engine.tasks.events(task_id)]
    assert "background_unfinished" in events and "missing_delivery" in events
    assert any("port 8000" in t for t in sim.channel.texts())


async def test_without_a_delivery_block_it_asks_to_finish_once(environment):
    engine, sim = environment
    calls = []

    def script(prompt, options, registry):
        calls.append(prompt)
        if "is missing the <delivery>" in prompt:
            return [End("<delivery>Now it is really done.</delivery><summary>Done.</summary>")]
        return [End("I am halfway there.")]

    FakeClient.script = script
    [task_id] = await sim.send("Do something")
    assert len(calls) == 2
    assert any("Now it is really done" in t for t in sim.channel.texts())
    assert (await engine.tasks.require(task_id)).state == TaskState.COMPLETED


async def test_after_delivery_it_does_not_wait_for_a_background_server(environment):
    from tests.fake_sdk import Background

    engine, sim = environment
    FakeClient.script = lambda p, o, r: [
        Background("servidor", "local_bash", 3600, notifies=False),
        End("<delivery>Website running at http://127.0.0.1:8000.</delivery>"),
    ]
    start = time.monotonic()
    [task_id] = await sim.send("Start the website and leave it running")
    assert time.monotonic() - start < 15  # it does not wait for the 5-minute cap
    assert (await engine.tasks.require(task_id)).state == TaskState.COMPLETED
    events = [e["kind"] for e in await engine.tasks.events(task_id)]
    assert "waiting_plain_second" not in events and "missing_delivery" not in events


async def test_clean_shutdown_midway_through_a_turn_does_not_retry_and_resumes(
    config_tmp, monkeypatch
):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    steps = []

    def script(prompt, options, registry):
        if "is resuming now" in prompt:
            return [
                Tool("Bash", {"command": "echo step 2"}, lambda e: steps.append(2) or "ok"),
                End("<delivery>Finished after the shutdown.</delivery>"),
            ]
        return [
            Tool("Bash", {"command": "echo step 1"}, lambda e: steps.append(1) or "ok"),
            Wait(60),
            End("<delivery>Should not get here.</delivery>"),
        ]

    FakeClient.script = script
    engine = Engine(config_tmp)
    await engine.start()
    task = await engine.create_direct_task("Two steps with a wait")
    for _ in range(50):
        if steps:
            break
        await asyncio.sleep(0.1)
    start = time.monotonic()
    await asyncio.wait_for(engine.stop(), timeout=30)
    assert time.monotonic() - start < 10

    new = Engine(config_tmp)
    await new.start()
    sim = Simulator(new, Script(), print_out=False)
    t = await new.tasks.require(task.id)
    assert t.state == TaskState.PAUSED_SHUTDOWN
    assert "retry" not in [e["kind"] for e in await new.tasks.events(task.id)]
    assert await new.recover_to_start() == [task.id]
    await sim.wait_rest()
    assert (await new.tasks.require(task.id)).state == TaskState.COMPLETED
    assert steps == [1, 2]
    await new.stop()


async def test_closing_session_is_idempotent_and_does_not_block(config_tmp, monkeypatch):
    from aide.core import orchestrator
    from aide.core.context import SessionContext

    monkeypatch.setattr(orchestrator, "CLOSING_WAIT", 0.5)

    class StuckClient(FakeClient):
        closings = 0

        async def disconnect(self) -> None:
            type(self).closings += 1
            # Like the SDK's protected close: it does not respond to cancellation and
            # takes longer than the maximum wait (the SDK's is bounded to about 20 s;
            # here, 1.5 s).
            end = time.monotonic() + 1.5
            while time.monotonic() < end:
                with contextlib.suppress(asyncio.CancelledError):
                    await asyncio.sleep(0.1)

    monkeypatch.setattr(AgentSession, "client_factory", StuckClient)
    engine = Engine(config_tmp)
    await engine.start()
    ses = SessionContext(core=engine.core, task_id=None, folder=None, role="quick")
    from claude_agent_sdk import ClaudeAgentOptions

    session = AgentSession(ses, ClaudeAgentOptions(), "test")
    await session.open_()
    start = time.monotonic()
    await asyncio.gather(session.close(), session.close())  # two closings at once
    assert time.monotonic() - start < 5
    assert StuckClient.closings == 1
    await engine.stop()
