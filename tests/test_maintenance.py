"""Weekly maintenance: security, usage, improvements, cleanup, backup, updates and report."""

from __future__ import annotations

import json
import os
import zipfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from aide.bot.channel import Entry, FakeChannel
from aide.bot.commands import run_command
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.core.states import TaskState
from aide.maintenance import (
    backups,
    cleanup,
    consumption,
    improvements,
    report,
    security,
    updates,
)
from aide.maintenance.section import Section
from aide.simulate import SIMULATED_USER
from tests.fake_sdk import FakeClient, Recorder


@pytest.fixture
async def engine(config_tmp, monkeypatch, tmp_path):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    # Nothing being tested touches the LaunchAgent or the real transcripts.
    monkeypatch.setattr(security, "PLIST", tmp_path / "no-existe.plist")
    monkeypatch.setattr(cleanup, "CLAUDE_PROJECTS", tmp_path / "proyectos-claude")
    m = Engine(config_tmp, channel=FakeChannel(print_out=False))
    await m.start(workers=False)
    m.commands = run_command
    yield m
    await m.stop()


async def button(engine: Engine, datum: str) -> None:
    await engine.receive(Entry("button", SIMULATED_USER, button_datum=datum))


def no_audit(monkeypatch, vulnerable=None, error=None):
    monkeypatch.setattr(security, "audit_dependencies", lambda root: (vulnerable or [], error))
    monkeypatch.setattr(security, "process_listeners", lambda port, pid=None: ["127.0.0.1"])


# --- a) security --------------------------------------------------------------------------------


async def test_security_fingerprints_and_their_approval(engine, monkeypatch):
    no_audit(monkeypatch)
    s = await security.review(engine)
    assert s.ok and "First check" in s.lines[0]
    s = await security.review(engine)
    assert s.ok and "haven't changed" in s.lines[0]
    permissions = engine.config.paths.config / "permissions.yaml"
    permissions.write_text(permissions.read_text() + "\n# change\n")
    s = await security.review(engine)
    assert not s.ok and "permissions.yaml" in s.lines[0]
    [[button_approve]] = s.buttons
    digest = button_approve.datum.split(":")[2]
    # If it changes again after the report, what was not seen is not approved.
    permissions.write_text(permissions.read_text() + "# another\n")
    assert not await security.approve_fingerprints(engine, digest)
    s = await security.review(engine)
    await button(engine, s.buttons[0][0].datum)
    assert "Security changes approved" in engine.channel.texts()[-1]
    assert (await security.review(engine)).ok


async def test_secrets_environment_panel_and_dependencies(engine, monkeypatch):
    paths = engine.config.paths
    paths.env.write_text("TELEGRAM_BOT_TOKEN=x\n")
    paths.env.chmod(0o644)
    no_audit(monkeypatch)
    s = await security.review(engine)
    assert not s.ok and any("644" in line for line in s.lines)
    assert any(f'chmod 600 "{paths.env}"' in q for q in s.that_do)
    paths.env.chmod(0o600)
    assert (await security.review(engine)).ok
    # Anthropic API credentials in the environment or in the .env.
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    s = await security.review(engine)
    assert not s.ok and any("ANTHROPIC_API_KEY" in line for line in s.lines)
    assert "sk-test" not in json.dumps(s.for_model())
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    paths.env.write_text("ANTHROPIC_AUTH_TOKEN=x\n")
    assert not (await security.review(engine)).ok
    paths.env.write_text("TELEGRAM_BOT_TOKEN=x\n")
    # The panel listening outside 127.0.0.1.
    monkeypatch.setattr(security, "process_listeners", lambda port, pid=None: ["*"])
    s = await security.review(engine)
    assert not s.ok and any("listens on *" in line for line in s.lines)
    # Known vulnerabilities.
    vulnerable = {
        "name": "requests",
        "version": "2.0",
        "vulns": [{"id": "CVE-1", "fix_versions": ["2.32"]}],
    }
    no_audit(monkeypatch, [vulnerable])
    s = await security.review(engine)
    assert not s.ok and any("CVE-1" in line for line in s.lines)
    assert any("requests to 2.32" in q for q in s.that_do)


async def test_a_check_that_cannot_be_done_is_a_warning(engine, monkeypatch):
    # Under launchd uv was not found: the audit was not run and the report did not highlight it.
    def no_uv():
        raise FileNotFoundError("can't find the uv program")

    monkeypatch.setattr(security, "search_uv", no_uv)
    vulnerable, error = security.audit_dependencies(engine.config.paths.root)
    assert vulnerable == [] and error.startswith("FileNotFoundError")
    no_audit(monkeypatch, error=error)
    s = await security.review(engine)
    assert not s.ok and any("couldn't look for vulnerabilities" in x for x in s.lines)
    assert any("Claude Code" in q for q in s.that_do)

    def fails(root):
        raise FileNotFoundError("can't find the uv program")

    monkeypatch.setattr(updates, "outdated", fails)
    s = await updates.review(engine)
    assert not s.ok and "not urgent" in s.that_do[0]


def test_uv_is_found_even_if_not_in_the_path(monkeypatch, tmp_path):
    import sys

    from aide.maintenance import environment

    assert Path(sys.base_prefix) / "bin" / "uv" in environment.uv_candidates()
    monkeypatch.setattr(environment.shutil, "which", lambda name: None)  # launchd's PATH
    fake = tmp_path / "bin" / "uv"
    fake.parent.mkdir()
    fake.write_text("#!/bin/sh\n")
    monkeypatch.setattr(environment, "uv_candidates", lambda: [tmp_path / "no" / "uv", fake])
    with pytest.raises(FileNotFoundError):
        environment.search_uv()  # it exists, but cannot be executed
    fake.chmod(0o755)
    assert environment.search_uv() == str(fake)
    monkeypatch.setattr(environment, "uv_candidates", lambda: [])
    with pytest.raises(FileNotFoundError):
        environment.search_uv()


async def test_google_permissions_and_week_summary(engine, monkeypatch):
    from aide.tools.google_auth import SCOPES

    no_audit(monkeypatch)
    engine.core.services["google"] = SimpleNamespace(granted_permissions=lambda: list(SCOPES))
    assert any("exactly Aide's three" in line for line in (await security.review(engine)).lines)
    # Without GOOGLE_ACCOUNT in .env the account would not be checked on renewal: warning.
    s = await security.review(engine)
    assert not s.ok and any("GOOGLE_ACCOUNT" in q for q in s.that_do)
    engine.config.environment.GOOGLE_ACCOUNT = "alex.doe@example.com"
    assert (await security.review(engine)).ok
    extra = [*SCOPES, "https://mail.google.com/"]
    engine.core.services["google"] = SimpleNamespace(granted_permissions=lambda: extra)
    assert not (await security.review(engine)).ok
    await engine.tasks.event(None, "blocked", "Bash: rm -rf")
    await engine.tasks.event(None, "user_no_allowed", "id 666")
    week = await security.week_summary(engine)
    assert week["blocked"] == 1 and week["disallowed_users"] == 1


# --- b) usage -----------------------------------------------------------------------------------


async def save_weekly(engine: Engine, used: float, reset: datetime) -> None:
    from claude_agent_sdk import RateLimitInfo

    raw = {
        "unifiedWindows": {"seven_day": {"utilization": used, "resetsAt": int(reset.timestamp())}}
    }
    await engine.usage.log_limit(
        RateLimitInfo(status="allowed", rate_limit_type="seven_day", raw=raw)
    )


def test_projection_to_reset():
    reset = datetime(2026, 10, 25, 19, 0, tzinfo=UTC)
    # Midweek at 40%: it would reach 80%.
    assert consumption.projection(0.4, reset, reset - timedelta(days=3.5)) == pytest.approx(0.8)
    assert consumption.projection(0.1, reset, reset - timedelta(days=6.5)) is None  # very soon


async def test_usage_section(engine):
    moment = datetime.now(UTC)
    await save_weekly(engine, 0.6, moment + timedelta(days=3))
    task = await engine.tasks.create("Big report", state=TaskState.COMPLETED)
    await engine.db.run(
        "INSERT INTO usage(task_id, hour, origin, model, input_tokens, output_tokens, cache_read) "
        "VALUES (?, ?, 'orchestrator', 'claude-opus-5-5', 1000, 2000, 1200000)",
        (task.id, moment.isoformat(timespec="seconds")),
    )
    s = await consumption.review(engine, moment)
    text = " ".join(s.lines)
    assert "Weekly limit at 60%" in text
    assert not s.ok and "cutting it close" in text  # 60% in 4 days: it would reach 105%
    assert f'task {task.id} "Big report" with 1.2M tokens' in text


async def test_70_and_90_percent_warnings_and_temporary_sonnet(engine):
    reset = datetime.now(UTC) + timedelta(days=2)
    await save_weekly(engine, 0.65, reset)
    await consumption.review_thresholds(
        engine,
        {
            "unifiedWindows": {
                "seven_day": {"utilization": 0.65, "resetsAt": int(reset.timestamp())}
            }
        },
    )
    assert engine.channel.texts() == []
    raw = {
        "unifiedWindows": {"seven_day": {"utilization": 0.72, "resetsAt": int(reset.timestamp())}}
    }
    await consumption.review_thresholds(engine, raw)
    await consumption.review_thresholds(engine, raw)  # only once a week
    assert len(engine.channel.texts()) == 1 and "72%" in engine.channel.texts()[0]
    raw["unifiedWindows"]["seven_day"]["utilization"] = 0.91
    await consumption.review_thresholds(engine, raw)
    notice = engine.channel.messages[-1]
    assert "91%" in notice.text and "I won't change it without your permission" in notice.text
    data = [b.datum for row in notice.buttons for b in row]
    assert f"sonnet:yes:{int(reset.timestamp())}" in data and "sonnet:no" in data
    # Without the yes, nothing changes.
    task = await engine.tasks.create("Something", state=TaskState.IN_QUEUE)
    assert (await engine.model_of(task))[0].alias == "opus"
    await button(engine, f"sonnet:yes:{int(reset.timestamp())}")
    assert "go back to Opus on their own" in engine.channel.texts()[-1]
    assert await engine.temporary_sonnet_active()
    assert (await engine.model_of(task))[0].alias == "sonnet"
    forced = await engine.tasks.create("With Opus", state=TaskState.IN_QUEUE)
    await engine.tasks.update(forced.id, forced_model="opus")
    assert (await engine.model_of(await engine.tasks.require(forced.id)))[0].alias == "opus"
    # Recording a limits data point triggers the threshold review.
    calls = []

    async def observer(raw):
        calls.append(raw)

    engine.usage.to_log = observer
    await save_weekly(engine, 0.5, reset)
    assert calls and calls[0]["unifiedWindows"]["seven_day"]["utilization"] == 0.5


# --- c) improvements ----------------------------------------------------------------------------


async def test_validation_of_improvements_without_code(engine):
    C = improvements.Change
    path, new = improvements.validate_change(engine, C("agent/CLAUDE.md", "add", "- New rule."))
    assert path.name == "CLAUDE.md" and new.endswith("- New rule.\n")
    for bad in (
        C("config/permissions.yaml", "add", "x"),
        C("config/settings.yaml", "add", "x"),
        C("src/aide/core/policy.py", "add", "x"),
        C("agent/CLAUDE.md", "reemplazar", "new", search="text that does not exist"),
        C("config/models.yaml", "reemplazar", "roles: {}", search="roles:"),
        C("memory", "add", "two\nlines"),
        C("agent/CLAUDE.md", "add", "x" * 700),
    ):
        with pytest.raises(ValueError):
            improvements.validate_change(engine, bad)
    # Exceeding 150 lines between CLAUDE.md and profile.md, whatever the profile (in a clean
    # copy of the repository, the tests' one is minimal).
    long = "\n".join(["-"] * (improvements.LINES_LIMIT + 1))
    assert len(long) <= improvements.MAX_TEXT
    with pytest.raises(ValueError, match="lines"):
        improvements.validate_change(engine, C("agent/CLAUDE.md", "add", long))
    path, _ = improvements.validate_change(
        engine,
        C("agent/.claude/skills/letters/SKILL.md", "create", "---\nname: letters\n---\nHello"),
    )
    assert path == engine.config.paths.skills / "letters" / "SKILL.md"


async def test_proposals_apply_undo_and_type_b(engine):
    claude_md = engine.config.paths.agent / "CLAUDE.md"
    original = claude_md.read_text()
    proposals = [
        {
            "kind": "A",
            "title": "Date format",
            "reason": "In task 3 you corrected the dates.",
            "change": {
                "file": "agent/CLAUDE.md",
                "operation": "add",
                "text": "- Dates as Oct 28.",
            },
        },
        {
            "kind": "A",
            "title": "Touch the policy",
            "reason": "A command was blocked.",
            "change": {"file": "config/permissions.yaml", "operation": "add", "text": "x"},
        },
        {
            "kind": "B",
            "title": "Gmail retries",
            "reason": "It failed twice.",
            "change": {
                "file": "src/aide/tools/google_gmail.py",
                "operation": "describe",
                "text": "Retry 3 times.",
            },
        },
    ]
    saved = await improvements.save_proposals(engine, proposals)
    assert [g["kind"] for g in saved] == ["A", "B", "B"]  # the permissions one becomes B
    assert "Can't be applied without code" in saved[1]["note"]
    b_file = engine.config.paths.root / saved[1]["b_file"]
    b_text = b_file.read_text()
    assert "Gmail retries" in b_text and "Touch the policy" in b_text
    assert "never applied on their own" in b_text
    # They are read as text, not as JSON.
    assert "What to ask Claude Code (in `src/aide/tools/google_gmail.py`):" in b_text
    assert "Retry 3 times." in b_text and '"operation"' not in b_text
    assert "Proposed change in `config/permissions.yaml` (add)" in b_text
    # Apply with the button, undo with the command.
    to_id = saved[0]["id"]
    await button(engine, f"improvement:{to_id}:apply")
    assert "Improvement" in engine.channel.texts()[-1]
    assert "/undo_improvement" in engine.channel.texts()[-1]
    assert claude_md.read_text().endswith("- Dates as Oct 28.\n")
    await button(engine, f"improvement:{to_id}:apply")
    assert "is already applied" in engine.channel.texts()[-1]
    await engine.receive(
        Entry(
            "command",
            SIMULATED_USER,
            text=f"/undo_improvement {to_id}",
            command="undo_improvement",
            arguments=str(to_id),
        )
    )
    assert claude_md.read_text() == original and "undone" in engine.channel.texts()[-1]
    # Type B ones are not applied from the agent.
    assert "can't be applied" in (await improvements.apply(engine, saved[2]["id"]))
    assert (await improvements.discard(engine, saved[2]["id"])).startswith("Improvement")


async def test_a_created_skill_is_undone_by_deleting_it(engine):
    [g] = await improvements.save_proposals(
        engine,
        [
            {
                "kind": "A",
                "title": "Letters skill",
                "reason": "You asked for two formal letters.",
                "change": {
                    "file": "agent/.claude/skills/letters/SKILL.md",
                    "operation": "create",
                    "text": "---\nname: letters\ndescription: Formal letters.\n---\nBe formal.",
                },
            }
        ],
    )
    skill = engine.config.paths.skills / "letters" / "SKILL.md"
    await improvements.apply(engine, g["id"])
    assert skill.read_text().startswith("---\nname: letters")
    await improvements.undo(engine, g["id"])
    assert not skill.exists()


async def test_signals_of_the_week(engine):
    failed = await engine.tasks.create("Something that failed", state=TaskState.FAILED)
    await engine.tasks.update(failed.id, error="no connection")
    done = await engine.tasks.create("Report", state=TaskState.COMPLETED)
    child = await engine.tasks.create("Make it shorter", kind="message", state=TaskState.COMPLETED)
    await engine.tasks.update(child.id, parent_task=done.id)
    for text in (
        "What will the weather be like tomorrow in Springfield?",
        "What will the weather be like tomorrow in downtown Springfield?",
    ):
        await engine.tasks.create(text, kind="quick", state=TaskState.COMPLETED)
    s = await improvements.signals(engine)
    assert s["failed_or_cancelled"][0]["error"] == "no connection"
    assert s["user_corrections"][0] == {"task": done.id, "message": "Make it shorter"}
    assert len(s["duplicate_questions"]) == 1


# --- d) cleanup ---------------------------------------------------------------------------------


async def test_cleanup_never_touches_what_is_protected(engine):
    paths = engine.config.paths
    roots = [paths.tasks, paths.agent / "test_tasks"]
    for protected in (
        paths.finished,
        paths.projects,
        paths.profile,
        paths.memory,
        paths.backups,
        paths.config,
        paths.root,
    ):
        assert not cleanup.can_delete(protected, paths, roots + [paths.root])
    assert not cleanup.can_delete(Path("/etc"), paths, roots)
    (paths.agent / "test_tasks" / "x").mkdir(parents=True)
    assert cleanup.can_delete(paths.agent / "test_tasks" / "x", paths, roots)


async def test_cleanup_of_old_internal_data(engine):
    paths = engine.config.paths
    stale = await engine.tasks.create("Old", state=TaskState.COMPLETED)
    no_backup = await engine.tasks.create("No copy", state=TaskState.COMPLETED)
    recent = await engine.tasks.create("Recent", state=TaskState.COMPLETED)
    ago_40 = (datetime.now(UTC) - timedelta(days=40)).isoformat(timespec="seconds")
    for t in (stale, no_backup):
        await engine.db.run("UPDATE transitions SET hour=? WHERE task_id=?", (ago_40, t.id))
    for t in (stale, no_backup, recent):
        folder = paths.create_task_folder(t.id)
        (folder / "data" / "bruto.json").write_text("{}")
        (folder / "results" / "report.docx").write_text("final")
    final = paths.finished / "2026-09-10 old"
    final.mkdir(parents=True)
    await engine.tasks.update(stale.id, final_folder=str(final))
    await engine.tasks.update(recent.id, final_folder=str(final))
    tests = paths.agent / "test_tasks" / "datos_viejos"
    tests.mkdir(parents=True)
    old = (datetime.now() - timedelta(days=10)).timestamp()
    os.utime(tests, (old, old))

    s = await cleanup.clean(engine, test=True)
    assert s.lines[0].startswith("I would delete 4 items")
    assert (paths.task(stale.id) / "data").exists() and tests.exists()
    s = await cleanup.clean(engine, test=False)
    assert s.lines[0].startswith("I deleted 4 items") and "compacted" in s.lines[1]
    assert not (paths.task(stale.id) / "data").exists()
    assert not (paths.task(stale.id) / "progress.md").exists()
    assert (paths.task(stale.id) / "results" / "report.docx").exists()
    assert (paths.task(no_backup.id) / "data").exists()  # without a final folder it is not touched
    assert (paths.task(recent.id) / "data").exists()
    assert not tests.exists() and final.exists()


# --- e) backup ----------------------------------------------------------------------------------


async def test_backup_without_secrets_and_only_the_latest(engine):
    paths = engine.config.paths
    (paths.config / "credentials.json").write_text('{"secret": 1}')
    (paths.config / "backups").mkdir()
    (paths.config / "backups" / "settings.yaml.1").write_text("old")
    paths.env.write_text("TELEGRAM_BOT_TOKEN=x")
    project = paths.projects / "web"
    (project / "node_modules" / "lib").mkdir(parents=True)
    (project / "node_modules" / "lib" / "x.js").write_text("x")
    (project / "index.html").write_text("<h1>web</h1>")
    paths.memory.write_text("# Memory\n")
    folder = paths.root / "Backups"
    done = []
    for minute in range(6):
        file, _, _ = backups.create_backup(paths, datetime(2026, 10, 25, 19, minute), keep=4)
        done.append(file)
    assert sorted(p.name for p in folder.iterdir()) == [p.name for p in done[-4:]]
    assert done[-1].name == "backup-2026-10-25-1905.zip"
    assert (done[-1].stat().st_mode & 0o777) == 0o600
    names = zipfile.ZipFile(done[-1]).namelist()
    assert "data/agent.db" in names and "agent/profile.md" in names
    assert "agent/memory.md" in names and "config/models.yaml" in names
    assert "agent/projects/web/index.html" in names
    assert not any(
        "credentials" in n or ".env" in n or "node_modules" in n or "backups/" in n for n in names
    )


# --- f) updates -----------------------------------------------------------------------------------


async def test_updates_only_for_the_main_ones_and_touching_nothing(engine, monkeypatch):
    root = Path(__file__).resolve().parents[1]
    assert "claude-agent-sdk" in updates.main(root)

    output = json.dumps(
        [
            {"name": "fastapi", "version": "0.148.0", "latest_version": "0.156.0"},
            {"name": "anyio", "version": "4.0", "latest_version": "4.1"},
        ]
    )
    monkeypatch.setattr(updates, "search_uv", lambda: "uv")
    monkeypatch.setattr(
        updates.subprocess,
        "run",
        lambda *a, **k: SimpleNamespace(returncode=0, stdout=output, stderr=""),
    )
    new = updates.outdated(root)
    assert [p["name"] for p in new] == ["fastapi"]  # anyio is not a direct dependency
    tested = []

    def test(root, packages):
        tested.append(packages)
        return True, "280 passed"

    monkeypatch.setattr(updates, "outdated", lambda root: new)
    monkeypatch.setattr(updates, "test", test)
    s = await updates.review(engine)
    assert tested == [["fastapi"]]
    assert "fastapi 0.148.0 → 0.156.0" in " ".join(s.lines)
    assert "it's safe to update" in " ".join(s.lines)
    assert "I haven't changed anything" in " ".join(s.lines)


def test_the_updates_test_never_launches_the_real_tests(monkeypatch, tmp_path):
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='x'\n")
    commands = []

    def run(order, **kwargs):
        commands.append((order, kwargs))
        output = "331 passed, 3 deselected in 98.20s" if "pytest" in order else ""
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    monkeypatch.setattr(updates, "search_uv", lambda: "/opt/uv/bin/uv")
    monkeypatch.setattr(updates.subprocess, "run", run)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "secreto")
    ok, summary = updates.test(root, ["fastapi"])
    assert ok and summary == "331 passed, 3 deselected in 98.20s"
    lock, sync, pytest_ = (order for order, _ in commands)
    assert lock[:2] == ["/opt/uv/bin/uv", "lock"] and "--upgrade-package" in lock
    assert pytest_[pytest_.index("-m") + 1] == "not real"
    for _, kwargs in commands:
        environment = kwargs["env"]
        assert "TELEGRAM_BOT_TOKEN" not in environment  # no secrets in the copy
        assert environment["PATH"].startswith("/opt/uv/bin")
        assert environment["UV_PROJECT_ENVIRONMENT"] != str(root / ".venv")  # separate environment
        assert kwargs["cwd"] != root


# --- report ------------------------------------------------------------------------------------


def test_report_puts_what_fails_first():
    well = Section("consumption", "Usage", lines=["Weekly limit at 40%."])
    bad = Section("security", "Security")
    bad.failure(".env has permissions 644 instead of 600.", 'In Terminal: chmod 600 ".env"')
    proposals = [
        {
            "id": 3,
            "kind": "A",
            "title": "Dates",
            "reason": "You corrected dates.",
            "change": {"file": "agent/CLAUDE.md", "text": "- Oct 28"},
        },
        {
            "id": 4,
            "kind": "B",
            "title": "Retries",
            "reason": "Gmail failed.",
            "b_file": "improvements/2026-10-25.md",
        },
    ]
    message, buttons = report.compose(
        [well, bad],
        "All good except the .env.",
        proposals,
        datetime(2026, 10, 25, 19, 0),
        False,
        "http://127.0.0.1:8765/enter?t=x",
    )
    assert message.index("Needs your attention") < message.index("All good")
    assert "chmod 600" in message and "→" in message
    assert 'Change in agent/CLAUDE.md → "- Oct 28"' in message
    assert "improvements/2026-10-25.md" in message and "open this link on the Mac" in message
    assert "Weekly maintenance</b>, Oct 25" in message
    assert [[b.datum for b in row] for row in buttons] == [
        ["improvement:3:apply", "improvement:3:discard"]
    ]
    long, _ = report.compose([well], "x " * 5000, [], datetime(2026, 10, 25), True, None)
    assert len(long) <= report.MESSAGE_LIMIT + 200 and "(test:" in long


async def test_full_run_with_a_single_report(engine, monkeypatch):
    no_audit(monkeypatch)
    monkeypatch.setattr(updates, "outdated", lambda root: [])
    received = {}

    async def draft(engine_, data):
        received["data"] = data
        return {
            "report": "A quiet week.",
            "improvements": [
                {
                    "kind": "A",
                    "title": "Rule",
                    "reason": "Because of task 1.",
                    "change": {
                        "file": "agent/CLAUDE.md",
                        "operation": "add",
                        "text": "- Rule.",
                    },
                }
            ],
        }

    monkeypatch.setattr(report, "draft", draft)
    engine.maintenance = report.run
    expires = datetime.now(UTC) + timedelta(hours=20)
    engine.core.services["google"] = SimpleNamespace(
        expires_in=lambda: expires, granted_permissions=lambda: []
    )
    engine.core.services["panel_link"] = lambda destination="/": (
        f"http://127.0.0.1:8765/enter?t=SECRET&go={destination}"
    )
    await engine.db.kv_put("test_maintenance", True)
    await engine.run_maintenance()
    [message] = engine.channel.messages
    assert message.text.startswith("<b>Weekly maintenance</b>") and "(test" in message.text
    assert "A quiet week." in message.text and "SECRET" in message.text  # the link reaches the user
    assert "SECRET" not in json.dumps(received["data"])  # but the model does not see it
    assert "The Google authorization expires" in message.text
    assert any(b.datum.startswith("improvement:") for row in message.buttons for b in row)
    assert await engine.db.kv_read("test_maintenance") is None
    assert (engine.config.paths.root / "Backups").is_dir()
    [event] = await engine.db.everything("SELECT * FROM events WHERE kind='maintenance'")
    assert json.loads(event["data"])["test"] is True


async def test_if_the_connection_drops_the_report_arrives_on_return(engine, monkeypatch):
    """With no connection when sending, the report, with the button to approve the
    security changes, is sent the next time there is a connection."""
    import asyncio

    from aide.core.engine import PENDING_MAINTENANCE_KEY

    no_audit(monkeypatch)
    monkeypatch.setattr(updates, "outdated", lambda root: [])

    async def draft(engine_, data):
        return {"report": "A quiet week.", "improvements": []}

    monkeypatch.setattr(report, "draft", draft)
    engine.maintenance = report.run
    await engine.db.kv_put(security.FINGERPRINTS_KEY, {"config/permissions.yaml": "the old one"})
    compose = report.compose

    def leaves(*a, **k):
        engine.connection.mark_offline()  # right before sending it
        return compose(*a, **k)

    monkeypatch.setattr(report, "compose", leaves)
    await engine.run_maintenance()
    assert engine.channel.messages == [] and await engine.db.kv_read(PENDING_MAINTENANCE_KEY)
    assert not await engine.db.everything("SELECT * FROM events WHERE kind='maintenance'")
    monkeypatch.setattr(report, "compose", compose)
    engine.connection.mark_online()
    await engine.check_all()
    while engine._background:
        await asyncio.gather(*list(engine._background), return_exceptions=True)
    [message] = [m for m in engine.channel.messages if m.text.startswith("<b>Weekly maintenance")]
    assert any(b.datum.startswith("security:approve:") for row in message.buttons for b in row)
    assert not await engine.db.kv_read(PENDING_MAINTENANCE_KEY)


async def test_without_telegram_at_startup_the_report_is_not_considered_sent(engine, monkeypatch):
    """If the agent starts without a connection and the report is made before Telegram
    responds, it stays pending for the next connection."""
    from aide.core.engine import PENDING_MAINTENANCE_KEY

    no_audit(monkeypatch)
    monkeypatch.setattr(updates, "outdated", lambda root: [])

    async def draft(engine_, data):
        return {"report": "A quiet week.", "improvements": []}

    monkeypatch.setattr(report, "draft", draft)
    engine.maintenance = report.run
    channel, engine.channel = engine.channel, None
    await engine.run_maintenance()
    engine.channel = channel
    assert await engine.db.kv_read(PENDING_MAINTENANCE_KEY)
    assert not await engine.db.everything("SELECT * FROM events WHERE kind='maintenance'")


async def test_a_single_scheduled_maintenance_task(engine):
    await report.ensure_scheduled_task(engine)
    await report.ensure_scheduled_task(engine)
    [row] = await engine.db.everything("SELECT * FROM scheduled_tasks WHERE kind='maintenance'")
    assert row["timing_text"] == "every Sunday at 19:00" and row["policy"] == "run_always"
    engine.config.settings.maintenance.hour = "20:30"
    await report.ensure_scheduled_task(engine)
    [row] = await engine.db.everything("SELECT * FROM scheduled_tasks WHERE kind='maintenance'")
    assert row["timing_text"] == "every Sunday at 20:30"
    # If it was overdue (Mac off), it runs when checking the scheduled tasks, without creating
    # tasks.
    calls = []

    async def maintenance(m, test):
        calls.append(test)

    engine.maintenance = maintenance
    await engine.db.run(
        "UPDATE scheduled_tasks SET next_run=? WHERE id=?",
        ((datetime.now(UTC) - timedelta(days=2)).isoformat(timespec="seconds"), row["id"]),
    )
    assert await engine.check_scheduled_tasks() == []
    for _ in range(50):
        if calls:
            break
        await __import__("asyncio").sleep(0.02)
    assert calls == [False]


def test_a_stored_weekly_scheduled_task_is_read_again():
    """Cuando.to_json stores the days as mon...sun: interpreting must accept them."""
    from zoneinfo import ZoneInfo

    from aide.core import scheduler

    zone = ZoneInfo("Europe/Berlin")
    timing = scheduler.interpret(
        {"kind": "weekly", "days": ["sunday", "wednesday"], "hour": "19:00"}, zone
    )
    assert scheduler.interpret(timing.to_json(), zone) == timing
    assert timing.days == ("wed", "sun")
    assert scheduler.describe(timing) == "every Wednesday and Sunday at 19:00"
    # Spanish names are still valid, with or without accent and in capitals.
    for alias in (["sunday", "miércoles"], ["Domingo", "miercoles"], ["SUNDAY", "Wednesday"]):
        assert scheduler.interpret({**json.loads(timing.to_json()), "days": alias}, zone) == timing
    with pytest.raises(scheduler.InvalidTiming, match="invalid day"):
        scheduler.interpret({"kind": "weekly", "days": ["someday"], "hour": "19:00"}, zone)


def test_maintenance_day_in_english_or_spanish():
    from pydantic import ValidationError

    from aide.common.config import Maintenance

    assert Maintenance().day == "sunday"
    assert Maintenance(day="sunday").day == "sunday"
    assert Maintenance(day="Miércoles").day == "wednesday"
    assert Maintenance(day="Friday").day == "friday"
    with pytest.raises(ValidationError):
        Maintenance(day="someday")


async def test_backup_carries_what_is_not_in_git(engine):
    """What is not in git also goes in the backup: projects without dependencies or
    caches, and your documents."""
    paths = engine.config.paths
    web = paths.projects / "web"
    (web / "data").mkdir(parents=True, exist_ok=True)
    (web / "data" / "notes.csv").write_text("a,b\n", encoding="utf-8")
    (web / "__pycache__").mkdir(exist_ok=True)
    (web / "__pycache__" / "x.pyc").write_bytes(b"x")
    (paths.documents / "2026").mkdir(parents=True, exist_ok=True)
    (paths.documents / "2026" / "insurance.pdf").write_bytes(b"%PDF-1.4 insurance")
    file, _, _ = backups.create_backup(paths, datetime(2026, 10, 25, 19, 0), keep=4)
    names = zipfile.ZipFile(file).namelist()
    for expected in ("agent/projects/web/data/notes.csv", "data/documents/2026/insurance.pdf"):
        assert expected in names, expected
    assert not any("__pycache__" in n for n in names)


async def test_backup_carries_no_project_secrets(engine):
    """What stays outside git can be a secret: neither a project's .env nor its keys enter the
    backup."""
    paths = engine.config.paths
    other = paths.projects / "portfolio"
    other.mkdir(parents=True, exist_ok=True)
    for name in (
        ".env",
        ".env.local",
        "server.pem",
        "token.json",
        "credentials.json",
        "app.py",
    ):
        (other / name).write_text("x", encoding="utf-8")
    (other / "keys").mkdir(exist_ok=True)
    (other / "keys" / "id_ed25519").write_text("x", encoding="utf-8")
    file, _, _ = backups.create_backup(paths, datetime(2026, 10, 25, 19, 0), keep=4)
    names = zipfile.ZipFile(file).namelist()
    assert "agent/projects/portfolio/app.py" in names
    for secret in (
        ".env",
        ".env.local",
        "server.pem",
        "token.json",
        "credentials.json",
        "id_ed25519",
    ):
        assert not any(n.endswith("/" + secret) for n in names), secret


async def test_cleanup_removes_old_things_received_by_telegram(engine):
    """What is received is copied to the task or the documents on arrival; the download
    is deleted after the log retention days, and the recent stays."""
    downloads = engine.config.paths.agent_cache / "telegram"
    old, new = downloads / "101", downloads / "202"
    for folder in (old, new):
        folder.mkdir(parents=True)
        (folder / "invoice.pdf").write_bytes(b"%PDF-1.4")
    days = engine.config.settings.maintenance.log_days + 1
    ago = datetime.now().timestamp() - days * 86400
    os.utime(old, (ago, ago))
    items = {path: reason for path, reason, _ in await cleanup.candidates(engine)}
    assert items.get(old) == "file received on Telegram"
    assert new not in items
    s = await cleanup.clean(engine)
    assert not old.exists() and (new / "invoice.pdf").is_file()
    assert s.ok


async def test_the_old_scheduled_task_changes_its_text_without_touching_the_next_run(engine):
    """A database from before stores “cada domingo a las 19:00”: it is moved to the new text
    without shifting the next run (if it was overdue, it must still be done)."""
    await report.ensure_scheduled_task(engine)
    overdue = (datetime.now(UTC) - timedelta(days=1)).isoformat(timespec="seconds")
    await engine.db.run(
        "UPDATE scheduled_tasks SET timing_text='cada sunday a las 19:00', description='Mantenimiento "
        "weekly', next_run=? WHERE kind='maintenance'",
        (overdue,),
    )
    await report.ensure_scheduled_task(engine)
    [row] = await engine.db.everything("SELECT * FROM scheduled_tasks WHERE kind='maintenance'")
    assert row["timing_text"] == "every Sunday at 19:00" and row["next_run"] == overdue
    assert row["description"].startswith("Weekly maintenance")


def test_the_config_the_update_copies_exists():
    root = Path(__file__).resolve().parents[1]
    assert all((root / "config" / name).is_file() for name in updates.COPY_CONFIG)
