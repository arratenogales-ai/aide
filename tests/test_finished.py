"""Final results folder: “Finished Tasks/YYYY-MM-DD short name/”."""

from __future__ import annotations

import os
import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from aide.common.paths import FINISHED_FOLDER, Paths
from aide.core import finished
from aide.core.engine import Engine
from aide.core.finished import brief_name, export_task, free_folder
from aide.core.orchestrator import AgentSession
from aide.core.states import TaskState
from aide.simulate import Script, Simulator
from tests.fake_sdk import End, FakeClient, Recorder, Tool, write

ZONE = "Europe/Berlin"


@pytest.fixture
async def environment(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    engine = Engine(config_tmp)
    await engine.start()
    sim = Simulator(engine, Script(), print_out=False)
    yield engine, sim
    await engine.stop()


@pytest.mark.parametrize(
    ("title", "name"),
    [
        ("Apartment rental prices in Riverton", "apartment rental prices riverton"),
        (
            "Research the founding of the museum and its first director",
            "founding museum first director",
        ),
        ("Café and crème brûlée recipes", "cafe creme brulee recipes"),
        ("Email for the academy!", "email academy"),
        ("Report: Q3/2026 (sales €) — North", "report q3 2026 sales"),
        ("Create a file hello.txt in the results folder", "file hello results folder"),
        ("Create tips.md and phrases.md in results", "tips phrases results"),
        # Titles in Spanish still give a clean name.
        ("Precios de alquiler de pisos en el centro", "precios alquiler pisos centro"),
        ("Hello", "hello task"),
        ("", "task task"),
    ],
)
def test_short_name(title, name):
    brief = brief_name(title)
    assert brief == name
    words = brief.split()
    assert 2 <= len(words) <= 4 and re.fullmatch(r"[a-z0-9 ]+", brief)


def test_if_the_name_exists_it_is_numbered(tmp_path):
    assert free_folder(tmp_path, "2026-10-20 report") == tmp_path / "2026-10-20 report"
    (tmp_path / "2026-10-20 report").mkdir()
    assert free_folder(tmp_path, "2026-10-20 report") == tmp_path / "2026-10-20 report 2"
    (tmp_path / "2026-10-20 report 2").mkdir()
    assert free_folder(tmp_path, "2026-10-20 report") == tmp_path / "2026-10-20 report 3"


def test_with_separate_data_the_folder_goes_with_that_data(tmp_path):
    assert Paths(root=tmp_path).finished == tmp_path / FINISHED_FOLDER
    data = tmp_path / "datos_prueba"
    assert Paths(root=tmp_path, data_dir=data).finished == data / FINISHED_FOLDER


async def test_export_copies_the_results_without_summary(environment, tmp_path):
    engine, _ = environment
    paths = engine.config.paths
    task = await engine.tasks.create("Find internship offers", state=TaskState.COMPLETED)
    await engine.tasks.update(task.id, title="AI and data internship offers in Riverton")
    internal = paths.create_task_folder(task.id)
    (internal / "results" / "offers.md").write_text("| Company |")
    (internal / "results" / "appendix").mkdir()
    (internal / "results" / "appendix" / "table.csv").write_text("a,b")
    (internal / "results" / ".hidden").write_text("no")
    secret = tmp_path / "secret.txt"
    secret.write_text("outside the task")
    os.symlink(secret, internal / "results" / "link.txt")
    (internal / "data" / "raw.json").write_text("{}")
    task = await engine.tasks.require(task.id)

    moment = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    destination = await export_task(engine.tasks, paths, task, ZONE, moment)
    assert destination == paths.finished / "2026-10-20 ai data internship offers"
    copied = sorted(str(p.relative_to(destination)) for p in destination.rglob("*") if p.is_file())
    # Only the deliverables: no SUMMARY.md (it is in the panel), no hidden files or symlinks.
    assert copied == ["appendix/table.csv", "offers.md"]
    task = await engine.tasks.require(task.id)
    assert task.final_folder == str(destination)
    assert await engine.tasks.events(task.id, ["final_folder"])

    # If the task is reopened and completed again, the same folder is updated.
    (internal / "results" / "offers.md").write_text("| Company | Link |")
    (internal / "results" / "new.txt").write_text("new")
    other = await export_task(engine.tasks, paths, task, ZONE, moment)
    assert other == destination
    assert (destination / "offers.md").read_text() == "| Company | Link |"
    assert (destination / "new.txt").exists() and not (destination / "SUMMARY.md").exists()
    assert len(list(paths.finished.iterdir())) == 1


async def test_a_second_task_with_the_same_name_is_numbered(environment):
    engine, _ = environment
    moment = datetime(2026, 10, 20, 10, 0, tzinfo=UTC)
    destinations = []
    for _ in range(2):
        t = await engine.tasks.create("Sales report", state=TaskState.COMPLETED)
        await engine.tasks.update(t.id, title="Sales report")
        t = await engine.tasks.require(t.id)
        results = engine.config.paths.task(t.id) / "results"
        results.mkdir(parents=True, exist_ok=True)
        (results / "sales.xlsx").write_bytes(b"x")
        destinations.append(
            await export_task(engine.tasks, engine.config.paths, t, ZONE, moment=moment)
        )
    assert [d.name for d in destinations] == [
        "2026-10-20 sales report",
        "2026-10-20 sales report 2",
    ]


async def test_a_task_without_deliverables_creates_no_folder(environment):
    """With no delivered file there is nothing to save: no empty folder."""
    engine, _ = environment
    t = await engine.tasks.create(
        "Which offer was the one for the kids?", state=TaskState.COMPLETED
    )
    t = await engine.tasks.require(t.id)
    assert await export_task(engine.tasks, engine.config.paths, t, ZONE) is None
    assert not engine.config.paths.finished.exists() or not any(
        engine.config.paths.finished.iterdir()
    )
    assert (await engine.tasks.require(t.id)).final_folder is None


async def test_quick_answers_do_not_create_a_folder(environment):
    engine, _ = environment
    t = await engine.tasks.create("What is 7 times 8?", kind="quick", state=TaskState.COMPLETED)
    assert await export_task(engine.tasks, engine.config.paths, t, ZONE) is None
    assert not engine.config.paths.finished.exists()


async def test_on_completion_the_delivery_names_the_folder(environment):
    engine, sim = environment

    def script(prompt, options, registry):
        folder = Path(re.search(r"Task folder: (\S.*?)\n", prompt).group(1).strip())
        destination = folder / "results" / "tips.txt"
        return [
            Tool(
                "Write",
                {"file_path": str(destination), "content": "1. Practice"},
                write(destination),
            ),
            End(
                "<delivery>Here are the tips.</delivery><summary>Tips for learning German.</summary>"
            ),
        ]

    FakeClient.script = script
    [task_id] = await sim.send("Give me tips for learning German")
    task = await engine.tasks.require(task_id)
    assert task.state == TaskState.COMPLETED and task.final_folder
    final = Path(task.final_folder)
    assert final.parent == engine.config.paths.finished
    assert (final / "tips.txt").read_text() == "1. Practice"
    assert not (final / "SUMMARY.md").exists()
    # The internal folder stays intact (resuming and the sandbox depend on it).
    assert (engine.config.paths.task(task_id) / "results" / "tips.txt").exists()
    assert not (final / "progress.md").exists()
    delivery = next(t for t in sim.channel.texts() if f"Task {task_id} done" in t)
    assert f"Saved in {FINISHED_FOLDER}/{final.name}." in delivery


async def test_if_the_copy_fails_the_delivery_continues(environment, monkeypatch):
    engine, sim = environment

    def broken(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(finished, "_copy", broken)
    FakeClient.script = lambda p, o, r: [End("<delivery>Done.</delivery>")]
    [task_id] = await sim.send("Do something small")
    task = await engine.tasks.require(task_id)
    assert task.state == TaskState.COMPLETED and task.final_folder is None
    delivery = next(t for t in sim.channel.texts() if f"Task {task_id} done" in t)
    assert "Saved in" not in delivery and await engine.usage.task_text(task_id) in delivery


async def test_export_already_completed_tasks_from_the_terminal(tmp_paths, monkeypatch, capsys):
    from aide.common.config import Config
    from aide.core.db import Database
    from aide.core.tasks import TaskRepository

    monkeypatch.setenv("AIDE_ROOT", str(tmp_paths.root))
    monkeypatch.delenv("AIDE_DATA", raising=False)
    Config.load(tmp_paths)
    db = Database(tmp_paths.db)
    await db.connect()
    tasks = TaskRepository(db, tmp_paths)
    done = await tasks.create(
        "Research Northfield College", title="Research on Northfield College history"
    )
    for state in (TaskState.IN_QUEUE, TaskState.RUNNING, TaskState.COMPLETED):
        await tasks.change_state(done.id, state)
    await db.run(
        "UPDATE transitions SET hour='2026-10-19T16:24:39+00:00' WHERE task_id=? AND until='completed'",
        (done.id,),
    )
    tmp_paths.create_task_folder(done.id)
    (tmp_paths.task(done.id) / "results" / "college.md").write_text("1855")
    quick = await tasks.create("What is 7 times 8?", kind="quick", state=TaskState.COMPLETED)
    await db.close()

    assert await finished._export_completed([done.id, quick.id]) == 1
    output = capsys.readouterr().out
    destination = tmp_paths.finished / "2026-10-19 northfield college history"
    assert str(destination) in output and "not a completed task" in output
    assert (destination / "college.md").read_text() == "1855" and not (
        destination / "SUMMARY.md"
    ).exists()
    assert len(list(tmp_paths.finished.iterdir())) == 1
