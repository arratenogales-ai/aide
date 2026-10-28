"""F7: browser without a session (Playwright) and its policy. It uses real Chromium,
without a model."""

from __future__ import annotations

import base64
import functools
import json
import threading
from collections import deque
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aide.core.engine import Engine
from aide.core.policy import APPROVAL, FORBIDDEN, FREE, CallContext, Policy
from aide.core.states import TaskState
from aide.tools.browser import normalize_url

WEB = Path(__file__).parent / "web"


class _Silent(SimpleHTTPRequestHandler):
    def log_message(self, *args) -> None:
        pass


@pytest.fixture(scope="module")
def web_server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Silent, directory=str(WEB)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()


@pytest.fixture
async def tools(config_tmp):
    from aide.tools.server import tools_of

    engine = Engine(config_tmp)
    await engine.start()
    task = await engine.tasks.create("browser test", state=TaskState.RUNNING)
    ses = engine._context(task, "run")
    yield {h.name: h.handler for h in tools_of(ses)}, ses
    await engine.stop()


def _text(result: dict) -> str:
    return "\n".join(c.get("text", "") for c in result["content"] if c["type"] == "text")


def _image(result: dict) -> bytes:
    [image] = [c for c in result["content"] if c["type"] == "image"]
    assert image["mimeType"] == "image/png"
    return base64.b64decode(image["data"])


def test_normalize_url(tmp_path):
    assert normalize_url("example.com") == "https://example.com"
    assert normalize_url("http://127.0.0.1:8000/a") == "http://127.0.0.1:8000/a"
    assert normalize_url(str(tmp_path / "a.html")) == (tmp_path / "a.html").as_uri()


async def test_read_a_page_with_javascript(tools, web_server):
    h, _ = tools
    r = await h["browser_read"]({"url": f"{web_server}/offer.html"})
    data = json.loads(_text(r))
    assert not r.get("is_error") and data["http_status"] == 200
    assert data["title"] and data["text"]


async def test_screenshot_of_local_web_and_of_file(tools, web_server):
    h, ses = tools
    r = await h["browser_screenshot"]({"url_or_local_path": f"{web_server}/form.html"})
    assert _image(r).startswith(b"\x89PNG")
    data = json.loads(_text(r))
    path = Path(data["screenshot"])
    assert path.exists() and path.is_relative_to(ses.folder / "data" / "screenshots")
    assert data["title"] == "Test form"
    # A file in the agent folder, by path.
    page = ses.folder / "results" / "web.html"
    page.write_text("<h1>Hi Alex</h1>", encoding="utf-8")
    r = await h["browser_screenshot"]({"url_or_local_path": str(page), "complete_page": True})
    assert _image(r).startswith(b"\x89PNG")


async def test_try_a_local_form(tools, web_server):
    h, _ = tools
    r = await h["browser_test"](
        {
            "url": f"{web_server}/form.html",
            "steps": [
                {"action": "fill", "selector": "#name", "value": "Alex"},
                {"action": "fill", "selector": "#correo", "value": "usuario@example.com"},
                {"action": "click", "selector": "#enviar"},
                {"action": "check_text", "selector": "#resultado", "value": "Thanks, Alex"},
                {"action": "screenshot"},
            ],
        }
    )
    data = json.loads(_text(r))
    assert data["all_ok"], data["steps"]
    assert data["javascript_errors"] == []
    assert Path(data["final_screenshot"]).exists() and _image(r).startswith(b"\x89PNG")


async def test_testing_detects_errors_and_stops_at_the_first_failure(tools, web_server):
    h, _ = tools
    r = await h["browser_test"](
        {
            "url": f"{web_server}/error.html",
            "steps": [
                {"action": "check_text", "selector": "#value", "value": "42"},
                {"action": "click", "selector": "#no-se-llega"},
            ],
        }
    )
    data = json.loads(_text(r))
    assert not data["all_ok"] and len(data["steps"]) == 1 and not data["steps"][0]["ok"]
    assert any("funcionQueNoExiste" in e for e in data["javascript_errors"])


async def test_nonexistent_page_gives_a_readable_error(tools):
    h, _ = tools
    r = await h["browser_read"]({"url": "http://127.0.0.1:9/nada"})
    assert r.get("is_error") and "Couldn't read" in _text(r)


# --- policy -----------------------------------------------------------------------------------


def test_browser_policy_without_a_session(config_tmp):
    policy = Policy(config_tmp)
    ctx = CallContext(task_id=1, task_folder=config_tmp.paths.task(1))

    def level(tool: str, entry: dict) -> int:
        return policy.evaluate(f"mcp__aide__{tool}", entry, ctx).level

    assert level("browser_read", {"url": "https://www.admin.ch"}) == FREE
    assert level("browser_read", {"url": "mail.google.com"}) == APPROVAL
    assert (
        level("browser_screenshot", {"url_or_local_path": "https://calendar.google.com/x"})
        == APPROVAL
    )
    panel = f"http://127.0.0.1:{config_tmp.panel_port}/"
    assert level("browser_screenshot", {"url_or_local_path": panel}) == FORBIDDEN
    env = str(config_tmp.paths.root / ".env")
    assert level("browser_screenshot", {"url_or_local_path": env}) == FORBIDDEN
    assert level("browser_screenshot", {"url_or_local_path": f"file://{env}"}) == FORBIDDEN
    local = {"url": "http://127.0.0.1:8000/", "steps": [{"action": "click", "selector": "#a"}]}
    assert level("browser_test", local) == FREE
    file = {
        "url": str(config_tmp.paths.agent / "x.html"),
        "steps": [{"action": "click", "selector": "#a"}],
    }
    assert level("browser_test", file) == FREE
    external = {
        "url": "https://example.com",
        "steps": [{"action": "fill", "selector": "#a", "value": "x"}],
    }
    assert level("browser_test", external) == APPROVAL
    only_read = {
        "url": "https://example.com",
        "steps": [{"action": "check_text", "value": "x"}],
    }
    assert level("browser_test", only_read) == FREE
    # A “go” step to an outside address also counts.
    jump = {
        "url": "http://127.0.0.1:8000/",
        "steps": [
            {"action": "go", "value": "https://example.com"},
            {"action": "click", "selector": "#a"},
        ],
    }
    assert level("browser_test", jump) == APPROVAL
    to_panel = {"url": "http://127.0.0.1:8000/", "steps": [{"action": "go", "value": panel}]}
    assert level("browser_test", to_panel) == FORBIDDEN
    to_email = {
        "url": "http://127.0.0.1:8000/",
        "steps": [{"action": "go", "value": "https://mail.google.com"}],
    }
    assert level("browser_test", to_email) == APPROVAL
    # To be safe, steps with English names count the same (the tool does not accept them, but
    # the policy does not fall short if the model uses them).
    in_english = {
        "url": "https://example.com",
        "steps": [{"action": "click", "selector": "#a"}],
    }
    assert level("browser_test", in_english) == APPROVAL
    go_panel = {"url": "http://127.0.0.1:8000/", "steps": [{"action": "go", "value": panel}]}
    assert level("browser_test", go_panel) == FORBIDDEN


# --- real Chrome: pause and resume (simulated SDK) -----------------------------------------


@pytest.fixture
async def chrome_environment(config_tmp, monkeypatch):
    from aide.core.orchestrator import AgentSession
    from aide.simulate import Script, Simulator
    from tests.fake_sdk import FakeClient, Recorder

    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    engine = Engine(config_tmp)
    available = {"chrome": False}

    async def ready_chrome() -> bool:
        return available["chrome"]

    engine.ready_chrome = ready_chrome
    await engine.start()
    sim = Simulator(engine, Script(), print_out=False)
    yield engine, sim, available
    await engine.stop()


async def test_without_chrome_the_task_waits_and_resumes_when_it_opens(chrome_environment):
    from tests.fake_sdk import End, FakeClient, Tool

    engine, sim, available = chrome_environment
    FakeClient.script = lambda p, o, r: [
        Tool("mcp__claude-in-chrome__navigate", {"url": "https://example.com"}, lambda e: "ok"),
        End("<delivery>Data extracted.</delivery>"),
    ]
    task = await engine.create_direct_task("Look at example.com in my Chrome", needs_browser=True)
    await sim.wait_rest()
    task = await engine.tasks.require(task.id)
    assert task.state == TaskState.PAUSED_BROWSER
    assert FakeClient.registry.queries == []  # no session was ever opened
    assert any("needs your Chrome" in t for t in sim.channel.texts())
    # Without Chrome, the periodic check does nothing.
    assert await engine.check_browser() == []
    available["chrome"] = True
    assert await engine.check_browser() == [task.id]
    await sim.wait_rest()
    assert (await engine.tasks.require(task.id)).state == TaskState.COMPLETED
    assert FakeClient.registry.queries[0].startswith("Task ")  # initial prompt, not a resume one
    assert any("Chrome is available again" in t for t in sim.channel.texts())
    assert FakeClient.registry.options[-1].extra_args.get("chrome", "falta") is None


async def test_extension_that_disconnects_midway_pauses_after_one_retry(chrome_environment):
    from tests.fake_sdk import End, FakeClient, Tool

    engine, sim, available = chrome_environment
    available["chrome"] = True
    outage = "Browser extension is not connected. Please make sure Chrome is open."

    def script(prompt, options, registry):
        if "is resuming now" in prompt:
            return [
                Tool("mcp__claude-in-chrome__get_page_text", {"tabId": 1}, lambda e: "text"),
                End("<delivery>Finished after resuming.</delivery>"),
            ]
        return [
            Tool("mcp__claude-in-chrome__navigate", {"url": "https://example.com"}, lambda e: "ok"),
            Tool("mcp__claude-in-chrome__get_page_text", {"tabId": 1}, lambda e: outage),
            Tool("mcp__claude-in-chrome__get_page_text", {"tabId": 1}, lambda e: outage),
            End("<delivery>I couldn't finish.</delivery>"),
        ]

    FakeClient.script = script
    task = await engine.create_direct_task(
        "Pull data from example.com in my Chrome", needs_browser=True
    )
    await sim.wait_rest()
    assert (await engine.tasks.require(task.id)).state == TaskState.PAUSED_BROWSER
    assert "down_browser" in [e["kind"] for e in await engine.tasks.events(task.id)]
    assert await engine.check_browser() == [task.id]
    await sim.wait_rest()
    assert (await engine.tasks.require(task.id)).state == TaskState.COMPLETED


async def test_a_stray_chrome_failure_does_not_pause(chrome_environment):
    from tests.fake_sdk import End, FakeClient, Tool

    engine, sim, available = chrome_environment
    available["chrome"] = True
    FakeClient.script = lambda p, o, r: [
        Tool(
            "mcp__claude-in-chrome__get_page_text",
            {"tabId": 1},
            lambda e: "Receiving end does not exist",
        ),
        Tool("mcp__claude-in-chrome__get_page_text", {"tabId": 1}, lambda e: "Title: Example"),
        End("<delivery>Done.</delivery>"),
    ]
    task = await engine.create_direct_task("Read the tab in my Chrome", needs_browser=True)
    await sim.wait_rest()
    assert (await engine.tasks.require(task.id)).state == TaskState.COMPLETED
    assert "down_browser" not in [e["kind"] for e in await engine.tasks.events(task.id)]


async def test_real_chrome_does_not_open_email_or_banks_without_permission(chrome_environment):
    from tests.fake_sdk import End, FakeClient, Tool

    engine, sim, available = chrome_environment
    available["chrome"] = True
    open_items = []

    def open_(entry):
        open_items.append(entry["url"])
        return "ok"

    FakeClient.script = lambda p, o, r: [
        Tool(
            "mcp__claude-in-chrome__navigate", {"url": "https://mail.google.com/mail/u/0/"}, open_
        ),
        Tool(
            "mcp__claude-in-chrome__navigate", {"url": "https://www.northbank.example/login"}, open_
        ),
        Tool(
            "mcp__claude-in-chrome__tabs_create_mcp", {"url": "https://calendar.google.com"}, open_
        ),
        End("<delivery>Done.</delivery>"),
    ]
    sim.script.default_approve = "no"
    task = await engine.create_direct_task("Open my email in Chrome", needs_browser=True)
    await sim.wait_rest()
    assert open_items == []  # not email, not the bank, not the calendar
    results = [r for h, r in FakeClient.registry.tools_results]
    assert all(r.startswith("DENEGADA") for r in results)
    rows = await engine.db.everything("SELECT state FROM approvals WHERE task_id=?", (task.id,))
    assert [f["state"] for f in rows] == [
        "rejected",
        "rejected",
    ]  # the bank is not even asked about


async def test_screenshot_is_valid_if_nothing_changed_the_page_afterwards(
    chrome_environment, tmp_path
):
    """A submit is approved with a loose screenshot of its tab, taken after the last change to
    that page."""
    import base64 as b64

    from aide.core.context import SessionContext
    from aide.core.hooks import create_hooks
    from aide.core.states import TaskState

    engine, _, _ = chrome_environment
    task = await engine.tasks.create("Fill in the form", state=TaskState.COMPLETED)
    ses = SessionContext(core=engine.core, task_id=task.id, folder=tmp_path, role="orchestrator")
    hooks = create_hooks(ses)
    post = hooks["PostToolUse"][0].hooks[0]
    failure = hooks["PostToolUseFailure"][0].hooks[0]
    data = b64.b64encode(b"\xff\xd8\xff\xe0captura").decode()
    image = [
        {"type": "image", "source": {"type": "base64", "type_mean": "image/jpeg", "data": data}}
    ]

    async def step(name, entry, reply=None):
        call = {
            "tool_name": "mcp__claude-in-chrome__" + name,
            "tool_input": entry,
            "tool_response": reply or [{"type": "text", "text": "ok"}],
        }
        await post(call, None, None)

    async def screenshot(tab=1):
        await step("computer", {"action": "screenshot", "tabId": tab}, image)

    def batch(*actions):
        return {"actions": [{"name": "computer", "input": a} for a in actions]}

    await screenshot()
    assert "1" in ses.screenshots
    await step("get_page_text", {"tabId": 1})
    await step("computer", {"action": "wait", "duration": 1, "tabId": 1})
    assert "1" in ses.screenshots  # reading or waiting does not change the page
    await step("form_input", {"ref": "ref_2", "value": "x", "tabId": 1})
    assert "1" not in ses.screenshots
    # The screenshot of another tab, a zoom or an image from inside a batch do not count.
    await screenshot(tab=2)
    await step("computer", {"action": "zoom", "region": [0, 0, 10, 10], "tabId": 1}, image)
    await step("browser_batch", batch({"action": "screenshot", "tabId": 1}), image)
    assert set(ses.screenshots) == {"2"}
    # A batch that changes the page invalidates its screenshot, even if it brings images.
    await screenshot()
    click = {"action": "left_click", "ref": "ref_1", "tabId": 1}
    await step("browser_batch", batch(click, {"action": "screenshot", "tabId": 1}), image)
    assert "1" not in ses.screenshots
    # A page whose text looks like a connection error also counts as a change.
    await screenshot()
    page = [{"type": "text", "text": 'tabId 1: "Not connected? WiFi mug"'}]
    await step("navigate", {"url": "https://example.com/wifi-mug", "tabId": 1}, page)
    assert "1" not in ses.screenshots
    # A failing action may have half changed the page.
    await screenshot()
    await failure(
        {"tool_name": "mcp__claude-in-chrome__computer", "tool_input": click, "error": "timeout"},
        None,
        None,
    )
    assert "1" not in ses.screenshots
    # Switching browser applies to all tabs.
    await screenshot()
    await screenshot(tab=2)
    await step("switch_browser", {})
    assert ses.screenshots == {}


async def test_real_chrome_submit_needs_approval_and_stale_screenshot_or_ref_fail(
    chrome_environment,
):
    import base64 as b64

    from tests.fake_sdk import End, FakeClient, Tool
    from tests.test_navigation import READ_PAGE

    engine, sim, available = chrome_environment
    available["chrome"] = True
    pressed = []
    jpeg = b"\xff\xd8\xff\xe0captura"

    def press(entry):
        pressed.append(entry.get("ref") or entry.get("action"))
        return [{"type": "text", "text": "Clicked"}]

    c = "mcp__claude-in-chrome__"
    page = {"tabId": 11}
    FakeClient.script = lambda p, o, r: [
        Tool(
            c + "navigate",
            {"url": "http://127.0.0.1:8731/form.html", **page},
            lambda e: "ok",
        ),
        Tool(
            c + "read_page",
            {"filter": "interactive", **page},
            lambda e: [{"type": "text", "text": READ_PAGE}],
        ),
        Tool(c + "form_input", {"ref": "ref_1", "value": "Alex", **page}, lambda e: "ok"),
        Tool(c + "form_input", {"ref": "ref_5", "value": "secreto", **page}, lambda e: "MAL"),
        Tool(
            c + "computer",
            {"action": "screenshot", **page},
            lambda e: [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "type_mean": "image/jpeg",
                        "data": b64.b64encode(jpeg).decode(),
                    },
                }
            ],
        ),
        Tool(c + "computer", {"action": "left_click", "ref": "ref_4", **page}, press),
        Tool(c + "computer", {"action": "left_click", "ref": "ref_3", **page}, press),
        # The click changed the page: without a new screenshot, the submit is not even asked
        # about.
        Tool(c + "computer", {"action": "left_click", "ref": "ref_4", **page}, press),
        Tool(
            c + "computer",
            {"action": "screenshot", **page},
            lambda e: [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "type_mean": "image/jpeg",
                        "data": b64.b64encode(jpeg).decode(),
                    },
                }
            ],
        ),
        # After the click, ref_4 is no longer identified: approval is requested again.
        Tool(c + "computer", {"action": "left_click", "ref": "ref_4", **page}, press),
        End("<delivery>Form filled in without sending.</delivery>"),
    ]
    sim.script.approve = deque(["no", "no"])
    task = await engine.create_direct_task(
        "Fill in the local form in my Chrome", needs_browser=True
    )
    await sim.wait_rest()
    assert pressed == ["ref_3"]  # only the click on a text field; the submit button, never
    denied = [
        r
        for h, r in FakeClient.registry.tools_results
        if isinstance(r, str) and r.startswith("DENEGADA")
    ]
    assert any("passwords" in d for d in denied)  # ref_5 is a password field
    assert any("take a screenshot" in d for d in denied)
    rows = await engine.db.everything(
        "SELECT kind, detail, state FROM approvals WHERE task_id=? ORDER BY id", (task.id,)
    )
    assert [(f["kind"], f["state"]) for f in rows] == [
        ("chrome_send", "rejected"),
        ("chrome_send", "rejected"),
    ]
    assert "Send request" in rows[0]["detail"]
    screenshots = [m for m in sim.channel.messages if m.kind == "image"]
    assert (
        screenshots
        and screenshots[0].path.suffix == ".jpg"
        and screenshots[0].path.read_bytes() == jpeg
    )
    assert screenshots[0].path.is_relative_to(engine.config.paths.task(task.id))
