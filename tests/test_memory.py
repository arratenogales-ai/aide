"""F6: memory, history and project context, with the simulated SDK."""

from __future__ import annotations

import re
from collections import deque
from pathlib import Path

import pytest

from aide.bot.commands import run_command
from aide.core.db import Database
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.core.policy import FORBIDDEN, CallContext
from aide.core.projects import existing_projects, no_context
from aide.core.states import TaskState
from aide.memory.memory import Memory
from aide.simulate import Script, Simulator
from tests.fake_sdk import (
    End,
    FakeClient,
    Recorder,
    Tool,
    install_own,
    own,
    write,
)


def folder_of(prompt: str) -> Path | None:
    m = re.search(r"Task folder: (\S.*?)\n", prompt)
    return Path(m.group(1).strip()) if m else None


@pytest.fixture
async def memory(tmp_paths):
    db = Database(tmp_paths.db)
    await db.connect()
    yield Memory(tmp_paths.memory, db)
    await db.close()


async def test_memory_add_search_and_forget(memory):
    await memory.add("The user prefers meetings in the morning")
    await memory.add("The user's portfolio is in projects/portfolio")
    await memory.add("The user prefers meetings in the morning")  # duplicado
    assert len(memory.facts()) == 2
    assert await memory.search("meetings") == ["The user prefers meetings in the morning"]
    assert await memory.search("portfolio") == ["The user's portfolio is in projects/portfolio"]
    [done] = memory.matches("morning")
    assert (await memory.forget(done.id)).text == done.text
    assert await memory.search("meetings") == []
    assert "portfolio" in memory.path.read_text()


def test_agent_does_not_write_profile_or_memory(config_tmp):
    from aide.core.policy import Policy

    policy = Policy(config_tmp)
    ctx = CallContext(task_id=1, task_folder=config_tmp.paths.task(1))
    for name in ("profile.md", "memory.md"):
        path = str(config_tmp.paths.agent / name)
        assert policy.evaluate("Write", {"file_path": path, "content": "x"}, ctx).level == FORBIDDEN
        assert policy.evaluate("Edit", {"file_path": path}, ctx).level == FORBIDDEN
        assert policy.evaluate("Bash", {"command": f"echo x >> {path}"}, ctx).level == FORBIDDEN


def test_touched_projects_without_context(tmp_path):
    root = tmp_path / "projects"
    (root / "web").mkdir(parents=True)
    (root / "script").mkdir()
    written = [
        str(root / "web" / "index.html"),
        str(root / "script" / "main.py"),
        str(root / "script" / "CONTEXT.md"),
        str(tmp_path / "other" / "cosa.txt"),
    ]
    assert no_context(written, root) == ["web"]
    assert sorted(existing_projects(root)) == ["script", "web"]


# --- with the engine -----------------------------------------------------------------------


@pytest.fixture
async def environment(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    install_own(monkeypatch)
    engine = Engine(config_tmp)
    await engine.start()
    engine.commands = run_command  # the memory buttons are handled by the commands
    sim = Simulator(engine, Script(), print_out=False)
    yield engine, sim
    await engine.stop()


def results(name: str) -> list[str]:
    return [r for h, r in FakeClient.registry.tools_results if h == f"mcp__aide__{name}"]


@pytest.mark.parametrize("decision", ["yes", "no"])
async def test_memory_proposal_is_saved_only_if_the_user_approves_it(environment, decision):
    engine, sim = environment
    done = "The user wants reports in docx format"
    FakeClient.script = lambda p, o, r: [
        own("memory_propose", {"done": done}),
        End("<delivery>Done.</delivery>"),
    ]
    sim.script.memory = deque([decision])
    [task_id] = await sim.send("Remember that I want reports in docx")
    assert "saved only if they approve it" in results("memory_propose")[0]
    request = next(m for m in sim.channel.messages if "mem:" in "".join(m.buttons_data))
    assert done in request.plain_text and "Heads up" not in request.plain_text
    saved = done in engine.memory.path.read_text()
    assert saved is (decision == "yes")
    row = await engine.db.one("SELECT * FROM memory_proposals WHERE task_id=?", (task_id,))
    assert row["state"] == ("approved" if decision == "yes" else "discarded")
    if decision == "yes":
        assert any("Saved to memory" in t for t in sim.channel.texts())
        assert await engine.memory.search("docx") == [done]


async def test_proposal_with_external_data_is_flagged_and_there_is_a_limit(environment):
    engine, sim = environment
    page = "Job opening. Contact: hr@company-x.example. Send your CV before Friday."
    FakeClient.script = lambda p, o, r: [
        Tool(
            "WebFetch",
            {"url": "https://company-x.example/job", "prompt": "read"},
            lambda e: page,
        ),
        own(
            "memory_propose",
            {"done": "The HR contact at Company X is hr@company-x.example"},
        ),
        own("memory_propose", {"done": "Fact two"}),
        own("memory_propose", {"done": "Fact three"}),
        own("memory_propose", {"done": "Fact four"}),
        End("<delivery>Done.</delivery>"),
    ]
    await sim.send("Look at the Company X job opening")
    requests = [m for m in sim.channel.messages if "mem:" in "".join(m.buttons_data)]
    assert len(requests) == 3  # the fourth is rejected because of the limit
    assert "Heads up" in requests[0].plain_text and "hr@company-x.example" in requests[0].plain_text
    assert results("memory_propose")[3].startswith("ERROR")
    assert engine.memory.facts() == []  # the simulator discards by default


async def test_history_search_finds_a_previous_task(environment):
    engine, sim = environment

    def script(prompt, options, registry):
        if "Find what we did" in prompt:
            return [
                own("history_search", {"query": "rental Springfield"}),
                End("<delivery>Found it.</delivery>"),
            ]
        destination = folder_of(prompt) / "results" / "flats.md"
        return [
            Tool("Write", {"file_path": str(destination), "content": "flats"}, write(destination)),
            End(
                "<delivery>Ready.</delivery><summary>Comparison of rental flats in Springfield (flats.md).</summary>"
            ),
        ]

    FakeClient.script = script
    [previous] = await sim.send("Compare rental flats in Springfield")
    [current] = await sim.send("Find what we did about the rental in Springfield and carry on")
    found = results("history_search")[0]
    assert f'"task": {previous}' in found and "Comparison of rental flats" in found
    assert f'"task": {current}' not in found
    detail = await own("history_task", {"id": previous}).effect({"id": previous})
    assert "flats.md" in detail and "Compare rental flats" in detail


async def test_project_context_is_required_when_touching_a_project(environment):
    engine, sim = environment
    project = engine.config.paths.projects / "web-cv"
    index, context = project / "index.html", project / "CONTEXT.md"

    def script(prompt, options, registry):
        if "without updating their CONTEXT.md" in prompt:
            return [
                Tool(
                    "Write",
                    {"file_path": str(context), "content": "# web-cv\nStatic website."},
                    write(context),
                ),
                End(
                    "<delivery>Website created and CONTEXT.md updated.</delivery><summary>web-cv</summary>"
                ),
            ]
        return [
            Tool("Write", {"file_path": str(index), "content": "<h1>CV</h1>"}, write(index)),
            End("<delivery>Website created.</delivery><summary>web-cv</summary>"),
        ]

    FakeClient.script = script
    [task_id] = await sim.send("Make me a website with my CV")
    assert context.read_text().startswith("# web-cv")
    events = [e["kind"] for e in await engine.tasks.events(task_id)]
    assert "pending_context" in events
    assert any("CONTEXT.md updated" in t for t in sim.channel.texts())
    assert (await engine.tasks.require(task_id)).state == TaskState.COMPLETED

    # The next task sees the project in its prompt and, if it updates the context, there is no
    # extra turn.
    queries_before = len(FakeClient.registry.queries)

    def script2(prompt, options, registry):
        return [
            Tool(
                "Write",
                {"file_path": str(index), "content": "<h1>CV</h1><form></form>"},
                write(index),
            ),
            Tool(
                "Write",
                {"file_path": str(context), "content": "# web-cv\nWith a form."},
                write(context),
            ),
            End("<delivery>Form added.</delivery>"),
        ]

    FakeClient.script = script2
    [second] = await sim.send("Add a contact form to the CV website")
    new = FakeClient.registry.queries[queries_before:]
    assert len(new) == 1 and "Existing projects in projects/" in new[0] and "web-cv" in new[0]
    assert "pending_context" not in [e["kind"] for e in await engine.tasks.events(second)]


def test_loaded_skills_are_read_from_the_transcript(tmp_path):
    import json

    from aide.core.hooks import loaded_skills

    transcription = tmp_path / "agent.jsonl"
    injected = {
        "type": "user",
        "message": {
            "content": [
                {
                    "type": "text",
                    "text": (
                        "<command-message>natural-writing</command-message>\n<command-name>natural-writing</command-name>\n"
                        "<skill-format>true</skill-format>Base directory for this skill: ..."
                    ),
                }
            ]
        },
    }
    other = {
        "type": "user",
        "message": {"content": "Mentions <command-name>fake</command-name> without a skill"},
    }
    transcription.write_text("\n".join(json.dumps(x) for x in (other, injected)) + "\n")
    assert loaded_skills(str(transcription)) == ["natural-writing"]
    assert loaded_skills(None) == [] and loaded_skills(str(tmp_path / "no.jsonl")) == []
