"""F2: Telegram with simulated tests (commands, buttons, files, single user)."""

from __future__ import annotations

import datetime as dt
import logging
from collections import deque
from types import SimpleNamespace

import pytest
from telegram import Chat, Message, Update, User

from aide.bot.channel import Entry
from aide.bot.commands import run_command
from aide.bot.telegram import TelegramBot, TelegramChannel, entry_from_message, is_allowed
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.core.states import TaskState
from aide.simulate import Script, Simulator
from tests.fake_sdk import End, FakeClient, Recorder, Wait

USER = 7_000_001
INTRUDER = 9_999_999


def _message(
    text: str, user: int = USER, message_id: int = 10, replies_to: int | None = None
) -> Update:
    chat = Chat(id=user, type="private")
    previous = Message(replies_to, dt.datetime.now(dt.UTC), chat) if replies_to else None
    m = Message(
        message_id,
        dt.datetime.now(dt.UTC),
        chat,
        from_user=User(user, "The user", False),
        text=text,
        reply_to_message=previous,
    )
    return Update(update_id=message_id, message=m)


@pytest.fixture
async def environment(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    FakeClient.script = staticmethod(lambda p, o, r: [End("<delivery>Hecho.</delivery>")])
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    engine = Engine(config_tmp)
    await engine.start()
    engine.commands = run_command
    sim = Simulator(engine, Script(), print_out=False)
    yield engine, sim
    await engine.stop()


async def command(engine: Engine, text: str) -> None:
    name, _, rest = text.lstrip("/").partition(" ")
    await engine.receive(Entry("command", USER, text=text, command=name, arguments=rest))


# --- message conversion and single user ----------------------------------------------------


def test_text_input_command_and_reply():
    e = entry_from_message(_message("hi"))
    assert (e.kind, e.text, e.user_id) == ("text", "hi", USER)
    c = entry_from_message(_message("/task 12"))
    assert (c.kind, c.command, c.arguments) == ("command", "task", "12")
    c_2 = entry_from_message(_message("/status@AideBot"))
    assert c_2.command == "status"
    r = entry_from_message(_message("go on", replies_to=55))
    assert r.replies_to == 55


def test_user_not_allowed():
    assert is_allowed(_message("hi"), USER)
    assert not is_allowed(_message("hi", user=INTRUDER), USER)
    assert not is_allowed(_message("hi"), None)


async def test_bot_ignores_and_logs_an_intruder(config_tmp, caplog):
    received = []

    class FakeEngine:
        async def receive(self, entry):
            received.append(entry)

    bot = TelegramBot(
        "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        USER,
        FakeEngine(),
        config_tmp.paths.agent_cache,
    )
    with caplog.at_level(logging.WARNING):
        await bot._message(_message("give me the user's email", user=INTRUDER), None)
    assert received == []
    assert bot.ignored == 1
    assert "not allowed" in caplog.text
    await bot._message(_message("hi", user=USER), None)
    assert len(received) == 1 and received[0].text == "hi"


async def test_bot_intruder_button_does_not_reach_the_engine(config_tmp):
    received = []

    class FakeEngine:
        async def receive(self, entry):
            received.append(entry)

    bot = TelegramBot(
        "123456789:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA",
        USER,
        FakeEngine(),
        config_tmp.paths.agent_cache,
    )
    for user, expected in ((INTRUDER, 0), (USER, 1)):
        answered: list[bool] = []

        async def answer(*a, _r=answered, **k):
            _r.append(True)

        # PTB objects are immutable: the update is simulated with what the bot uses.
        query = SimpleNamespace(data="apr:1:yes", answer=answer)
        update = SimpleNamespace(
            callback_query=query, effective_user=SimpleNamespace(id=user, username="x")
        )
        await bot._button(update, None)
        assert len(received) == expected
        assert answered  # the button is always answered to remove the loading indicator
    assert received[0].button_datum == "apr:1:yes"


# --- Telegram channel: HTML, buttons and files ---------------------------------------------


class FakeBot:
    def __init__(self, fail_html: bool = False):
        self.calls = []
        self.fail_html = fail_html

    async def send_message(self, **kw):
        from telegram.error import BadRequest

        self.calls.append(("send_message", kw))
        if self.fail_html and kw.get("parse_mode"):
            raise BadRequest("Can't parse entities")
        return SimpleNamespace(message_id=len(self.calls))

    async def send_document(self, **kw):
        self.calls.append(("send_document", kw))
        return SimpleNamespace(message_id=len(self.calls))

    async def send_chat_action(self, **kw):
        self.calls.append(("send_chat_action", kw))


async def test_channel_sends_html_with_buttons():
    from aide.bot.channel import Button

    bot = FakeBot()
    channel = TelegramChannel(bot, USER)
    mid = await channel.send("<b>Hi</b> &amp; bye", buttons=[[Button("Approve", "apr:1:yes")]])
    name, kw = bot.calls[0]
    assert mid == 1 and kw["parse_mode"] == "HTML" and kw["chat_id"] == USER
    assert kw["reply_markup"].inline_keyboard[0][0].callback_data == "apr:1:yes"
    await channel.writing()
    assert bot.calls[-1][1]["action"] == "typing"


async def test_channel_retries_in_plain_text_if_html_fails():
    bot = FakeBot(fail_html=True)
    channel = TelegramChannel(bot, USER)
    await channel.send("<b>roto")
    assert len(bot.calls) == 2 and "parse_mode" not in bot.calls[1][1]


async def test_channel_sends_file(tmp_path):
    bot = FakeBot()
    path = tmp_path / "report.md"
    path.write_text("content")
    await TelegramChannel(bot, USER).send_file(path, "Report")
    name, kw = bot.calls[0]
    assert name == "send_document" and kw["filename"] == "report.md" and kw["caption"] == "Report"


# --- commands ------------------------------------------------------------------------------


async def test_start_and_help(environment):
    engine, sim = environment
    await command(engine, "/start")
    await command(engine, "/help")
    texts = sim.channel.texts()
    assert "I'm Aide" in texts[0] and "/status" in texts[0]
    assert "/scheduled" in texts[1]


async def test_unknown(environment):
    engine, sim = environment
    await command(engine, "/inventado")
    assert "I don't know the command /inventado" in sim.channel.texts()[-1]
    assert "/help" in sim.channel.texts()[-1]


async def test_status_tasks_and_task(environment):
    engine, sim = environment
    await sim.send("First task")
    await command(engine, "/status")
    assert "Nothing running or pending" in sim.channel.texts()[-1]
    await command(engine, "/tasks")
    assert "1. First task (completed)" in sim.channel.texts()[-1]
    await command(engine, "/task 1")
    detail = sim.channel.texts()[-1]
    assert "Task 1 · Completed" in detail and "Usage: " in detail
    # With no delivered file there is no final folder.
    assert "Saved in Finished Tasks/" not in detail and "USD" not in detail
    await command(engine, "/task 99")
    assert "Task 99 doesn't exist" in sim.channel.texts()[-1]


async def test_stop_pause_and_resume(environment):
    engine, sim = environment
    FakeClient.script = staticmethod(lambda p, o, r: [Wait(5), End("<delivery>Hecho.</delivery>")])
    task = await engine.create_direct_task("Slow task", origin="test")
    for _ in range(60):
        e = engine.in_progress.get(task.id)
        if e and e.session and e.session.in_turn:
            break
        import asyncio

        await asyncio.sleep(0.05)
    await command(engine, f"/pause {task.id}")
    await sim.wait_rest()
    paused = await engine.tasks.require(task.id)
    assert paused.state == TaskState.PAUSED_SHUTDOWN and paused.pause_reason == "paused by the user"
    FakeClient.script = staticmethod(lambda p, o, r: [End("<delivery>Terminada.</delivery>")])
    await command(engine, f"/resume {task.id}")
    await sim.wait_rest()
    assert (await engine.tasks.require(task.id)).state == TaskState.COMPLETED
    assert any("interrupted" in c for c in FakeClient.registry.queries)
    sim.script.plan = deque()
    sim.script.default_plan = "nothing"  # the plan is not pressed: it stays waiting
    other = await engine.create_direct_task("Another one", origin="test", needs_plan=True)
    await sim.wait_rest()
    assert (await engine.tasks.require(other.id)).state == TaskState.WAITING_PLAN
    await command(engine, f"/stop {other.id}")
    assert (await engine.tasks.require(other.id)).state == TaskState.CANCELLED


async def test_plan_forces_prior_plan(environment):
    engine, sim = environment

    def script(p, o, r):
        if "PLAN PHASE" in p:
            return [End("<plan>Objetivo: x.</plan>")]
        return [End("<delivery>Hecho.</delivery>")]

    FakeClient.script = staticmethod(script)
    await command(engine, "/plan Organize my notes")
    await sim.wait_rest()
    task = (await engine.tasks.list_items(limit=1))[0]
    assert task.needs_plan == 1 and task.state == TaskState.COMPLETED
    assert any("Plan for task" in t for t in sim.channel.texts())


async def test_forced_models_and_maximum_with_confirmation(environment):
    engine, sim = environment
    await command(engine, "/sonnet")
    assert engine.next_model == "sonnet"
    task = await engine.create_direct_task("Something", origin="test")
    await sim.wait_rest()
    assert (await engine.tasks.require(task.id)).model == "claude-sonnet-5-5"
    assert engine.next_model is None  # only affects the next task
    await command(engine, "/max")
    question = sim.channel.messages[-1]
    assert "max:yes" in question.buttons_data and engine.next_model is None
    await engine.receive(Entry("button", USER, button_datum="max:yes"))
    assert engine.next_model == "maximum"
    assert await engine.db.kv_read("next_model") == "maximum"


async def test_usage(environment):
    engine, sim = environment
    await sim.send("Task with usage")
    await command(engine, "/usage")
    # The text and, afterwards, the usage chart as an image.
    assert sim.channel.messages[-1].kind == "image"
    text = sim.channel.texts()[-2]
    assert "Plan limits" in text and "Usage per task, last 7 days" in text
    assert "1. Task with usage · 150 tokens" in text and "Agent usage today: 150 tokens" in text
    assert "USD" not in text


async def test_memory_and_forget(environment):
    engine, sim = environment
    await engine.memory.add("The user prefers reports as PDF")
    await engine.memory.add("German class is on Tuesdays")
    await command(engine, "/memory")
    assert "reports as PDF" in sim.channel.texts()[-1]
    await command(engine, "/forget German")
    question = sim.channel.messages[-1]
    datum = next(d for d in question.buttons_data if d.startswith("forget:") and d.endswith(":yes"))
    await engine.receive(Entry("button", USER, button_datum=datum))
    assert [h.text for h in engine.memory.facts()] == ["The user prefers reports as PDF"]
    await command(engine, "/forget nonexistent")
    assert "I can't find any fact" in sim.channel.texts()[-1]


async def test_schedule_and_scheduled(environment):
    engine, sim = environment
    await command(engine, "/scheduled")
    assert "No scheduled tasks" in sim.channel.texts()[-1]
    await command(engine, "/schedule every Monday at 9 check my email")
    await sim.wait_rest()
    task = (await engine.tasks.list_items(limit=1))[0]
    assert "schedule_task" in task.request and "every Monday" in task.request


async def test_profile_and_panel(environment):
    engine, sim = environment
    await command(engine, "/profile")
    assert "User profile" in sim.channel.texts()[-1]
    await command(engine, "/panel")
    assert "http://127.0.0.1:8765" in sim.channel.texts()[-1]


async def test_reply_to_a_task_message_continues_the_task(environment):
    engine, sim = environment
    [task_id] = await sim.send("First version")
    FakeClient.script = staticmethod(
        lambda p, o, r: [End("<delivery>Second version done.</delivery>")]
    )
    affected = await sim.send("Change its title", replies_to_task=task_id)
    assert affected == [task_id]  # no new task is created
    assert any(
        "New message from the user" in c and "Change its title" in c
        for c in FakeClient.registry.queries
    )
    assert (await engine.tasks.require(task_id)).state == TaskState.COMPLETED


async def test_a_new_message_does_not_reopen_a_finished_task(environment, tmp_path):
    """A message that does not reply to a finished task does not reopen it even if the triage takes it
    for a continuation: it is a new task, no loops. Only a direct reply continues it (on 22-10
    a PDF reopened task 14 and re-entered nine times)."""
    from aide.core.engine import Triage

    engine, sim = environment
    [finished] = await sim.send("First version")
    cancelled = (
        await engine.tasks.create("Something that was canceled", state=TaskState.CANCELLED)
    ).id
    calls: list[str] = []
    related = {"id": finished}

    async def triage(text, recent, task_id):
        calls.append(text)
        return Triage(kind="continuation", related_task=related["id"])

    engine.triage = triage
    pdf = tmp_path / "essay.pdf"
    pdf.write_bytes(b"%PDF-1.4")

    # A PDF with its caption, not replying to anything: a new task, with the PDF, and the
    # finished one is not touched. (Without a caption, it asks what to do with it
    # instead of creating a task.)
    affected = await sim.send("Review this essay", files=[pdf])
    assert len(affected) == 1 and len(calls) == 1
    new = await engine.tasks.require(affected[0])
    assert new.kind == "task" and new.parent_task is None
    assert (engine.config.paths.task(new.id) / "input" / "essay.pdf").exists()
    assert await engine.tasks.events(finished, ["user_message"]) == []
    assert (await engine.tasks.require(finished)).state == TaskState.COMPLETED

    # A direct reply to a cancelled task: goes to a new task and the triage, even if it insists
    # on the cancelled one, does not send it back to it in a loop.
    related["id"] = cancelled
    calls.clear()
    affected = await sim.send("Do it again", replies_to_task=cancelled)
    new_items = [t for t in affected if t != cancelled]
    assert len(new_items) == 1 and len(calls) == 1
    assert (await engine.tasks.require(cancelled)).state == TaskState.CANCELLED

    # A direct reply to a message of the finished task: that one does continue it, without
    # triage.
    calls.clear()
    affected = await sim.send("Change its title", replies_to_task=finished)
    assert affected == [finished] and calls == []
    assert await engine.tasks.events(finished, ["user_message"])


def test_triage_only_continues_running_tasks():
    from aide.core.tasks import Task
    from aide.core.triage import TriageData, apply_rules

    def task(id_, state):
        return SimpleNamespace(id=id_, state=state, state_enum=TaskState(state))

    recent: list[Task] = [
        task(1, "completed"),
        task(2, "paused_cap"),
        task(3, "running"),
        task(4, "cancelled"),
    ]
    base = dict(
        kind="continuation",
        size="small",
        external_actions=False,
        needs_plan=False,
        needs_real_browser=False,
        title="x",
    )
    for id_, expected in ((1, "new_task"), (2, "new_task"), (4, "new_task")):
        t = apply_rules(TriageData(**base, related_task=id_), recent, "go on")
        assert (t.kind, t.related_task) == (expected, None), id_
    t = apply_rules(TriageData(**base, related_task=3), recent, "add this")
    assert (t.kind, t.related_task) == ("continuation", 3)


async def test_voice_note_without_transcriber_warns(environment, tmp_path):
    engine, sim = environment
    audio = tmp_path / "voz.ogg"
    audio.write_bytes(b"OggS")
    await engine.receive(Entry("voice", USER, files=[audio]))
    assert "can't transcribe" in sim.channel.texts()[-1]


async def test_voice_note_with_transcriber(environment, tmp_path):
    engine, sim = environment

    async def transcribe(path):
        return "Make a shopping list"

    engine.transcribe = transcribe
    audio = tmp_path / "voz.ogg"
    audio.write_bytes(b"OggS")
    await engine.receive(Entry("voice", USER, files=[audio]))
    await sim.wait_rest()
    assert 'Transcript: "Make a shopping list"' in sim.channel.texts()
    task = (await engine.tasks.list_items(limit=1))[0]
    assert task.request == "Make a shopping list"


async def test_document_is_saved_in_input(environment, tmp_path):
    engine, sim = environment
    doc = tmp_path / "cv.pdf"
    doc.write_bytes(b"%PDF-1.4")
    affected = await sim.send("Review my resume", files=[doc])
    task_id = affected[0]
    saved = engine.config.paths.task(task_id) / "input" / "cv.pdf"
    assert saved.read_bytes() == b"%PDF-1.4"
    assert any(str(saved) in c for c in FakeClient.registry.queries)


async def test_chat_not_found_does_not_bring_down_the_agent(caplog):
    """A Telegram failure (for example, the bot not yet started) is logged with a hint."""
    from telegram.error import BadRequest

    from aide.bot.channel import GuardedChannel

    class BotWithoutChat(FakeBot):
        async def send_message(self, **kw):
            self.calls.append(("send_message", kw))
            raise BadRequest("Chat not found")

    bot = BotWithoutChat()
    channel = GuardedChannel(TelegramChannel(bot, USER))
    with caplog.at_level(logging.ERROR):
        result = await channel.send("<b>Notice</b>")
    assert result is None
    assert len(bot.calls) == 1  # no retries or plain text: it is not a formatting error
    assert "tap Start" in caplog.text
