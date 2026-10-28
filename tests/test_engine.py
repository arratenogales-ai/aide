"""F1: core with the simulated SDK (states, queue, caps, approvals, limits, plan)."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from pathlib import Path

import pytest

from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.core.states import TaskState
from aide.simulate import Script, Simulator
from tests.fake_sdk import End, FakeClient, Recorder, Text, Tool, Wait, write


def folder_of(prompt: str) -> Path | None:
    m = re.search(r"Task folder: (\S.*?)\n", prompt)
    return Path(m.group(1).strip()) if m else None


@pytest.fixture
async def environment(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    engine = Engine(config_tmp)
    await engine.start()
    sim = Simulator(engine, Script(), print_out=False)
    yield engine, sim
    await engine.stop()


def simple_script(name: str = "hello.txt"):
    def script(prompt, options, registry):
        folder = folder_of(prompt)
        destination = folder / "results" / name
        return [
            Text("Creating the file."),
            Tool(
                "Write",
                {"file_path": str(destination), "content": "hello from Alex"},
                write(destination),
            ),
            End(f"<delivery>I created {name}.</delivery><summary>File {name} created.</summary>"),
        ]

    return script


async def test_simple_task_completes_file_log_and_usage(environment):
    engine, sim = environment
    FakeClient.script = simple_script()
    [task_id] = await sim.send("Create a file hello.txt")
    task = await engine.tasks.require(task_id)
    assert task.state == TaskState.COMPLETED
    assert (
        engine.config.paths.task(task_id) / "results" / "hello.txt"
    ).read_text() == "hello from Alex"
    assert any(a["kind"] == "result" for a in await engine.tasks.files(task_id))
    assert task.usd_used > 0 and task.turns_used == 2
    assert await engine.usage.by_task(task_id)
    transitions = [t["until"] for t in await engine.tasks.transitions(task_id)]
    assert transitions == ["received", "in_queue", "running", "completed"]
    texts = sim.channel.texts()
    delivery = next(t for t in texts if f"Task {task_id} done" in t)
    # Token usage (no dollars) and the final folder where the result was copied.
    assert "Usage: " in delivery and "tokens" in delivery and "USD" not in delivery
    assert "Saved in Finished Tasks/" in delivery
    assert any(m.kind == "file" and m.path.name == "hello.txt" for m in sim.channel.messages)
    progress = (engine.config.paths.task(task_id) / "progress.md").read_text()
    assert "Delivery sent" in progress
    assert task.summary.startswith("File hello.txt")
    assert await engine.tasks.search_history("hello")


async def test_cap_reached_and_continuation_with_higher_cap(environment):
    engine, sim = environment
    calls = {"n": 0}

    def script(prompt, options, registry):
        calls["n"] += 1
        if calls["n"] == 1:
            return [End(subtype="error_max_turns", is_error=True, turns=20)]
        return (
            simple_script()(prompt, options, registry)
            if folder_of(prompt)
            else [End("<delivery>Finished after raising the cap.</delivery>")]
        )

    FakeClient.script = script
    sim.script.cap = deque(["more"])
    [task_id] = await sim.send("Do something long")
    task = await engine.tasks.require(task_id)
    transitions = [t["until"] for t in await engine.tasks.transitions(task_id)]
    assert "paused_cap" in transitions
    assert task.state == TaskState.COMPLETED
    cap = engine.config.settings.caps.small.turns
    assert task.turn_cap == max(cap * 2, 20 + cap)  # ampliado
    assert any("cap" in t.lower() for t in sim.channel.texts())
    # The second connection resumes the same session.
    assert FakeClient.registry.options[-1].resume is not None


@pytest.mark.parametrize("reply,executed", [("yes", True), ("no", False)])
async def test_level_2_approval(environment, reply, executed):
    engine, sim = environment
    done = {"push": False}

    def effect(entry):
        done["push"] = True
        return "pushed"

    FakeClient.script = lambda p, o, r: [
        Tool("Bash", {"command": "git push origin main"}, effect),
        End("<delivery>Done.</delivery>"),
    ]
    sim.script.approve = deque([reply])
    [task_id] = await sim.send("Push the changes")
    assert done["push"] is executed
    rows = await engine.db.everything("SELECT * FROM approvals WHERE task_id=?", (task_id,))
    assert len(rows) == 1
    assert rows[0]["state"] == ("approved" if executed else "rejected")
    transitions = [t["until"] for t in await engine.tasks.transitions(task_id)]
    assert "waiting_approval" in transitions
    assert (await engine.tasks.require(task_id)).state == TaskState.COMPLETED
    request = next(m for m in sim.channel.messages if "apr:" in "".join(m.buttons_data))
    assert "git push origin main" in request.plain_text


async def test_forbidden_command_blocked_by_code(environment):
    engine, sim = environment
    executed = {"yes": False}
    FakeClient.script = lambda p, o, r: [
        Tool(
            "Bash", {"command": "sudo rm -rf /"}, lambda e: executed.__setitem__("yes", True) or "x"
        ),
        End("<delivery>I couldn't.</delivery>"),
    ]
    [task_id] = await sim.send("Delete everything")
    assert executed["yes"] is False
    assert FakeClient.registry.tools_results[0][1].startswith("DENEGADA")
    events = await engine.tasks.events(task_id, ["blocked"])
    assert events and "sudo" in events[0]["summary"]
    assert not await engine.db.everything("SELECT * FROM approvals")


async def test_plan_limit_pauses_with_reset_time(environment):
    engine, sim = environment
    reset = int(time.time()) + 3600
    FakeClient.script = lambda p, o, r: [
        End(
            "You've hit your session limit · resets 3:45pm",
            is_error=True,
            error_assistant="rate_limit",
            limit={"status": "rejected", "resets_at": reset, "rate_limit_type": "five_hour"},
        )
    ]
    [task_id] = await sim.send("Do something")
    task = await engine.tasks.require(task_id)
    assert task.state == TaskState.PAUSED_LIMIT
    assert task.pause_until is not None
    from aide.core.db import to_date

    assert abs(to_date(task.pause_until).timestamp() - reset) < 2
    assert await engine.db.value("SELECT COUNT(*) FROM limits WHERE state='rejected'") == 1


async def test_expired_login_pauses(environment):
    engine, sim = environment
    FakeClient.script = lambda p, o, r: [
        End(
            "Failed to authenticate: OAuth session expired and could not be refreshed",
            is_error=True,
            error_assistant="authentication_failed",
        )
    ]
    [task_id] = await sim.send("Do something")
    assert (await engine.tasks.require(task_id)).state == TaskState.PAUSED_LOGIN
    assert any("/login" in t for t in sim.channel.texts())


async def test_plan_with_change_and_approval(environment):
    engine, sim = environment

    def script(prompt, options, registry):
        if "PLAN PHASE" in prompt or "Redo the plan" in prompt:
            sources = "one source" if "Redo" in prompt else "three sources"
            return [End(f"<plan>Goal: report. Steps: research with {sources}.</plan>")]
        return [End("<delivery>Report done.</delivery>")]

    FakeClient.script = script
    sim.script.plan = deque(["change:just one source", "approve"])
    task = await engine.create_direct_task(
        "Research X", origin="test", size="large", needs_plan=True
    )
    await sim.wait_rest()
    task = await engine.tasks.require(task.id)
    assert task.state == TaskState.COMPLETED
    assert "one source" in task.plan
    queries = FakeClient.registry.queries
    assert any("just one source" in c for c in queries)
    assert any("has approved the plan" in c for c in queries)
    # During the plan phase the policy only allows reading.
    transitions = [t["until"] for t in await engine.tasks.transitions(task.id)]
    assert transitions.count("waiting_plan") == 2


async def test_plan_phase_cannot_write(environment):
    engine, sim = environment

    def script(prompt, options, registry):
        if "PLAN PHASE" in prompt:
            folder = folder_of(prompt)
            return [
                Tool(
                    "Write",
                    {"file_path": str(folder / "x.txt"), "content": "x"},
                    write(folder / "x.txt"),
                ),
                End("<plan>Plan.</plan>"),
            ]
        return [End("<delivery>Done.</delivery>")]

    FakeClient.script = script
    sim.script.plan = deque(["cancel"])
    task = await engine.create_direct_task("Do X", origin="test", needs_plan=True)
    await sim.wait_rest()
    assert not (engine.config.paths.task(task.id) / "x.txt").exists()
    assert (await engine.tasks.require(task.id)).state == TaskState.CANCELLED


async def test_mid_turn_message_is_incorporated(environment):
    engine, sim = environment
    FakeClient.script = lambda p, o, r: [Wait(1.0), End("<delivery>Done.</delivery>")]
    task = await engine.create_direct_task("Slow task", origin="test")
    for _ in range(40):
        run = engine.in_progress.get(task.id)
        if run and run.session and run.session.in_turn:
            break
        await asyncio.sleep(0.05)
    await engine.continue_task(task.id, "also add a summary")
    await sim.wait_rest()
    task = await engine.tasks.require(task.id)
    assert task.state == TaskState.COMPLETED
    assert any("also add a summary" in c for c in FakeClient.registry.queries)
    texts = [e["summary"] for e in await engine.tasks.events(task.id, ["agent_text"])]
    assert any("merged in" in t for t in texts)  # the message entered the current turn
    assert len([c for c in FakeClient.registry.options]) == 1  # without opening another session
    assert any("take that into account" in t for t in sim.channel.texts())


async def test_question_with_buttons(environment):
    engine, sim = environment
    replies = {}

    def effect(entry):
        replies.update(entry.get("answers") or {})
        return "respondido"

    question = {
        "question": "Format?",
        "header": "Format",
        "multiSelect": False,
        "options": [{"label": "Word", "description": ""}, {"label": "PDF", "description": ""}],
    }
    FakeClient.script = lambda p, o, r: [
        Tool("AskUserQuestion", {"questions": [question]}, effect),
        End("<delivery>Done.</delivery>"),
    ]
    sim.script.replies = deque(["2"])
    await sim.send("Write me a report")
    assert replies == {"Format?": "PDF"}


async def test_opus_limit_continues_with_sonnet(environment):
    engine, sim = environment
    state = {"n": 0}

    def script(prompt, options, registry):
        state["n"] += 1
        if state["n"] == 1:
            return [
                End(
                    "You've hit your Opus limit",
                    is_error=True,
                    error_assistant="rate_limit",
                    limit={
                        "status": "rejected",
                        "resets_at": int(time.time()) + 7200,
                        "rate_limit_type": "seven_day_opus",
                    },
                )
            ]
        return [End("<delivery>Done with Sonnet.</delivery>")]

    FakeClient.script = script
    # A medium task, which goes with Opus (small ones already go with Sonnet).
    task_id = (await engine.create_direct_task("Do something", size="medium")).id
    await sim.wait_rest()
    task = await engine.tasks.require(task_id)
    assert task.state == TaskState.COMPLETED
    assert FakeClient.registry.models[0] == "claude-opus-5-5"
    assert FakeClient.registry.models[-1] == "claude-sonnet-5-5"
    assert await engine.opus_limit_active()
    assert any("Opus limit" in t for t in sim.channel.texts())


async def test_new_task_does_not_inherit_the_folder_of_another_database(environment):
    engine, sim = environment
    old = engine.config.paths.task(1) / "results" / "old.txt"
    old.parent.mkdir(parents=True)
    old.write_text("from another database")
    FakeClient.script = simple_script()
    [task_id] = await sim.send("Create a file hello.txt")
    assert task_id == 1
    assert not (engine.config.paths.task(1) / "results" / "old.txt").exists()
    reserved = list((engine.config.paths.tasks / "_previous").glob("1-*"))
    assert len(reserved) == 1 and (reserved[0] / "results" / "old.txt").exists()
    assert not any(m.kind == "file" and m.path.name == "old.txt" for m in sim.channel.messages)


def test_separate_data_uses_separate_task_folders(tmp_path):
    from aide.common.paths import Paths

    with_data = Paths(root=tmp_path, data_dir=tmp_path / "datos_sim")
    assert with_data.task(3) == tmp_path / "agent" / "test_tasks" / "datos_sim" / "3"
    assert Paths(root=tmp_path).task(3) == tmp_path / "agent" / "tasks" / "3"
    assert with_data.memory == tmp_path / "datos_sim" / "memory.md"
    assert Paths(root=tmp_path).memory == tmp_path / "agent" / "memory.md"


async def test_cutting_the_turn_saves_the_interruption(environment):
    """The interruption goes with the engine's background tasks, which are kept until
    they finish (before, it was launched loose and asyncio could lose it halfway)."""
    from types import SimpleNamespace

    from aide.core.engine import TaskRun

    engine, _ = environment
    interrupted = asyncio.Event()

    async def interrupt():
        await asyncio.sleep(0.05)
        interrupted.set()

    session = SimpleNamespace(in_turn=True, interrupt=interrupt)
    engine.in_progress[99] = TaskRun(task_id=99, ses=None, session=session)
    try:
        before = set(engine._background)
        engine._cut_turn(99)
        await asyncio.sleep(1.02)
        assert [t for t in engine._background - before if not t.done()]  # saved while running
        await asyncio.wait_for(interrupted.wait(), 2)
        # If the turn already ended or the session changed, nothing is interrupted.
        interrupted.clear()
        session.in_turn = False
        engine._cut_turn(99)
        await asyncio.sleep(1.1)
        assert not interrupted.is_set()
        engine._cut_turn(None)
    finally:
        # Out before stopping the engine, whatever happens (otherwise stopping would wait
        # for it).
        del engine.in_progress[99]
