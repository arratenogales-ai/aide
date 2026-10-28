"""The user's folders outside the program's: Finished Tasks, Document Inbox,
Backups and the Projects link go in the folder that contains the project, with the same
protections as inside."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from aide.common.config import Config
from aide.common.paths import (
    BACKUPS_FOLDER,
    FINISHED_FOLDER,
    INBOX_FOLDER,
    PROJECTS_LINK,
    Paths,
    user_folders,
)
from aide.core.policy import CallContext, Policy
from aide.core.scripts import sandbox_profile
from aide.core.session import sandbox_settings

REAL_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def outside(tmp_paths) -> Config:
    """The project in <tmp>/project and the user's folders in <tmp>, as on their Mac."""
    paths = Paths(root=tmp_paths.root, folders_dir=tmp_paths.root.parent)
    paths.ensure()
    return Config.load(paths)


def test_folders_go_outside_the_program(outside):
    r = outside.paths
    home = r.root.parent
    assert r.finished == home / FINISHED_FOLDER
    assert r.inbox == home / INBOX_FOLDER and r.inbox.is_dir()
    assert r.backups == home / BACKUPS_FOLDER
    # The link to the projects, next to them, relative and correctly pointed.
    link = r.projects_link
    assert link == home / PROJECTS_LINK and link.is_symlink()
    assert not Path(link.readlink()).is_absolute()
    assert link.resolve() == r.projects.resolve()
    # None of that stays inside the program folder.
    for path in (r.finished, r.inbox, r.backups, r.projects_link):
        assert not path.is_relative_to(r.root), path


def test_without_specifying_they_stay_in_the_root(tmp_paths, tmp_path):
    assert tmp_paths.folders == tmp_paths.root
    assert tmp_paths.finished == tmp_paths.root / FINISHED_FOLDER
    # With separate data (simulator), they go with that data, wherever the user's are.
    data = tmp_path / "test-data"
    apart = Paths(root=tmp_paths.root, data_dir=data, folders_dir=tmp_path)
    assert apart.finished == data / FINISHED_FOLDER
    assert apart.inbox == data / INBOX_FOLDER


def test_they_are_chosen_automatically(tmp_path, monkeypatch):
    root = tmp_path / "My Agent" / "aide"
    root.mkdir(parents=True)
    monkeypatch.delenv("AIDE_FOLDERS", raising=False)
    assert user_folders(root) == root  # not moved yet
    (root.parent / FINISHED_FOLDER).mkdir()
    assert user_folders(root) == root.parent
    other = tmp_path / "other"
    monkeypatch.setenv("AIDE_FOLDERS", str(other))
    assert user_folders(root) == other.resolve()


def test_outside_they_stay_protected(outside):
    pol = Policy(outside)
    ctx = CallContext(task_id=7, task_folder=outside.paths.create_task_folder(7))
    r = outside.paths

    def level(tool, **entry):
        return pol.evaluate(tool, entry, ctx).level

    zip_ = r.backups / "backup-2026-11-01-1900.zip"
    paper = r.inbox / "insurance.pdf"
    for path in (zip_, paper):
        assert level("Read", file_path=str(path)) == 3, path
        assert level("Bash", command=f"cat '{path}'") == 3, path
        assert level("Bash", command=f"cp '{path}' .") == 3, path
    assert level("Grep", pattern="x", path=str(r.inbox)) == 3
    # The agent does not write to the final folders or the inbox: the core fills them.
    for folder in (r.finished, r.inbox, r.backups):
        assert level("Write", file_path=str(folder / "x.txt"), content="x") == 3, folder
    # The sandboxes (the agent's and the scripts') do not read them either.
    denied = sandbox_settings(outside)["filesystem"]["denyRead"]
    assert str(r.backups) in denied and str(r.inbox) in denied
    profile = sandbox_profile(r.task(7), r)
    for folder in (r.backups, r.inbox, r.finished):
        assert f'(subpath "{folder.resolve()}")' in profile, folder


def test_no_background_notice_is_lost():
    """Asyncio keeps only a weak reference to what create_task launches; what is not
    stored anywhere can be lost halfway."""
    loose = []
    for file in (REAL_ROOT / "src").rglob("*.py"):
        for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Call)
                and isinstance(node.value.func, ast.Attribute)
                and node.value.func.attr in ("create_task", "ensure_future")
            ):
                loose.append(f"{file.relative_to(REAL_ROOT)}:{node.lineno}")
    assert not loose, loose


async def test_a_hook_notice_is_kept_while_it_is_sent(config_tmp, tmp_path):
    """The notice for a command with notice is sent without waiting, but it is kept until it
    finishes."""
    import asyncio

    from aide.bot.channel import FakeChannel
    from aide.core import hooks
    from aide.core.context import SessionContext
    from aide.core.engine import Engine
    from aide.core.states import TaskState

    engine = Engine(config_tmp, channel=FakeChannel(print_out=False))
    await engine.start(workers=False)
    try:
        task = await engine.tasks.create("Install requests", state=TaskState.RUNNING)
        folder = config_tmp.paths.create_task_folder(task.id)
        ses = SessionContext(core=engine.core, task_id=task.id, folder=folder, role="orchestrator")
        sent = asyncio.Event()

        async def notify(task_id, text):
            await asyncio.sleep(0.05)
            sent.set()

        engine.core.notify = notify
        pre = hooks.create_hooks(ses)["PreToolUse"][0].hooks[0]
        order = {"tool_name": "Bash", "tool_input": {"command": "pip install requests"}}
        await pre(order, None, None)
        assert hooks._NOTICES_IN_PROGRESS and not sent.is_set()
        await asyncio.wait_for(sent.wait(), 2)
        await asyncio.sleep(0)
        assert not hooks._NOTICES_IN_PROGRESS
    finally:
        await engine.stop()
