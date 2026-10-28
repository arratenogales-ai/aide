"""Script-type scheduled tasks: fixed project command, sandbox, no model and with approval."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from aide.bot.channel import Entry, FakeChannel
from aide.core import scripts
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.core.policy import CallContext, Policy
from aide.simulate import SIMULATED_USER
from tests.fake_sdk import FakeClient, Recorder

with_sandbox = pytest.mark.skipif(shutil.which("sandbox-exec") is None, reason="macOS only")


@pytest.fixture
def project(tmp_paths):
    folder = tmp_paths.projects / "alert"
    folder.mkdir(parents=True)
    (folder / "alert.py").write_text(
        "import sys\nprint(sys.argv[1] if len(sys.argv) > 1 else 'NO_RAIN')\n"
    )
    return folder


@pytest.fixture
async def engine(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    m = Engine(config_tmp, channel=FakeChannel(print_out=False))
    await m.start(workers=False)
    yield m
    await m.stop()


def test_only_an_interpreter_and_a_project_script(tmp_paths, project, tmp_path):
    c = scripts.validate(tmp_paths, "alert", "python3 alert.py --threshold 50")
    assert c.argv == ("python3", "alert.py", "--threshold", "50")
    assert c.text() == "python3 alert.py --threshold 50"
    bad = [
        ("alert", "python3 alert.py; rm -rf /"),
        ("alert", "python3 alert.py | mail x"),
        ("alert", "python3 alert.py $(whoami)"),
        ("alert", "curl https://example.com"),
        ("alert", "python3"),
        ("alert", "python3 ../other/alert.py"),
        ("alert", "python3 /etc/passwd"),
        ("alert", "python3 alert.py ../../.env"),
        ("../alert", "python3 alert.py"),
        ("does-not-exist", "python3 alert.py"),
    ]
    for bad_project, command in bad:
        with pytest.raises(ValueError):
            scripts.validate(tmp_paths, bad_project, command)
    # A project that is a link to another folder is not valid either.
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "x.py").write_text("print(1)")
    (tmp_paths.projects / "link").symlink_to(outside)
    with pytest.raises(ValueError):
        scripts.validate(tmp_paths, "link", "python3 x.py")


def test_what_is_sent_to_the_user():
    c = scripts.Command("alert", ("python3", "alert.py"))
    ok = scripts.ScriptResult(0, "NO_RAIN\n", "", 0.3)
    assert scripts.message_for_user(1, c, "NO_RAIN", ok) is None
    rain = scripts.ScriptResult(0, "☔ Rain today at 14:00\n", "", 0.3)
    assert scripts.message_for_user(1, c, "NO_RAIN", rain) == "☔ Rain today at 14:00"
    failure = scripts.ScriptResult(2, "", "Traceback...\nERROR: the API is not responding\n", 0.3)
    text = scripts.message_for_user(1, c, "NO_RAIN", failure)
    assert "failed (exit code 2)" in text and "the API is not responding" in text
    afternoon = scripts.ScriptResult(None, "", "", 120.0, exhausted=True)
    assert "more than 2 minutes" in scripts.message_for_user(1, c, "NO_RAIN", afternoon)


@with_sandbox
async def test_it_runs_in_the_sandbox(tmp_paths, project, monkeypatch):
    tmp_paths.env.write_text("TELEGRAM_BOT_TOKEN=secret\n")
    (project / "check.py").write_text(
        "import pathlib, sys\n"
        "accion = sys.argv[1]\n"
        "if accion == 'inside': pathlib.Path('output.txt').write_text('ok'); print('DONE')\n"
        "if accion == 'outside': pathlib.Path(sys.argv[2]).write_text('x')\n"
        "if accion == 'secret': print(pathlib.Path(sys.argv[2]).read_text())\n"
        "if accion == 'slow':\n    import time; time.sleep(5)\n"
    )
    inside = await scripts.run(
        tmp_paths,
        scripts.validate(tmp_paths, "alert", ["python3", "check.py", "inside"]),
        "Europe/Berlin",
    )
    assert inside.ok and inside.output.strip() == "DONE" and (project / "output.txt").exists()
    # Writing outside the project (and the temp files) or reading secrets: the sandbox prevents
    # it.
    outside = Path.home() / ".aide-sandbox-test.txt"
    try:
        r = await scripts.run(
            tmp_paths,
            scripts.Command("alert", ("python3", "check.py", "outside", str(outside))),
            "Europe/Berlin",
        )
        assert not r.ok and not outside.exists()
        assert "Operation not permitted" in r.error or "PermissionError" in r.error
    finally:
        outside.unlink(missing_ok=True)
    r = await scripts.run(
        tmp_paths,
        scripts.Command("alert", ("python3", "check.py", "secret", str(tmp_paths.env))),
        "Europe/Berlin",
    )
    assert not r.ok and "secret" not in r.output
    # Maximum time.
    monkeypatch.setattr(scripts, "MAX_TIME", 1)
    r = await scripts.run(
        tmp_paths, scripts.validate(tmp_paths, "alert", "python3 check.py slow"), "Europe/Berlin"
    )
    assert r.exhausted and not r.ok


@with_sandbox
async def test_a_script_does_not_write_in_its_projects_git(tmp_paths):
    """A script scheduled task writes in its project folder, but not in its git repository
    nor in what changes how git works."""
    project = tmp_paths.projects / "web"
    project.mkdir(parents=True, exist_ok=True)
    (project / "check.py").write_text(
        "import pathlib, sys\n"
        "ruta = pathlib.Path(sys.argv[1])\n"
        "ruta.parent.mkdir(parents=True, exist_ok=True)\n"
        "ruta.write_text('x')\n"
    )

    async def write(relative: str):
        command = scripts.Command(project.name, ("python3", "check.py", relative))
        return await scripts.run(tmp_paths, command, "Europe/Berlin")

    assert (await write("data/note.txt")).ok and (project / "data" / "note.txt").exists()
    r = await write(".git/HEAD")
    assert not r.ok and not (project / ".git" / "HEAD").exists()
    r = await write(".gitattributes")
    assert not r.ok and not (project / ".gitattributes").exists()


async def schedule(
    engine: Engine, command: list[str], silence: str = "NO_RAIN", pending: int = 0
) -> int:
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds")
    return await engine.db.run(
        "INSERT INTO scheduled_tasks(description, timing, policy, active, created, next_run, timing_text, "
        "kind, project, command, silence, pending) VALUES ('Alert', ?, 'run_if_recent', ?, "
        "?, ?, 'every day at 07:30', 'script', 'alert', ?, ?, ?)",
        (
            json.dumps({"kind": "daily", "hour": "07:30"}),
            0 if pending else 1,
            past,
            past,
            json.dumps(command),
            silence,
            pending,
        ),
    )


@with_sandbox
async def test_the_script_scheduled_task_does_not_use_the_model(engine, project):
    quiet = await schedule(engine, ["python3", "alert.py"])
    talkative = await schedule(engine, ["python3", "alert.py", "Rain today"])
    tasks_before = await engine.db.value("SELECT COUNT(*) FROM tasks")
    assert await engine.check_scheduled_tasks() == []  # no task is created
    for _ in range(100):
        if len(await engine.tasks.db.everything("SELECT * FROM events WHERE kind='script'")) == 2:
            break
        await __import__("asyncio").sleep(0.05)
    events = await engine.db.everything("SELECT * FROM events WHERE kind='script' ORDER BY id")
    assert len(events) == 2
    assert await engine.db.value("SELECT COUNT(*) FROM tasks") == tasks_before
    assert await engine.db.value("SELECT COUNT(*) FROM usage") == 0
    assert FakeClient.registry.options == []  # no model session
    texts = engine.channel.texts()
    assert "Rain today" in texts and not any("NO_RAIN" in t for t in texts)
    for pid in (quiet, talkative):
        row = await engine.db.one("SELECT * FROM scheduled_tasks WHERE id=?", (pid,))
        assert row["last_run"] and row["next_run"] > datetime.now(UTC).isoformat()


async def test_signup_from_the_terminal_is_approved_in_telegram(engine, project):
    from aide.bot.commands import run_command

    engine.commands = run_command
    pid = await schedule(engine, ["python3", "alert.py"], pending=1)
    await engine.check_pending_scripts()
    for _ in range(100):
        pending = await engine.approvals.pending()
        if pending:
            break
        await __import__("asyncio").sleep(0.05)
    [approval] = pending
    assert "Exact command: python3 alert.py" in approval["detail"]
    assert "NO_RAIN" in approval["detail"]
    assert (await engine.db.one("SELECT active FROM scheduled_tasks WHERE id=?", (pid,)))[
        "active"
    ] == 0
    await engine.receive(Entry("button", SIMULATED_USER, button_datum=f"apr:{approval['id']}:yes"))
    for _ in range(100):
        row = await engine.db.one("SELECT * FROM scheduled_tasks WHERE id=?", (pid,))
        if row["active"]:
            break
        await __import__("asyncio").sleep(0.05)
    assert row["active"] == 1 and row["pending"] == 0 and row["next_run"]
    # A rejected request leaves nothing behind.
    other = await schedule(engine, ["python3", "alert.py"], pending=1)
    await engine.check_pending_scripts()
    for _ in range(100):
        pending = await engine.approvals.pending()
        if pending:
            break
        await __import__("asyncio").sleep(0.05)
    await engine.receive(Entry("button", SIMULATED_USER, button_datum=f"apr:{pending[0]['id']}:no"))
    for _ in range(100):
        if await engine.db.one("SELECT * FROM scheduled_tasks WHERE id=?", (other,)) is None:
            break
        await __import__("asyncio").sleep(0.05)
    assert await engine.db.one("SELECT * FROM scheduled_tasks WHERE id=?", (other,)) is None


def test_scheduling_a_script_asks_for_approval(config_tmp):
    pol = Policy(config_tmp)
    ctx = CallContext(task_id=1, task_folder=config_tmp.paths.create_task_folder(1))
    d = pol.evaluate(
        "mcp__aide__schedule_script", {"project": "alert", "command": "python3 a.py"}, ctx
    )
    assert d is not None and d.level == 2
    for free in ("mcp__aide__generate_document", "mcp__aide__verify_document"):
        assert pol.evaluate(free, {}, ctx).level == 0
