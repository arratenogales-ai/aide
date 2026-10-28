"""How states (5 groups) and usage (tokens, no dollars) are shown to the user.

Live progress: TaskCreate and TaskUpdate steps, what it is doing now, subagents and time."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from aide.bot.channel import Entry
from aide.bot.commands import run_command
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.core.presentation import GROUPS, readable_state, subtitle, task_group
from aide.core.progress import (
    _id_created,
    describe_tool,
    duration,
    log_tool,
    progress,
)
from aide.core.states import TaskState
from aide.core.tasks import Task
from aide.core.usage import Consumption, bar, format_tokens
from aide.panel.app import Panel
from aide.simulate import SIMULATED_USER, Script, Simulator
from tests.fake_sdk import End, FakeClient, Recorder, Tool, Wait

# --- from test_presentation ------------------------------------------------

ZONE = "Europe/Berlin"


def task(state: TaskState, **fields) -> Task:
    base = dict(
        id=1,
        created="2026-10-20T10:00:00+00:00",
        updated="2026-10-20T10:00:00+00:00",
        origin="telegram",
        kind="task",
        request="Test",
        title="Test",
        state=state.value,
        size=None,
        needs_plan=0,
        needs_browser=0,
        external_actions=0,
        model=None,
        forced_model=None,
        phase=None,
        plan=None,
        session_id=None,
        session_used=None,
        turn_cap=0,
        usd_cap=0.0,
        turns_used=0,
        usd_used=0.0,
        result=None,
        summary=None,
        error=None,
        reviews=0,
        parent_task=None,
        scheduled_task_id=None,
        pause_until=None,
        pause_reason=None,
        previous_state=None,
        next_step=None,
    )
    return Task(**{**base, **fields})


def test_the_16_states_fall_into_one_of_the_5_groups():
    assert [g.name for g in GROUPS] == [
        "In progress",
        "Waiting for you",
        "Paused",
        "Completed",
        "Canceled or failed",
    ]
    everything = [e for g in GROUPS for e in g.states]
    assert len(everything) == len(set(everything)) == len(TaskState) == 16
    expected = {
        "In progress": {
            "received",
            "in_triage",
            "planning",
            "in_queue",
            "running",
            "reviewing",
        },
        "Waiting for you": {"waiting_plan", "waiting_approval"},
        "Paused": {e.value for e in TaskState if e.value.startswith("paused_")},
        "Completed": {"completed"},
        "Canceled or failed": {"cancelled", "failed"},
    }
    for g in GROUPS:
        assert {e.value for e in g.states} == expected[g.name]
    for state in TaskState:
        assert task_group(state).name in expected and task_group(state.value) == task_group(state)


def test_reason_for_each_pause_in_plain_language():
    moment = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    until = datetime(2026, 10, 20, 13, 20, tzinfo=UTC)  # 15:20 in Berlin
    morning = (until + timedelta(days=1)).isoformat()
    cases = [
        (
            task(TaskState.PAUSED_LIMIT, pause_until=until.isoformat()),
            "plan limit, resumes at 15:20",
        ),
        (
            task(TaskState.PAUSED_LIMIT, pause_until=morning),
            "plan limit, resumes tomorrow at 15:20",
        ),
        (
            task(TaskState.PAUSED_LIMIT, pause_until=(until + timedelta(days=3)).isoformat()),
            "plan limit, resumes on Oct 23 at 15:20",
        ),
        (task(TaskState.PAUSED_LIMIT), "plan limit, resumes on its own"),
        (task(TaskState.PAUSED_BROWSER, pause_reason="Chrome no disponible"), "Chrome closed"),
        (task(TaskState.PAUSED_LOGIN, pause_reason="login caducado"), "Claude session expired"),
        (task(TaskState.PAUSED_LOGIN, pause_reason="google"), "Google authorization expired"),
        (task(TaskState.PAUSED_CAP), "usage cap reached, reply in Telegram"),
        (task(TaskState.PAUSED_SHUTDOWN, pause_reason="paused by the user"), "paused by you"),
        (task(TaskState.PAUSED_SHUTDOWN), "the program stopped, resumes on startup"),
        (task(TaskState.WAITING_PLAN), "plan awaiting approval in Telegram"),
        (task(TaskState.WAITING_APPROVAL), "approval pending in Telegram"),
        (task(TaskState.RUNNING), None),
        (task(TaskState.COMPLETED), None),
    ]
    for t, expected in cases:
        assert subtitle(t, ZONE, moment) == expected, t.state
    assert readable_state(task(TaskState.PAUSED_BROWSER), ZONE) == "Paused (Chrome closed)"
    assert readable_state(task(TaskState.FAILED), ZONE) == "Canceled or failed"


@pytest.mark.parametrize(
    ("quantity", "text"),
    [
        (0, "0 tokens"),
        (950, "950 tokens"),
        (1000, "1K tokens"),
        (85_400, "85K tokens"),
        (999_499, "999K tokens"),
        (999_500, "1.0M tokens"),
        (1_234_567, "1.2M tokens"),
        (12_340_000, "12.3M tokens"),
    ],
)
def test_tokens_in_readable_format(quantity, text):
    assert format_tokens(quantity) == text
    assert format_tokens(quantity, unit=False) == text.removesuffix(" tokens")


def test_total_usage_and_breakdown():
    c = Consumption(entry=12_000, output=3_000, cache_read=1_200_000, cache_creation=5_000)
    assert c.total == 1_220_000 and c.text() == "1.2M tokens"
    assert c.breakdown() == "12K input, 3K output and 1.2M cache"


def test_text_bar_for_telegram():
    assert bar(0) == "▱" * 10
    assert bar(13) == "▰" + "▱" * 9
    assert bar(61) == "▰" * 6 + "▱" * 4
    assert bar(100) == "▰" * 10


# --- from test_progress ----------------------------------------------------

TOKEN = "test-token"


@pytest.fixture
async def environment(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    engine = Engine(config_tmp)
    await engine.start()
    engine.commands = run_command
    sim = Simulator(engine, Script(), print_out=False)
    yield engine, sim
    await engine.stop()


def create(number: str, subject: str, action: str, agent: str | None = None) -> Tool:
    return Tool(
        "TaskCreate",
        {"subject": subject, "description": subject, "activeForm": action},
        lambda e: {"task": {"id": number, "subject": subject}},
        agent=agent,
    )


def update(number: str, state: str) -> Tool:
    return Tool(
        "TaskUpdate",
        {"taskId": number, "status": state},
        lambda e: {"success": True, "taskId": number, "updatedFields": ["status"]},
    )


def test_created_step_id_in_any_format():
    assert _id_created({"task": {"id": "3", "subject": "x"}}) == "3"
    assert _id_created(json.dumps({"task": {"id": 4}})) == "4"
    assert _id_created("Task #7 created successfully") == "7"
    assert _id_created({"other": 1}) is None and _id_created(None) is None


def test_phrases_for_what_it_is_doing():
    assert describe_tool("Read", "/a/b/report.md") == "Reading report.md"
    assert describe_tool("Write", "/a/results/table.xlsx") == "Writing table.xlsx"
    assert describe_tool("WebSearch", "AI internships Berlin") == (
        "Searching the web for “AI internships Berlin”"
    )
    assert describe_tool("Agent", "researcher: listings") == ("Handing work to the researcher")
    assert describe_tool("mcp__aide__gmail_search", "") == "Working on your email"
    assert describe_tool("mcp__claude-in-chrome__navigate", "") == "Using your Chrome"
    assert describe_tool("Bash", "ls") == "Running a command"
    assert duration(40) == "40 s" and duration(12 * 60 + 5) == "12 min"
    assert duration(65 * 60) == "1 h 05 min" and duration(50 * 3600) == "2 days 2 h"


async def test_live_progress_of_a_task(environment):
    engine, sim = environment
    FakeClient.script = lambda p, o, r: [
        create("1", "Find listings", "Finding listings"),
        create("2", "Compare languages", "Comparing languages"),
        create("3", "Prepare the table", "Preparing the table"),
        # A subagent can create its own list: it does not count in the task plan.
        create("1", "Researcher's internal step", "Researching", agent="researcher"),
        update("1", "in_progress"),
        update("1", "completed"),
        update("2", "in_progress"),
        Wait(1.5),
        update("2", "completed"),
        update("3", "completed"),
        End("<delivery>Hecho.</delivery>"),
    ]
    send = asyncio.create_task(sim.send("Find internship listings"))
    task_id = None
    p = None
    for _ in range(100):
        await asyncio.sleep(0.05)
        task_id = await engine.db.value("SELECT MAX(id) FROM tasks")
        if task_id is None:
            continue
        p = await progress(engine.tasks, await engine.tasks.require(task_id))
        if p.facts == 1 and p.now == "Comparing languages":
            break
    assert p is not None and (p.facts, p.total) == (1, 3), p
    assert [step.subject for step in p.steps] == [
        "Find listings",
        "Compare languages",
        "Prepare the table",
    ]
    assert [step.readable_state for step in p.steps] == ["done", "in progress", "pending"]
    assert p.percentage == 33 and p.elapsed() is not None

    # Midway through the task, /tarea N and the panel show the same summary.
    await engine.receive(
        Entry(
            "command",
            SIMULATED_USER,
            text=f"/task {task_id}",
            command="task",
            arguments=str(task_id),
        )
    )
    text = sim.channel.texts()[-1]
    assert f"Task {task_id} · In progress" in text and "1 of 3 steps done" in text
    assert "Comparing languages." in text and "Running for " in text
    assert "✓ Find listings\n▸ Compare languages\n○ Prepare the table" in text
    panel = Panel(engine, token=TOKEN)
    transport = httpx.ASGITransport(app=panel.app)
    async with httpx.AsyncClient(transport=transport, base_url=panel.base) as client:
        await client.get(f"/enter?t={TOKEN}&go=/")
        start = (await client.get("/")).text
        assert "1 of 3 steps" in start and "Comparing languages" in start
        assert "data-since=" in start  # the elapsed time runs in the browser
        detail = (await client.get(f"/task/{task_id}")).text
        assert "Prepare the table" in detail and "in progress" in detail
        before = await panel.views.version()

    await send
    final = await engine.tasks.require(task_id)
    assert final.state == TaskState.COMPLETED
    p = await progress(engine.tasks, final)
    assert (p.facts, p.total) == (3, 3) and p.now is None
    assert await panel.views.version() != before  # the live update notices it


async def test_active_subagents_and_phrase_per_state(environment):
    engine, _ = environment
    task = await engine.tasks.create("Research this", state=TaskState.RECEIVED)
    for state in (TaskState.IN_QUEUE, TaskState.RUNNING):
        await engine.tasks.change_state(task.id, state)
    await engine.tasks.event(task.id, "start_subagent", "researcher", {"agent_id": "a1"})
    await engine.tasks.event(task.id, "start_subagent", "reader", {"agent_id": "a2"})
    await engine.tasks.event(task.id, "tool", "WebSearch: flats Berlin", {"tool": "WebSearch"})
    p = await progress(engine.tasks, await engine.tasks.require(task.id))
    assert p.subagents == ["reader", "researcher"]
    assert p.now == "Searching the web for “flats Berlin”"  # without steps, the last tool
    await engine.tasks.event(task.id, "end_subagent", "reader", {"agent_id": "a2"})
    p = await progress(engine.tasks, await engine.tasks.require(task.id))
    assert p.subagents == ["researcher"]
    await engine.tasks.change_state(task.id, TaskState.WAITING_APPROVAL)
    p = await progress(engine.tasks, await engine.tasks.require(task.id))
    assert p.now == "Waiting for your approval in Telegram"
    # Paused or finished, nothing is working.
    await engine.tasks.change_state(task.id, TaskState.PAUSED_BROWSER)
    p = await progress(engine.tasks, await engine.tasks.require(task.id))
    assert p.subagents == [] and p.now is None


async def test_a_new_session_replaces_the_step_list(environment):
    engine, _ = environment
    task = await engine.tasks.create("Something", state=TaskState.RUNNING)
    for session in ("s1", "s2"):
        await log_tool(
            engine.tasks,
            task.id,
            session,
            "TaskCreate",
            {"subject": f"step from {session}"},
            {"task": {"id": "1"}},
        )
    await log_tool(
        engine.tasks, task.id, "s2", "TaskUpdate", {"taskId": "1", "status": "deleted"}, {}
    )
    await log_tool(
        engine.tasks, task.id, "s2", "TaskCreate", {"subject": "another"}, {"task": {"id": "2"}}
    )
    assert [p["subject"] for p in await engine.tasks.steps(task.id)] == ["another"]


async def test_status_groups_and_explains_the_pauses(environment):
    engine, sim = environment
    in_progress = await engine.tasks.create("Write the report", state=TaskState.RUNNING)
    await engine.tasks.update(in_progress.id, title="Report")
    queue = await engine.tasks.create("Another one", state=TaskState.IN_QUEUE)
    await engine.tasks.update(queue.id, title="Another one")
    plan = await engine.tasks.create("Big one", state=TaskState.WAITING_PLAN)
    await engine.tasks.update(plan.id, title="Big one")
    chrome = await engine.tasks.create("Web", state=TaskState.PAUSED_BROWSER)
    await engine.tasks.update(chrome.id, title="Web", pause_reason="Chrome no disponible")
    await engine.receive(Entry("command", SIMULATED_USER, text="/status", command="status"))
    text = sim.channel.texts()[-1]
    assert "In progress\n" in text and f"{in_progress.id}. Report" in text
    assert f"{queue.id}. Another one (queued)" in text
    assert "Waiting for you\n" in text
    assert f"{plan.id}. Big one (plan awaiting approval in Telegram)" in text
    assert f"Paused\n{chrome.id}. Web (Chrome closed)" in text
    assert "USD" not in text


async def test_cap_notice_without_dollars(environment):
    engine, sim = environment
    FakeClient.script = lambda p, o, r: [End(subtype="error_max_budget_usd", is_error=True)]
    [task_id] = await sim.send("Do something long")  # the simulator answers “no” to the cap
    transitions = [t["until"] for t in await engine.tasks.transitions(task_id)]
    assert TaskState.PAUSED_CAP.value in transitions
    notice = next(t for t in sim.channel.texts() if "usage cap" in t)
    assert "tokens" in notice and "USD" not in notice and "cost" not in notice


def test_the_scheduling_tools_have_their_own_text():
    for tool in ("schedule_task", "schedule_script"):
        assert describe_tool(f"mcp__aide__{tool}") == "Setting up a scheduled task"
