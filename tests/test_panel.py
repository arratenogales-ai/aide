"""F9: local web panel (views, security, decisions only in Telegram, scheduled tasks, memory,
settings, live progress and final folder)."""

from __future__ import annotations

import asyncio
import re
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from aide.bot.channel import Entry, FakeChannel
from aide.bot.commands import run_command
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.core.states import TaskState
from aide.panel.app import COOKIE, MENU, Panel
from aide.simulate import SIMULATED_USER
from tests.fake_sdk import End, FakeClient, Recorder, Tool

TOKEN = "token-de-prueba"


@pytest.fixture
async def panel(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    engine = Engine(config_tmp, channel=FakeChannel(print_out=False))
    await engine.start()
    engine.commands = run_command  # the memory buttons are handled by the commands
    p = Panel(engine, token=TOKEN)
    base = f"http://127.0.0.1:{p.port}"
    transport = httpx.ASGITransport(app=p.app)
    async with httpx.AsyncClient(
        transport=transport, base_url=base, follow_redirects=False
    ) as client:
        r = await client.get(f"/enter?t={TOKEN}&go=/")
        assert r.status_code == 200 and COOKIE in r.cookies
        yield engine, p, client
    await engine.stop()


def _csrf(html: str) -> str:
    return re.search(r'name="csrf" value="([^"]+)"', html).group(1)


async def _button(engine: Engine, datum: str) -> None:
    """Press a Telegram button (the same path as the real bot)."""
    await engine.receive(Entry("button", SIMULATED_USER, button_datum=datum))


async def test_panel_security(config_tmp, panel):
    engine, p, client = panel
    base = f"http://127.0.0.1:{p.port}"
    transport = httpx.ASGITransport(app=p.app)
    async with httpx.AsyncClient(transport=transport, base_url=base) as anonymous:
        assert (await anonymous.get("/")).status_code == 401
        assert (await anonymous.get("/enter?t=otro")).status_code == 403
        assert (await anonymous.get("/style.css")).status_code == 200
    async with httpx.AsyncClient(
        transport=transport, base_url="http://evil.example:8765"
    ) as foreign:
        assert (await foreign.get("/style.css")).status_code == 400  # DNS rebinding
    r = await client.get("/")
    assert r.status_code == 200 and "Content-Security-Policy" in r.headers
    assert "script-src 'self'" in r.headers["Content-Security-Policy"]
    # Forms: without CSRF or with another origin, no.
    assert (await client.post("/scheduled", data={"description": "x"})).status_code == 403
    r = await client.post(
        "/scheduled", data={"csrf": p.csrf}, headers={"Origin": "http://evil.example"}
    )
    assert r.status_code == 403
    r = await client.post("/scheduled", data={"csrf": p.csrf}, headers={"Origin": "null"})
    assert r.status_code == 403
    # A panel form in the browser carries its origin and is accepted (with Referrer-Policy
    # no-referrer, Chrome sent `Origin: null` and it was rejected).
    r = await client.post(
        "/scheduled", data={"csrf": p.csrf}, headers={"Origin": f"http://127.0.0.1:{p.port}"}
    )
    assert r.status_code == 303
    assert r.headers["Referrer-Policy"] == "same-origin"
    # Downloads: only inside agent/.
    inside = engine.config.paths.task(1) / "results" / "report.md"
    inside.parent.mkdir(parents=True)
    inside.write_text("report")
    assert (await client.get("/file", params={"path": str(inside)})).text == "report"
    env = engine.config.paths.root / ".env"
    env.write_text("SECRET=1")
    for path in (str(env), "/etc/hosts", str(engine.config.paths.agent / ".." / ".env")):
        assert (await client.get("/file", params={"path": path})).status_code == 404


async def test_six_sections_in_the_sidebar_menu_and_advanced_at_the_bottom(panel):
    """The sidebar menu: five sections with their icon and color and Advanced at the
    bottom (Memory is “My profile”). Now Health has its own section, and
    Projects and Scheduled are tabs of Tasks. Always a white background, no dark mode."""
    engine, p, client = panel
    assert [name for _, name, _, _ in MENU] == [
        "Today",
        "Tasks",
        "Documents",
        "Health",
        "My profile",
    ]
    # Each section with its own color, no repeats.
    assert len({color for _, _, _, color in MENU}) == len(MENU)
    html = (await client.get("/")).text
    menu = re.search(r'<nav class="side"[^>]*>(.*?)</nav>', html, re.S).group(1)
    names = re.findall(r'class="section[^"]*"[^>]*>.*?</svg></span>([^<]+)</a>', menu, re.S)
    assert names == [n for _, n, _, _ in MENU] + ["Advanced"]
    assert 'href="/settings"' in menu
    # Scheduled and Projects are no longer in the menu (neither in the sidebar nor in the mobile
    # row, which is the same one).
    assert "Scheduled" not in menu and "/scheduled" not in menu and "/projects" not in menu
    assert 'class="section lilac active" aria-current="page"' in menu
    # The panel addresses are in English; the old Spanish ones no longer exist.
    for path in ("/tasks", "/documents", "/health", "/profile", "/settings"):
        assert f'href="{path}"' in html, path
    for path in ("/email", "/logs"):
        assert (await client.get(path)).status_code == 200, path
    for path in (
        "/tareas",
        "/documentos",
        "/programadas",
        "/proyectos",
        "/memoria",
        "/ajustes",
        "/correo",
        "/registros",
        "/aprobaciones",
        "/uso",
        "/empleo",
        "/candidaturas",
    ):
        assert (await client.get(path)).status_code == 404, path
    css = (await client.get("/style.css")).text
    assert "prefers-color-scheme: dark" not in css and "background: #ffffff" in css
    assert 'content="light"' in html and '<html lang="en">' in html


async def test_no_page_carries_inline_styles_or_scripts(panel):
    """The content policy allows no inline styles or scripts: not a single style= on
    any page, and the charts are SVG with their attributes."""
    engine, p, client = panel
    for path in (
        "/",
        "/tasks",
        "/documents",
        "/health",
        "/health?view=gym",
        "/health?view=meals",
        "/health?view=habits",
        "/tasks?view=projects",
        "/tasks?view=scheduled",
        "/profile",
        "/settings",
        "/email",
        "/logs",
    ):
        html = (await client.get(path)).text
        assert " style=" not in html and "<style" not in html, path
        assert re.findall(r"<script(?![^>]*\bsrc=)", html) == [], path


async def test_every_view_loads_with_data_and_without_dollars(panel):
    engine, p, client = panel
    from aide.core.usage import format_tokens

    task = await engine.tasks.create("Research flats in Rivertown", state=TaskState.RUNNING)
    await engine.tasks.event(task.id, "tool", "Bash: ls", {"tool": "Bash"})
    await engine.tasks.update(task.id, title="Flats in Rivertown", summary="Flat comparison")
    await engine.db.run(
        "INSERT INTO usage(task_id, hour, origin, model, input_tokens, output_tokens, cache_read, "
        "cache_creation, cost_usd, turns, web_searches) VALUES (?, ?, 'orchestrator', "
        "'claude-opus-5-5', 12000, 3000, 1200000, 5000, 0.12, 3, 0)",
        (task.id, datetime.now(UTC).isoformat(timespec="seconds")),
    )
    await engine.memory.add("The user prefers reports in docx")
    project = engine.config.paths.projects / "web-cv"
    project.mkdir(parents=True)
    (project / "CONTEXT.md").write_text("# web-cv\nStatic CV website.")
    (project / "index.html").write_text("<h1>CV</h1>")
    expected = {
        "/": "Flats in Rivertown",
        "/tasks": "Flats in Rivertown",
        f"/task/{task.id}": "Bash: ls",
        f"/task/{task.id}/transcript": "Transcript",
        "/tasks?view=scheduled": "What, when, and what to do if the Mac was off",
        "/profile": "The user prefers reports in docx",
        "/tasks?view=projects": "Static CV website.",
        "/projects/web-cv": "index.html",
        "/settings": "permissions.yaml",
        "/logs": "Logs",
    }
    for path, text in expected.items():
        r = await client.get(path)
        assert r.status_code == 200, path
        assert text in r.text, path
        # No dollars in what the user sees. Settings and Logs (Advanced) show the files as
        # they are, and the internal USD caps are configured in models.yaml.
        if path not in ("/settings", "/logs"):
            assert "USD" not in r.text and "$" not in r.text, path
    # Usage in tokens: total in the list and in the detail, with the breakdown in the detail.
    total = format_tokens(12000 + 3000 + 1200000 + 5000)
    assert total in (await client.get("/tasks")).text
    detail = (await client.get(f"/task/{task.id}")).text
    # In the detail, only the total (without the input, output and cache breakdown).
    breakdown = (await engine.usage.task_consumption(task.id)).breakdown()
    assert total in detail and breakdown not in detail
    start = (await client.get("/")).text
    # No usage or limits in the panel: they are in Telegram, with /usage.
    assert "Usage" not in start and "Plan limits" not in start
    # Partial reload for the live update: only the content.
    r = await client.get("/", headers={"X-Partial": "1"})
    assert "<html" not in r.text and "Flats in Rivertown" in r.text
    assert (await client.get("/tasks", params={"q": "Rivertown"})).text.count("/task/") >= 1
    assert (await client.get("/projects/..%2F..%2Fconfig")).status_code == 404


async def test_task_filter_by_the_five_groups(panel):
    engine, p, client = panel
    states = {
        "running": TaskState.RUNNING,
        "queued": TaskState.IN_QUEUE,
        "plan": TaskState.WAITING_PLAN,
        "chrome": TaskState.PAUSED_BROWSER,
        "done": TaskState.COMPLETED,
        "failed": TaskState.FAILED,
    }
    for name, state in states.items():
        t = await engine.tasks.create(f"task {name}", state=state)
        await engine.tasks.update(t.id, title=f"task {name}")
    expected = {
        "in_progress": {"task running", "task queued"},
        "waiting": {"task plan"},
        "in_pause": {"task chrome"},
        "completed": {"task done"},
        "cancelled": {"task failed"},
    }
    page = (await client.get("/tasks")).text
    for key, names in expected.items():
        assert f'href="/tasks?group={key}"' in page  # the filter links reach this same parameter
        html = (await client.get("/tasks", params={"group": key})).text
        seen = set(re.findall(r'class="task-link"[^>]*>([^<]+)</a>', html))
        assert seen == names, key
    html = (await client.get("/tasks", params={"group": "in_pause"})).text
    assert "Paused" in html and "Chrome closed" in html


async def test_the_version_changes_for_the_live_update(panel):
    engine, p, client = panel
    before = await p.views.version()
    await engine.tasks.create("nueva", state=TaskState.RECEIVED)
    assert await p.views.version() != before


async def test_approvals_are_decided_only_in_telegram(panel):
    engine, p, client = panel
    done = []
    FakeClient.script = lambda pr, o, r: [
        Tool("Bash", {"command": "git push origin main"}, lambda e: done.append(1) or "pushed"),
        End("<delivery>Pushed.</delivery>"),
    ]
    task = await engine.create_direct_task("Push the changes")
    for _ in range(100):
        pending = await engine.approvals.pending()
        if pending:
            break
        await asyncio.sleep(0.05)
    [approval] = pending
    # The panel shows it (for reference only) and has no button to decide it.
    start = (await client.get("/")).text
    assert "Waiting for your reply in Telegram" in start and "git push" in start
    for page in (start, (await client.get(f"/task/{task.id}")).text):
        assert "Approve" not in page and "Reject" not in page
        assert 'action="/aprobaciones' not in page and 'action="/lotes' not in page
    # Not even by hand: the routes for deciding no longer exist.
    for path in (f"/aprobaciones/{approval['id']}", "/lotes/x"):
        r = await client.post(path, data={"csrf": p.csrf, "decision": "yes"})
        assert r.status_code in (404, 405), path
    assert (await engine.approvals.pending())[0]["id"] == approval["id"]
    assert done == []
    # In Telegram, yes.
    await _button(engine, f"apr:{approval['id']}:yes")
    final = await engine.wait_task(task.id, max_wait=20)
    assert final.state == TaskState.COMPLETED and done == [1]
    row = await engine.db.one("SELECT * FROM approvals WHERE id=?", (approval["id"],))
    assert row["state"] == "approved" and row["resolved_by"] == "telegram"
    # The history remains in the task detail, for reference only.
    detail = (await client.get(f"/task/{task.id}")).text
    assert "git push origin main" in detail and "approved" in detail


async def test_plans_and_caps_are_decided_in_telegram(panel):
    engine, p, client = panel
    plans = []

    def script(prompt, options, registry):
        if "PLAN PHASE" in prompt or "Redo the plan" in prompt:
            plans.append(prompt)
            return [End(f"<plan>Goal: plan {len(plans)}</plan>")]
        if "raised the limit" in prompt:
            return [End("<delivery>Finished with a higher cap.</delivery>")]
        return [End(subtype="error_max_turns", is_error=True, turns=20)]

    FakeClient.script = script
    task = await engine.create_direct_task("Big task", size="large", needs_plan=True)
    await engine.wait_task(task.id, {TaskState.WAITING_PLAN}, max_wait=10)
    start = (await client.get("/")).text
    assert "Waiting for you" in start and "plan awaiting approval in Telegram" in start
    assert "Approve the plan" not in start
    r = await client.post(f"/planes/{task.id}", data={"csrf": p.csrf, "decision": "yes"})
    assert r.status_code in (404, 405)
    assert (await engine.tasks.require(task.id)).state == TaskState.WAITING_PLAN
    # Telegram: approve the plan; the task reaches the cap and is also extended from Telegram.
    await _button(engine, f"plan:{task.id}:yes")
    await engine.wait_task(task.id, {TaskState.PAUSED_CAP}, max_wait=10)
    start = (await client.get("/")).text
    assert "usage cap reached, reply in Telegram" in start
    assert "Continue with a higher cap" not in start
    r = await client.post(f"/topes/{task.id}", data={"csrf": p.csrf, "decision": "more"})
    assert r.status_code in (404, 405)
    await _button(engine, f"cap:{task.id}:more")
    final = await engine.wait_task(task.id, max_wait=10)
    assert final.state == TaskState.COMPLETED


async def test_scheduled_tasks_from_the_panel(panel):
    engine, p, client = panel
    data = {
        "csrf": p.csrf,
        "description": "Check new offers",
        "kind": "weekly",
        "days": "monday, thursday",
        "hour": "09:15",
        "policy": "run_if_recent",
    }
    r = await client.post("/scheduled", data=data)
    # Each action returns to the Scheduled tab of Tasks, with its notice.
    assert r.status_code == 303
    assert r.headers["location"].startswith("/tasks?view=scheduled&notice=")
    assert "kind=ok" in r.headers["location"]
    page = (await client.get(r.headers["location"])).text
    assert "Scheduled: every Monday and Thursday at 09:15." in page
    assert 'href="/tasks?view=scheduled" class="active" aria-current="page"' in page
    row = await engine.db.one("SELECT * FROM scheduled_tasks")
    assert row["timing_text"] == "every Monday and Thursday at 09:15" and row["active"] == 1
    # Edit, pause, resume and delete.
    r = await client.post(
        f"/scheduled/{row['id']}", data={**data, "kind": "daily", "hour": "07:00"}
    )
    assert r.headers["location"].startswith("/tasks?view=scheduled&notice=")
    assert (await engine.db.one("SELECT timing_text FROM scheduled_tasks"))[
        "timing_text"
    ] == "every day at 07:00"
    for action, active in (("pause", 0), ("resume", 1)):
        r = await client.post(f"/scheduled/{row['id']}/{action}", data={"csrf": p.csrf})
        assert r.status_code == 303 and "kind=ok" in r.headers["location"], action
        assert r.headers["location"].startswith("/tasks?view=scheduled&notice="), action
        assert (await engine.db.one("SELECT active FROM scheduled_tasks"))["active"] == active
    r = await client.post(f"/scheduled/{row['id']}/delete", data={"csrf": p.csrf})
    assert r.headers["location"].startswith("/tasks?view=scheduled&notice=")
    assert await engine.db.one("SELECT * FROM scheduled_tasks") is None
    # Invalid data: nothing is saved and it is explained, also in the tab.
    past = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M")
    r = await client.post("/scheduled", data={**data, "kind": "one_time", "date": past})
    assert (
        "kind=error" in r.headers["location"]
        and r.headers["location"].startswith("/tasks?view=scheduled&notice=")
        and await engine.db.one("SELECT * FROM scheduled_tasks") is None
    )
    r = await client.post("/scheduled/999/pause", data={"csrf": p.csrf})
    assert r.headers["location"].startswith("/tasks?view=scheduled&notice=")
    assert "kind=error" in r.headers["location"]
    # Without CSRF or from another origin, nothing changes: the check stays at the usual
    # address.
    assert (await client.post("/scheduled", data=data | {"csrf": "x"})).status_code == 403
    r = await client.post("/scheduled", data=data, headers={"Origin": "http://evil.example"})
    assert r.status_code == 403
    assert await engine.db.one("SELECT * FROM scheduled_tasks") is None


async def test_scheduled_is_a_tasks_tab(panel):
    """Scheduled is the third tab of Tasks, with all its information; the old
    address leads there (with its notice if it has one) and the menu no longer has it."""
    engine, p, client = panel
    await engine.db.run(
        "INSERT INTO scheduled_tasks(description, timing, policy, next_run, timing_text, active, "
        'created) VALUES (\'Weekly reading list\', \'{"kind": "weekly", "days": ["monday"], '
        "\"hour\": \"09:00\"}', 'run_if_recent', '2026-11-02T08:00:00+00:00', "
        "'every Monday at 09:00', 1, '2026-10-20'), ('Paused one', '{\"kind\": \"daily\", "
        "\"hour\": \"18:00\"}', 'skip', NULL, 'every day at 18:00', 0, '2026-10-20')"
    )
    # The old address, with and without parameters (only the notice gets through).
    for params, destination in (
        ({}, "/tasks?view=scheduled"),
        ({"notice": "Done", "kind": "ok"}, "/tasks?view=scheduled&notice=Done&kind=ok"),
        (
            {"notice": "Bad", "kind": "error", "other": "x"},
            "/tasks?view=scheduled&notice=Bad&kind=error",
        ),
    ):
        r = await client.get("/scheduled", params=params)
        assert r.status_code == 303 and r.headers["location"] == destination, params
    html = (await client.get("/tasks?view=scheduled")).text
    assert "<title>Scheduled · Aide</title>" in html
    tabs = re.search(r'<nav class="section-tabs blue"[^>]*>(.*?)</nav>', html, re.S).group(1)
    assert re.findall(r"<span>([^<]+)</span>", tabs) == ["Tasks", "Projects", "Scheduled"]
    assert 'href="/tasks?view=scheduled" class="active" aria-current="page"' in tabs
    assert '<span>Scheduled</span><span class="how_many">2</span>' in tabs
    assert 'class="section blue active"' in html  # the menu highlights Tasks
    assert "Weekly reading list" in html and "every Monday at 09:00" in html
    assert "paused or finished" in html and "Resume" in html and "Delete" in html
    assert 'action="/scheduled"' in html and 'name="csrf"' in html
    # The other tabs do not load the scheduled tasks, but count how many there are.
    tasks = (await client.get("/tasks")).text
    assert "Weekly reading list" not in tasks and '<span class="how_many">2</span>' in tasks
    # Switching tabs without reloading requests only the content.
    partial = await client.get("/tasks?view=scheduled", headers={"X-Partial": "1"})
    assert "<html" not in partial.text and "Weekly reading list" in partial.text
    # No page links to the old addresses anymore (the forms are still submitted there).
    for path in ("/", "/tasks", "/tasks?view=projects", "/tasks?view=scheduled"):
        html = (await client.get(path)).text
        assert 'href="/scheduled' not in html and 'href="/projects"' not in html, path


async def test_tasks_and_projects_together_in_tabs(panel):
    engine, p, client = panel
    project = engine.config.paths.projects / "web-cv"
    project.mkdir(parents=True)
    (project / "CONTEXT.md").write_text("# web-cv\nStatic CV website.")
    html = (await client.get("/tasks")).text
    assert 'class="section-tabs blue"' in html and "<span>Projects</span>" in html
    assert "Static CV website." not in html
    html = (await client.get("/tasks?view=projects")).text
    assert "Static CV website." in html and 'class="section blue active"' in html
    assert " style=" not in html
    r = await client.get("/projects", params={"notice": "Done", "kind": "ok"})
    assert r.status_code == 303
    assert r.headers["location"] == "/tasks?view=projects&notice=Done&kind=ok"
    detail = (await client.get("/projects/web-cv")).text
    assert 'class="section blue active"' in detail and 'href="/tasks?view=projects"' in detail
    # A tab that does not exist falls back to the tasks tab.
    assert "Search request" in (await client.get("/tasks?view=otra")).text


async def test_memory_and_profile_in_the_panel_and_proposals_only_in_telegram(panel):
    engine, p, client = panel
    pid = await engine.memory.propose("The user has lived in Rivertown since November", None)
    page = (await client.get("/profile")).text
    assert "Rivertown" in page and "Save or discard them in Telegram" in page
    assert "Discard" not in page and f"/profile/proposals/{pid}" not in page
    r = await client.post(f"/profile/proposals/{pid}", data={"csrf": p.csrf, "decision": "yes"})
    assert r.status_code in (404, 405)
    assert "Rivertown" not in engine.config.paths.memory.read_text()
    await _button(engine, f"mem:{pid}:yes")
    assert "Rivertown" in engine.config.paths.memory.read_text()
    # Editing the memory and the profile is still possible.
    await client.post(
        "/profile/memory.md",
        data={"csrf": p.csrf, "content": "# Memory\n\n- (2026-10-19) Edited fact\n"},
    )
    assert await engine.memory.search("edited") == ["Edited fact"]
    profile = engine.config.paths.profile.read_text()
    r = await client.post(
        "/profile/profile.md", data={"csrf": p.csrf, "content": profile + "\n- New note"}
    )
    assert (
        "kind=ok" in r.headers["location"] and "New note" in engine.config.paths.profile.read_text()
    )
    long = "\n".join(f"- line {i}" for i in range(200))
    r = await client.post("/profile/profile.md", data={"csrf": p.csrf, "content": long})
    assert "kind=error" in r.headers["location"]  # it is saved, but warns about the 150-line limit


async def test_my_profile_in_cards_and_the_files_collapsed(panel):
    """My profile: each section of profile.md in its card, with the key apart; the
    memory facts with their date, and both files as they are in “Edit by hand”, collapsed."""
    from aide.panel.views import short_date

    engine, p, client = panel
    engine.config.paths.profile.write_text(
        "# Profile\n\n## Who I am\n- Where I live: Rivertown\n- I like tea\n",
        encoding="utf-8",
    )
    await engine.memory.add("The user prefers reports in docx")
    page = (await client.get("/profile")).text
    assert "<h1>My profile</h1>" in page and "<title>My profile · Aide</title>" in page
    assert '<span class="key">Where I live</span>' in page and "Rivertown" in page
    assert 'class="datum loose turquoise"' in page and "I like tea" in page
    done = page.split("What I remember about you")[1]
    assert f'<span class="chip turquoise">{short_date(date.today())}</span>' in done
    assert "The user prefers reports in docx" in done
    edit = page.split("Edit by hand")[0].rsplit("<details", 1)[1]
    assert " open" not in edit
    assert 'action="/profile/profile.md"' in page and 'action="/profile/memory.md"' in page


def test_the_profile_in_sections():
    from aide.panel.views import profile_sections

    text = (
        "# User profile\n\n## Who I am\n- Name: The user\n"
        "- What I do: looking for a job in AI.\n  - Research assistant.\n  - Internships.\n"
        "- Languages: native Spanish,\n  fluent English.\n\n## Nothing yet\n\n"
        "## Writing preferences\n"
        '- No long dashes.\n- No constructions like "Label: explanation".\n'
        "- Format: whatever I ask for.\n- Use «Note: something» at the end.\n\n## Misc\n"
        "A loose paragraph.\n- Find a job in Rivertown in AI, data or cybersecurity: junior.\n"
    )
    a = profile_sections(text)
    assert [x["title"] for x in a] == ["Who I am", "Writing preferences", "Misc"]
    assert [(x["icon"], x["color"]) for x in a] == [
        ("profile", "turquoise"),
        ("write", "peach"),
        ("memory", "green"),  # no known word: the wheel, without repeating the previous one
    ]
    who = a[0]["data"]
    assert [d["key"] for d in who] == ["Name", "What I do", "Languages"]
    assert who[1]["sub"] == ["Research assistant.", "Internships."]
    assert who[2]["value"] == "native Spanish, fluent English."
    redaction = a[1]["data"]
    # A key is short and has no quotes: “Use “Note”” is not.
    assert [d["key"] for d in redaction] == [None, None, "Format", None]
    assert redaction[1]["value"] == 'No constructions like "Label: explanation".'
    assert a[2]["data"][0] == {"key": None, "value": "A loose paragraph.", "sub": []}
    assert a[2]["data"][1]["key"] is None  # a long sentence with “:” is not one either
    assert profile_sections("") == []
    colors = [x["color"] for x in profile_sections("## One\n- a\n## Goals\n- b\n## Three\n- c\n")]
    assert colors == ["blue", "green", "lilac"]  # “Three” does not repeat the previous green
    # Titles in Spanish are also recognized.
    spanish = profile_sections("## Quién soy\n- a\n## Preferencias de redacción\n- b\n")
    assert [x["icon"] for x in spanish] == ["profile", "write"]


def test_the_important_date_of_a_document():
    """The one shown without opening it: the next one (today counts) or, if they have
    already passed, the last one."""
    from aide.panel.views import important_date, short_date, time_left

    def f(date_: str, missing: int) -> dict:
        return {"date": date_, "missing": missing}

    assert important_date([]) is None
    past = [f("2026-09-30", -27), f("2026-01-05", -295)]
    assert important_date(past)["date"] == "2026-09-30"
    assert important_date([*past, f("2027-02-28", 124), f("2026-10-31", 4)])["date"] == (
        "2026-10-31"
    )
    today_and_later = [*past, f("2027-01-01", 66), f("2026-10-27", 0)]
    assert important_date(today_and_later)["date"] == "2026-10-27"  # today's one counts
    assert short_date(date(2026, 10, 31)) == "Oct 31, 2026"
    assert [time_left(n) for n in (0, 1, 4)] == ["today", "tomorrow", "in 4 days"]


def test_inside_a_folder_first_what_expires():
    from aide.panel.views import order_in_folder

    def doc(id_: int, date: str | None = None, missing: int = 0) -> dict:
        return {"id": id_, "important": {"date": date, "missing": missing} if date else None}

    documents = [
        doc(1),
        doc(2, "2027-03-01", 120),
        doc(3, "2026-05-01", -179),
        doc(4, "2026-11-02", 6),
        doc(5),
    ]
    assert [d["id"] for d in sorted(documents, key=order_in_folder)] == [4, 2, 5, 3, 1]


def test_the_description_of_a_project():
    from aide.panel.views import project_description

    context = (
        "# rain-alert\n\n## What it is\n\nA small **Python** script that checks the forecast"
        " from [the weather service](https://weather.example). If it rains, it alerts via"
        " `Telegram`. This third sentence no longer fits because it is quite long and goes over."
        "\n\n```\nNO_RAIN\n```\n"
    )
    assert project_description(context, long=120) == (
        "A small Python script that checks the forecast from the weather service. If it rains,"
        " it alerts via Telegram."
    )
    assert project_description("# just a title\n") == ""
    short = project_description("word " * 60, long=50)
    assert short.endswith("…") and len(short) <= 51
    assert project_description("One. Two. Three.") == "One. Two."  # at most two sentences
    long = "First short sentence. " + "second " * 20 + "."
    assert project_description(long, long=60) == "First short sentence."


async def test_settings_are_validated_and_permissions_are_read_only(panel):
    engine, p, client = panel
    path = engine.config.paths.config / "settings.yaml"
    original = path.read_text()
    r = await client.post(
        "/settings/settings.yaml", data={"csrf": p.csrf, "content": "time_zone: Marte/Olimpo\n"}
    )
    assert "kind=error" in r.headers["location"] and path.read_text() == original
    new = original.replace("simultaneous_heavy_tasks: 1", "simultaneous_heavy_tasks: 2")
    r = await client.post("/settings/settings.yaml", data={"csrf": p.csrf, "content": new})
    assert "kind=ok" in r.headers["location"] and path.read_text() == new
    assert list((engine.config.paths.config / "backups").glob("settings.yaml.*"))
    assert (
        await client.post("/settings/otro.yaml", data={"csrf": p.csrf, "content": ""})
    ).status_code == 404
    # permissions.yaml is visible, but it cannot be saved (not even with a hand-made form).
    permissions = engine.config.paths.config / "permissions.yaml"
    before = permissions.read_text()
    page = (await client.get("/settings")).text
    assert "permissions.yaml" in page and 'action="/settings/permissions.yaml"' not in page
    assert 'action="/settings/models.yaml"' in page and 'action="/settings/settings.yaml"' in page
    r = await client.post(
        "/settings/permissions.yaml",
        data={"csrf": p.csrf, "content": "default_external_mcp_level: 0\n"},
    )
    assert r.status_code == 403 and permissions.read_text() == before


async def test_open_in_finder_only_the_tasks_final_folder(panel, tmp_path):
    engine, p, client = panel
    open_items = []

    async def open_(folder):
        open_items.append(folder)

    p.open_in_finder = open_
    root = engine.config.paths.finished
    final = root / "2026-10-20 internship offers"
    final.mkdir(parents=True)
    task = await engine.tasks.create("Offers", state=TaskState.COMPLETED)
    await engine.tasks.update(task.id, final_folder=str(final))
    detail = (await client.get(f"/task/{task.id}")).text
    assert str(final) in detail and "Open in Finder" in detail
    r = await client.post(f"/task/{task.id}/open", data={"csrf": p.csrf})
    assert r.status_code == 303 and "kind=ok" in r.headers["location"]
    assert open_items == [final.resolve()]
    # Without CSRF, no.
    assert (await client.post(f"/task/{task.id}/open")).status_code == 403
    # A saved path outside the folder (or the root itself, or one that does not exist) is not
    # opened.
    outside = tmp_path / "outside"
    outside.mkdir()
    for path in (str(outside), str(root), str(root / ".." / "config"), str(root / "missing")):
        await engine.tasks.update(task.id, final_folder=path)
        r = await client.post(f"/task/{task.id}/open", data={"csrf": p.csrf})
        assert "kind=error" in r.headers["location"], path
    assert open_items == [final.resolve()]
    assert (
        "kind=error"
        in (await client.post("/task/999/open", data={"csrf": p.csrf})).headers["location"]
    )


async def test_fast_shutdown_with_open_events_and_without_touching_the_signals(panel):
    import signal
    import socket
    import time

    engine, p, _client = panel
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        p.port = s.getsockname()[1]
    before = signal.getsignal(signal.SIGTERM)
    stop = await p.start()
    assert (
        signal.getsignal(signal.SIGTERM) == before
    )  # the application handles SIGTERM, not uvicorn
    base = f"http://127.0.0.1:{p.port}"
    async with httpx.AsyncClient(base_url=base, cookies={COOKIE: TOKEN}, timeout=10) as real:
        assert (await real.get("/")).status_code == 200
        async with real.stream("GET", "/events") as flow:
            assert flow.status_code == 200
            start = time.monotonic()
            await asyncio.wait_for(stop(), timeout=8)
            assert time.monotonic() - start < 6


async def test_open_in_finder_a_project_with_the_same_protection(panel, tmp_path):
    engine, p, client = panel
    open_items = []

    async def open_(folder):
        open_items.append(folder)

    p.open_in_finder = open_
    project = engine.config.paths.projects / "web-cv"
    project.mkdir(parents=True)
    (project / "CONTEXT.md").write_text("# web-cv")
    page = (await client.get("/tasks?view=projects")).text
    assert 'action="/projects/web-cv/open"' in page and "Projects" in page
    assert 'action="/projects/web-cv/open"' in (await client.get("/projects/web-cv")).text
    r = await client.post("/projects/web-cv/open", data={"csrf": p.csrf})
    assert "kind=ok" in r.headers["location"] and open_items == [project.resolve()]
    assert (await client.post("/projects/web-cv/open")).status_code == 403
    # A link that leaves agent/projects, the root, or a project that does not exist: no.
    outside = tmp_path / "outside"
    outside.mkdir()
    (engine.config.paths.projects / "link").symlink_to(outside)
    for name in ("link", "%2E%2E", "no-existe"):
        r = await client.post(f"/projects/{name}/open", data={"csrf": p.csrf})
        assert r.status_code == 404 or "kind=error" in r.headers["location"], name
    assert open_items == [project.resolve()]


def test_link_to_the_projects_at_the_root(tmp_paths):
    tmp_paths.ensure()
    link = tmp_paths.projects_link
    assert link.is_symlink() and link.resolve() == tmp_paths.projects.resolve()
    assert str(link.readlink()) == "agent/projects"  # relative
    tmp_paths.ensure()  # does not fail if it already exists


async def test_scripts_and_maintenance_are_not_edited_from_the_panel(panel):
    engine, p, client = panel
    script = await engine.db.run(
        "INSERT INTO scheduled_tasks(description, timing, policy, active, created, kind, project, command) "
        'VALUES (\'Alert\', \'{"kind": "daily", "hour": "07:30"}\', \'run_if_recent\', 1, '
        "'2026-10-20', 'script', 'alert', '[\"python3\", \"alert.py\"]')"
    )
    system = await engine.db.run(
        "INSERT INTO scheduled_tasks(description, timing, policy, active, created, kind) VALUES "
        '(\'Maintenance\', \'{"kind": "weekly", "days": ["sun"], "hour": "19:00"}\', '
        "'run_always', 1, '2026-10-20', 'maintenance')"
    )
    page = (await client.get("/tasks?view=scheduled")).text
    assert "python3 alert.py" in page and "script, no model" in page
    assert f'action="/scheduled/{script}"' not in page  # no form to edit the command
    data = {
        "csrf": p.csrf,
        "description": "something else",
        "kind": "daily",
        "hour": "08:00",
        "policy": "skip",
    }
    r = await client.post(f"/scheduled/{script}", data=data)
    assert "kind=error" in r.headers["location"]
    row = await engine.db.one("SELECT * FROM scheduled_tasks WHERE id=?", (script,))
    assert row["description"] == "Alert"
    for action in ("pause", "delete"):
        r = await client.post(f"/scheduled/{system}/{action}", data={"csrf": p.csrf})
        assert "kind=error" in r.headers["location"]
    assert (await engine.db.one("SELECT active FROM scheduled_tasks WHERE id=?", (system,)))[
        "active"
    ] == 1


def test_the_timeline_states_the_triage_in_english():
    from aide.panel.views import summarized_timeline

    events = [
        {"kind": "triage", "summary": "new_task", "local_hour": "10:00"},
        {"kind": "triage", "summary": "quick_question", "local_hour": "10:01"},
        {"kind": "triage", "summary": "otro_valor", "local_hour": "10:02"},
    ]
    assert [c["summary"] for c in summarized_timeline(events)] == [
        "new task",
        "quick question",
        "otro valor",
    ]


async def _saved_document(engine: Engine) -> tuple[int, Path]:
    """A saved document, with its file, a date and its entry in the search index."""
    from aide.paperwork import registry

    file = engine.config.paths.documents / "2026" / "20261031-Policy.pdf"
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(b"%PDF-1.4 policy")
    date = {"kind": "expiry", "date": "2027-12-31", "description": "the insurance expires"}
    document_id = await registry.create(
        engine.db,
        {
            "title": "Example Insurance policy",
            "file": "2026/20261031-Policy.pdf",
            "text": "Example Insurance",
        },
        [date],
    )
    await registry.save(engine.db, document_id)
    return document_id, file


async def test_remove_a_document_from_the_panel(panel, fake_trash):
    """With its confirmation, the file goes to Trash and the document no longer appears in
    folders, in search or in notices. Without the panel form nothing is removed."""
    from aide.paperwork import registry

    engine, _p, client = panel
    document_id, file = await _saved_document(engine)
    page = (await client.get("/documents")).text
    assert f'action="/documents/{document_id}/remove"' in page and "data-confirm" in page
    csrf = _csrf(page)
    no_csrf = await client.post(f"/documents/{document_id}/remove", data={"csrf": "other"})
    assert no_csrf.status_code == 403 and file.exists()
    # If it cannot be moved to Trash, nothing is removed.
    fake_trash.fails = True
    r = await client.post(f"/documents/{document_id}/remove", data={"csrf": csrf})
    assert r.status_code == 303 and "kind=error" in r.headers["location"]
    assert file.exists() and (await registry.get(engine.db, document_id))["state"] == "saved"
    fake_trash.fails = False
    r = await client.post(f"/documents/{document_id}/remove", data={"csrf": csrf})
    assert r.status_code == 303 and "Trash" in httpx.URL(r.headers["location"]).params["notice"]
    assert fake_trash.discarded == [file] and not file.exists()
    d = await registry.get(engine.db, document_id)
    assert d["state"] == "discarded" and d["title"] is None and d["text"] is None
    assert await registry.dates_of(engine.db, document_id) == []
    assert await registry.search(engine.db, "Example") == []
    assert await engine.db.value("SELECT COUNT(*) FROM documents_fts") == 0
    assert "Example Insurance policy" not in (await client.get("/documents")).text
    other = await client.post(f"/documents/{document_id}/remove", data={"csrf": csrf})
    assert "already gone" in httpx.URL(other.headers["location"]).params["notice"]


async def test_remove_only_works_for_a_saved_document(panel):
    from aide.paperwork import registry

    engine, _p, _client = panel
    document_id = await registry.create(
        engine.db, {"title": "A proposal", "file": "2026/propuesto.pdf", "text": "x"}, []
    )
    assert await registry.remove(engine.db, document_id) is None
    d = await registry.get(engine.db, document_id)
    assert d["state"] == "proposed" and d["title"] == "A proposal"


async def test_upload_documents_from_the_panel(panel, monkeypatch):
    """What is uploaded goes to the inbox (no folders in the name, without overwriting another
    file) and is asked about in Telegram as with /documents; what cannot be read, is empty
    or is large does not get in."""
    from aide.paperwork import inbox

    engine, _p, client = panel
    questions: list[str] = []

    async def ask(m):
        questions.append("sí")
        return "There are 2 new documents."

    monkeypatch.setattr(inbox, "ask", ask)
    monkeypatch.setattr(inbox, "MAX_UPLOAD_BYTES", 1000)
    page = (await client.get("/documents")).text
    assert 'action="/documents/upload"' in page and 'enctype="multipart/form-data"' in page
    csrf = _csrf(page)
    folder = engine.config.paths.inbox
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Policy.pdf").write_bytes(b"ya estaba")
    files = [
        ("files", ("Policy.pdf", b"%PDF-1.4 nueva", "application/pdf")),
        ("files", ("../../fuera.PDF", b"%PDF-1.4 otra", "application/pdf")),
        ("files", ("virus.exe", b"MZ", "application/octet-stream")),
        ("files", ("vacio.pdf", b"", "application/pdf")),
        ("files", ("grande.pdf", b"0" * 2000, "application/pdf")),
    ]
    no_csrf = await client.post("/documents/upload", data={"csrf": "other"}, files=files)
    assert no_csrf.status_code == 403 and not (folder / "Policy (2).pdf").exists()
    r = await client.post("/documents/upload", data={"csrf": csrf}, files=files)
    notice = httpx.URL(r.headers["location"]).params["notice"]
    assert r.status_code == 303 and notice.startswith("2 documents uploaded to the inbox.")
    assert "virus.exe" in notice and "empty" in notice and "more than 20 MB" in notice
    assert (folder / "Policy.pdf").read_bytes() == b"ya estaba"
    assert (folder / "Policy (2).pdf").read_bytes() == b"%PDF-1.4 nueva"
    assert (folder / "fuera.pdf").is_file() and not (
        engine.config.paths.root / "fuera.pdf"
    ).exists()
    assert sorted(p.name for p in folder.iterdir()) == [
        "Policy (2).pdf",
        "Policy.pdf",
        "fuera.pdf",
    ]
    assert questions == ["sí"]
    # With nothing valid, nothing is uploaded and nothing is asked.
    r = await client.post(
        "/documents/upload",
        data={"csrf": csrf},
        files=[("files", ("notas.xyz", b"x", "text/plain"))],
    )
    assert "kind=error" in r.headers["location"] and questions == ["sí"]


def test_cannot_upload_more_files_than_fit_at_once(tmp_path, monkeypatch):
    from io import BytesIO
    from types import SimpleNamespace

    from aide.paperwork import inbox

    monkeypatch.setattr(inbox, "MAX_UPLOAD_FILES", 2)
    files = [SimpleNamespace(filename=f"{i}.pdf", file=BytesIO(b"%PDF")) for i in range(3)]
    uploaded, rejected = inbox.save_uploaded(tmp_path, files)
    assert [r.name for r in uploaded] == ["0.pdf", "1.pdf"]
    assert rejected == ["“2.pdf” (more than 2 at once)"]
    assert not (tmp_path / "2.pdf").exists()


def test_the_name_of_an_uploaded_file():
    from aide.paperwork.inbox import _safe_name

    assert _safe_name("../../etc/passwd.pdf") == "passwd.pdf"
    assert _safe_name("C:\\Users\\x\\Policy 2027.PDF") == "Policy 2027.pdf"
    assert _safe_name(".oculto.pdf") == "oculto.pdf"
    assert _safe_name("") == "document"
