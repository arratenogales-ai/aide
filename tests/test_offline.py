"""Days without wifi and without connection: no loops of errors, no retries that
spend tokens, no notices piling up on return, scheduled tasks that are skipped,
maintenance that waits for the next connection, Chrome only if the user asks and email
every 30 minutes. No network: the connection is simulated."""

from __future__ import annotations

import asyncio
import logging
import socket
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import httpx
import pytest
from telegram.error import BadRequest, NetworkError, TimedOut

from aide.bot.channel import Entry, FakeChannel, GuardedChannel
from aide.bot.commands import run_command
from aide.core import engine as modulo_motor
from aide.core.connection import Connection, is_network_error
from aide.core.engine import (
    NOT_NOTIFIED_KEY,
    OFFLINE_REASON,
    PENDING_MAINTENANCE_KEY,
    Engine,
    requested_chrome,
)
from aide.core.orchestrator import AgentSession, TurnResult
from aide.core.states import TaskState
from tests.fake_sdk import End, FakeClient, Recorder


@pytest.fixture
async def engine(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    # As in production: the channel is wrapped in GuardedChannel, which watches the connection.
    m = Engine(config_tmp, channel=GuardedChannel(FakeChannel(print_out=False)))
    m.channel.connection = m.connection
    await m.start(workers=False)
    m.commands = run_command
    yield m
    await m.stop()


def no_network(engine: Engine) -> None:
    async def test() -> bool:
        return False

    engine.connection._test = test
    engine.connection.mark_offline()


def with_network(engine: Engine) -> None:
    async def test() -> bool:
        return True

    engine.connection._test = test
    engine.connection.mark_online()


async def background(engine: Engine) -> None:
    """Waits for what the engine launched in the background (maintenance)."""
    while engine._background:
        await asyncio.gather(*list(engine._background), return_exceptions=True)


def messages(engine: Engine) -> list:
    """What has reached the user (the fake channel, inside GuardedChannel)."""
    return engine.channel.channel.messages


def texts(engine: Engine) -> list[str]:
    return [m.plain_text for m in messages(engine) if m.kind == "text"]


async def scheduled_task(engine: Engine, **fields) -> int:
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat(timespec="seconds")
    values = {
        "description": "Rain alert",
        "timing": '{"kind": "daily", "hour": "07:30"}',
        "policy": "run_if_recent",
        "active": 1,
        "created": past,
        "next_run": past,
        "timing_text": "every day at 07:30",
        "kind": "script",
        "project": "alert",
        "command": '["python3", "alert.py"]',
        **fields,
    }
    columns = ", ".join(values)
    slots = ", ".join("?" for _ in values)
    return await engine.db.run(
        f"INSERT INTO scheduled_tasks({columns}) VALUES ({slots})", tuple(values.values())
    )


# --- the connection ------------------------------------------------------------------------------


def test_what_a_network_error_is():
    assert is_network_error(ConnectionRefusedError()) and is_network_error(socket.gaierror())
    assert is_network_error(httpx.ConnectError("no network")) and is_network_error(TimeoutError())
    assert is_network_error(NetworkError("httpx.ConnectError")) and is_network_error(TimedOut())
    # BadRequest inherits from NetworkError in Telegram, but it is a request error.
    assert not is_network_error(BadRequest("message is not modified"))
    assert not is_network_error(ValueError("something else"))
    try:
        try:
            raise OSError(8, "nodename nor servname provided")
        except OSError as e:
            raise RuntimeError("while checking") from httpx.ConnectError(str(e))
    except RuntimeError as wrapped:
        assert is_network_error(wrapped)


async def test_connection_is_checked_at_most_once_a_minute(caplog):
    tests: list[bool] = []
    replies = [False, True]

    async def test() -> bool:
        tests.append(True)
        return replies.pop(0)

    c = Connection()
    c._test = test
    with caplog.at_level(logging.INFO, logger="aide.core.connection"):
        assert await c.check() is False and c.no_connection_since is not None
        assert await c.check() is False and len(tests) == 1  # the one from a moment ago
        assert await c.check(force=True) is True and len(tests) == 2
    changes = [r.getMessage() for r in caplog.records]
    assert len(changes) == 2 and changes[0].startswith("no internet connection")
    assert changes[1].startswith("internet connection is back")
    # What is seen while working marks it right away, with a single line per change.
    c.mark_offline()
    c.mark_offline()
    assert not c.in_line
    c.mark_online()
    assert c.in_line and c.no_connection_since is None


# --- notices, mail and checks --------------------------------------------------------------------


async def test_without_connection_nothing_is_notified_or_accumulated(engine):
    no_network(engine)
    assert await engine.notify(None, "Task 3 failed") is None
    with_network(engine)
    await engine.check_all()
    assert messages(engine) == []  # on return, what came before does not arrive
    assert await engine.notify(None, "Now it works") is not None


async def test_telegram_channel_without_connection(caplog):
    calls: list[str] = []

    class NoNetwork:
        async def send(self, text, buttons=None, replies_to=None):
            calls.append(text)
            raise NetworkError("httpx.ConnectError: [Errno 8] nodename nor servname provided")

    connection = Connection()
    channel = GuardedChannel(NoNetwork(), connection)
    with caplog.at_level(logging.INFO):
        assert await channel.send("hello") is None
    assert calls == ["hello"] and not [r for r in caplog.records if r.levelno >= logging.ERROR]
    # A stray failure does not give it up for lost: the next check really tries.
    assert connection.in_line and connection._checked is None

    async def no(*a):
        return False

    connection._test = no
    assert await connection.check() is False
    # Connection already confirmed absent, so it is not even attempted.
    assert await channel.send("another") is None and calls == ["hello"]


async def test_without_connection_email_is_not_checked_and_on_return_right_away(
    engine, monkeypatch
):
    reviews: list[bool] = []

    async def check_email():
        reviews.append(True)

    engine.config.settings.email.active = True
    monkeypatch.setattr(engine, "check_email", check_email)
    no_network(engine)
    await engine.check_all()
    await engine.check_all()
    assert reviews == []
    with_network(engine)
    await engine.check_all()
    await engine.check_all()  # only once: the next one, after the minutes in the setting
    assert len(reviews) == 1
    assert engine.email_minutes() == 5
    engine.config.settings.no_wifi.active = True
    assert engine.email_minutes() == 30


async def test_a_network_failure_when_checking_email_does_not_fill_the_log(
    engine, monkeypatch, caplog
):
    from aide.email import review
    from aide.tools.google_auth import GoogleNotAuthorized

    failures = [httpx.ConnectError("no network")]

    async def fake_review(m):
        raise failures[0]

    monkeypatch.setattr(review, "review", fake_review)
    with caplog.at_level(logging.WARNING):
        await engine.check_email()
        await engine.check_email()
    assert engine.connection._checked is None  # it is really checked on the next round
    # It is checked again in two minutes, with a single line in the log.
    loop = asyncio.get_running_loop()
    wait = engine._last_email + engine.email_minutes() * 60 - loop.time()
    assert 100 < wait <= 120
    assert len([r for r in caplog.records if "offline" in r.getMessage()]) == 1
    assert not [r for r in caplog.records if r.exc_info]
    # With Google authorization expired: one line and the link only once.
    with_network(engine)
    failures[0] = GoogleNotAuthorized("the Google authorization has expired")
    engine.core.services["google_link"] = lambda: "http://127.0.0.1:8765/google"
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        await engine.check_email()
        await engine.check_email()
    assert len([r for r in caplog.records if "not checking email" in r.getMessage()]) == 1
    assert sum("has expired" in t for t in texts(engine)) == 1


async def test_the_google_notice_that_does_not_go_out_is_repeated_with_connection(engine):
    moment = datetime(2026, 10, 26, 10, 0, tzinfo=UTC)
    expires = moment + timedelta(hours=20)
    engine.core.services["google"] = SimpleNamespace(expires_in=lambda: expires)
    engine.core.services["google_link"] = lambda: "http://127.0.0.1:8765/google"
    no_network(engine)
    await engine.check_google_expiry(moment)
    assert not await engine.db.kv_read("google_preventive_notice") and messages(engine) == []
    with_network(engine)
    await engine.check_google_expiry(moment)
    await engine.check_google_expiry(moment)
    assert sum("Google authorization (Gmail and Calendar) expires" in t for t in texts(engine)) == 1


# --- scheduled tasks and maintenance -------------------------------------------------------------


async def test_without_connection_scheduled_tasks_are_skipped_without_notice(engine):
    script = await scheduled_task(engine)
    model = await scheduled_task(engine, kind="model", description="News summary")
    no_network(engine)
    await engine.check_all()
    for pid in (script, model):
        row = await engine.db.one("SELECT * FROM scheduled_tasks WHERE id=?", (pid,))
        assert datetime.fromisoformat(row["next_run"]) > datetime.now(UTC)
        assert row["last_run"] is None
    assert await engine.db.value("SELECT COUNT(*) FROM tasks") == 0
    skipped = await engine.db.everything("SELECT * FROM events WHERE kind='scheduled_task_skipped'")
    assert len(skipped) == 2 and all("no connection" in e["summary"] for e in skipped)
    # When the connection returns they are not all done at once, and no notice arrives.
    with_network(engine)
    await engine.check_all()
    assert await engine.db.value("SELECT COUNT(*) FROM tasks") == 0
    assert messages(engine) == []


async def test_maintenance_without_connection_waits_for_the_next_one(engine):
    facts: list[bool] = []

    async def maintenance(m, test):
        facts.append(test)

    engine.maintenance = maintenance
    await scheduled_task(
        engine,
        kind="maintenance",
        description="Weekly maintenance",
        policy="run_always",
        timing='{"kind": "weekly", "days": ["sunday"], "hour": "19:00"}',
        project=None,
        command=None,
    )
    no_network(engine)
    await engine.check_all()
    await background(engine)
    assert facts == [] and await engine.db.kv_read(PENDING_MAINTENANCE_KEY)
    await engine.check_all()  # still offline: waits
    await background(engine)
    assert facts == []
    with_network(engine)
    await engine.check_all()
    await background(engine)
    await engine.check_all()
    await background(engine)
    assert facts == [False] and not await engine.db.kv_read(PENDING_MAINTENANCE_KEY)


# --- tasks that lose the connection --------------------------------------------------------------


async def test_a_task_that_loses_the_connection_is_paused_without_retries(engine):
    turns: list[str] = []

    def script(prompt, options, registry):
        turns.append(prompt)
        return [
            End("API Error: Connection error.", subtype="error_during_execution", is_error=True)
        ]

    FakeClient.script = staticmethod(script)
    task = await engine.create_direct_task("Summarize the Lumora website", origin="telegram")
    no_network(engine)
    await engine._run(task.id)
    task = await engine.tasks.require(task.id)
    assert (task.state_enum, task.pause_reason) == (TaskState.PAUSED_SHUTDOWN, OFFLINE_REASON)
    assert len(turns) == 1  # not even one retry
    assert messages(engine) == []
    assert await engine.db.kv_read(NOT_NOTIFIED_KEY) == {str(task.id): "paused"}
    # A new start does not resume it by itself: the user resumes it.
    engine._live.clear()
    assert await engine.recover_to_start() == []
    assert (await engine.tasks.require(task.id)).pause_reason == OFFLINE_REASON
    # When the connection returns, a single notice with what to do.
    with_network(engine)
    await engine.check_all()
    await engine.check_all()
    assert texts(engine) == [
        f"Task {task.id} stopped because the connection dropped. To pick it up where it left off, "
        f"type /resume {task.id}."
    ]
    # With a connection, an API error is still an error (with its usual retries).
    assert await engine.resume_task(task.id)


async def test_a_plan_that_loses_the_connection(engine):
    task = await engine.create_direct_task("x", origin="telegram")
    no_network(engine)
    await engine._handle_turn_failure(
        task.id, TurnResult("error", detail="Connection error"), "plan"
    )
    task = await engine.tasks.require(task.id)
    assert task.pause_reason == OFFLINE_REASON and task.phase == "plan"
    assert task.next_step is None
    with_network(engine)
    await engine._handle_turn_failure(
        (await engine.create_direct_task("y", origin="telegram")).id,
        TurnResult("error", detail="another error"),
        "run",
    )
    assert any("failed" in t for t in texts(engine))


# --- these days without wifi: Chrome only if the user asks ------------------------------------------


def test_when_it_asks_for_chrome():
    for text in (
        "usa Chrome",
        "míralo en mi Chrome",
        "puedes usar Chrome",
        "utiliza mi Chrome",
        "usa Google Chrome",
        "con Google Chrome",
        "Chrome sí",
        "ábrelo en mi navegador",
        "usa el navegador",
        "use Chrome",
        "please use my Chrome",
        "check it in my Chrome",
        "open Google Chrome",
        "look it up in my browser",
        "yes, use Chrome",
    ):
        assert requested_chrome(text), text
    for text in (
        "Mira en la web las medidas",
        "no uses Chrome",
        "nada de Chrome",
        "no lo mires en Chrome",
        "He recibido un correo de Google, resúmemelo",
        "La web no carga en el navegador",
        "Look up the sizes on the website",
        "don't use Chrome",
        "please do not use my browser",
        "without Chrome",
        "I got an email from Google, summarize it",
        "The site doesn't load in my browser",
    ):
        assert not requested_chrome(text), text


async def test_without_wifi_only_automatic_things_go_without_chrome(engine):
    """Now what the user launches (via the bot, the panel or Claude Code) uses Chrome
    if it needs it, as always: if they launch it, they have a connection. Only scheduled
    tasks go without it."""
    engine.config.settings.no_wifi.active = True
    engine.config.settings.real_browser.enabled = True
    for origin in ("telegram", "panel", "simulator"):
        task = await engine.create_direct_task(
            "Check the mug sizes on the Mugworks site", origin=origin, needs_browser=True
        )
        assert task.needs_browser == 1, origin
        assert "use Chrome" not in await engine._extra_block(task)
    no_navigate = await engine.create_direct_task("Summarize this text", origin="telegram")
    assert "use Chrome" not in await engine._extra_block(no_navigate)
    scheduled_task_ = await engine.create_direct_task(
        "Craftly market trends with your Chrome", origin="scheduled_task", needs_browser=True
    )
    assert scheduled_task_.needs_browser == 0
    assert "No Chrome: while there's no wifi, scheduled tasks don't use it." in (
        engine.config.paths.task(scheduled_task_.id) / "progress.md"
    ).read_text(encoding="utf-8")
    assert 'replying "use Chrome"' in await engine._extra_block(scheduled_task_)
    # If it asks for it when continuing with it, it uses it.
    await engine.continue_task(scheduled_task_.id, "use Chrome, please")
    assert (await engine.tasks.require(scheduled_task_.id)).needs_browser == 1
    # Outside these days, as always.
    engine.config.settings.no_wifi.active = False
    normal = await engine.create_direct_task(
        "Check the sizes on the Mugworks site", origin="telegram", needs_browser=True
    )
    assert normal.needs_browser == 1 and "use Chrome" not in await engine._extra_block(normal)


async def test_without_wifi_maintenance_does_not_download_updates(engine, monkeypatch):
    from aide.maintenance import updates

    def dont_look(root):
        raise AssertionError("without wifi the versions are not checked")

    monkeypatch.setattr(updates, "outdated", dont_look)
    engine.config.settings.no_wifi.active = True
    s = await updates.review(engine)
    assert s.ok and "use your phone's data" in s.lines[0]


# --- startup and Telegram ------------------------------------------------------------------------


async def test_without_connection_at_bot_startup_it_waits(engine, monkeypatch, caplog):
    from aide import application

    attempts: list[str] = []
    stopped: list[bool] = []

    class Bot:
        async def start(self):
            attempts.append("x")
            if len(attempts) < 3:
                raise NetworkError("httpx.ConnectError: no network")
            return "channel"

        async def stop(self):
            stopped.append(True)

    monkeypatch.setattr(application, "STARTUP_WAIT", 0)
    with caplog.at_level(logging.WARNING):
        assert await application.start_bot(Bot(), engine) == "channel"
    assert len(attempts) == 3 and len(stopped) == 2
    assert len([r for r in caplog.records if "at startup" in r.getMessage()]) == 1

    class Broken(Bot):
        async def start(self):
            raise ValueError("mistyped token")

    with pytest.raises(ValueError):
        await application.start_bot(Broken(), engine)


async def test_telegram_polling_without_connection(engine, caplog):
    from aide.bot.telegram import TelegramBot

    bot = TelegramBot("123:abc", 1, engine, engine.config.paths.agent_cache / "telegram")
    with caplog.at_level(logging.WARNING):
        bot._error_polling(NetworkError("httpx.ConnectError: no network"))
        bot._error_polling(NetworkError("httpx.ConnectError: no network"))
    # A probe failure (can happen with wifi) does not give it up for lost: it is really checked.
    assert engine.connection.in_line and engine.connection._checked is None
    notices = [r for r in caplog.records if "Telegram is not responding" in r.getMessage()]
    assert len(notices) == 1 and notices[0].exc_info is None
    # What is not a network error is still logged with its traceback.
    caplog.clear()
    with caplog.at_level(logging.ERROR):
        bot._error_polling(BadRequest("Bad Request: chat not found"))
    [trace] = [r for r in caplog.records if "error fetching messages" in r.getMessage()]
    # With the log's secret filter, the traceback is already redacted in exc_text.
    assert trace.exc_info or "BadRequest" in (trace.exc_text or "")


async def test_without_wifi_telegram_waits_longer_on_each_request(engine, monkeypatch):
    from aide.bot.telegram import TelegramBot

    bot = TelegramBot("123:abc", 1, engine, engine.config.paths.agent_cache / "telegram")
    requested: dict = {}

    async def nothing(*a, **k):
        return None

    async def polling(self, **k):
        requested.update(k)

    # Telegram objects do not allow changing their methods: they are changed in their classes.
    monkeypatch.setattr(type(bot.app), "initialize", nothing)
    monkeypatch.setattr(type(bot.app), "start", nothing)
    monkeypatch.setattr(type(bot.app.bot), "set_my_commands", nothing)
    monkeypatch.setattr(type(bot.app.updater), "start_polling", polling)
    engine.config.settings.no_wifi.active = True
    await bot.start()
    assert requested["timeout"] == 50 and requested["error_callback"] == bot._error_polling
    engine.config.settings.no_wifi.active = False
    await bot.start()
    assert requested["timeout"] == 10


async def test_google_renews_the_authorization_now(engine):
    expires = datetime(2026, 10, 26, 16, 48, tzinfo=UTC)
    engine.core.services["google"] = SimpleNamespace(expires_in=lambda: expires)
    engine.core.services["google_link"] = lambda: "http://127.0.0.1:8765/google/authorize"
    await engine.receive(Entry("command", 1, text="/google", command="google"))
    [text] = texts(engine)
    assert text.startswith("The Google authorization expires on Oct 26 at 17:48.")
    assert text.endswith("http://127.0.0.1:8765/google/authorize")


def test_engine_constants():
    assert modulo_motor.OFFLINE_REASON == "offline"


# --- what the independent review found ---------------------------------------------------------


async def test_a_stray_failure_does_not_drop_a_tasks_delivery(engine):
    """A stray network error (happens with wifi) does not give the connection up for lost: the
    delivery goes out."""
    from aide.bot.telegram import TelegramBot

    bot = TelegramBot("123:abc", 1, engine, engine.config.paths.agent_cache / "telegram")
    bot._error_polling(NetworkError("httpx.ReadError"))
    task = await engine.create_direct_task("Summarize this", origin="telegram")
    await engine.tasks.change_state(task.id, TaskState.RUNNING, "test")
    await engine._complete(task.id, TurnResult("ok", text="<delivery>Done.</delivery>"))
    assert any("Done." in t for t in texts(engine))
    # And an old “offline” state is checked again before a notice is dropped.
    engine.connection.mark_offline()
    engine.connection._checked -= 60
    assert await engine.notify(None, "Still online") is not None


async def test_what_missed_offline_arrives_in_one_message_on_return(engine):
    quick = await engine.tasks.create("What will the weather be like tomorrow?", origin="telegram")
    await engine.tasks.update(quick.id, kind="quick")

    async def reply(task, *images):
        no_network(engine)  # the response arrives just as the connection drops
        return "Sunny and 18 degrees."

    engine.reply_quick = reply
    await engine._quick_reply(quick.id)
    finished = await engine.create_direct_task("Summarize this", origin="telegram")
    await engine.tasks.change_state(finished.id, TaskState.RUNNING, "test")
    await engine._complete(finished.id, TurnResult("ok", text="<delivery>Done.</delivery>"))
    failed = await engine.create_direct_task("Something else", origin="telegram")
    await engine._fail(failed.id, "API error")
    assert messages(engine) == []
    assert await engine.db.kv_read(NOT_NOTIFIED_KEY) == {
        str(quick.id): "reply",
        str(finished.id): "finished",
        str(failed.id): "failed",
    }
    with_network(engine)
    await engine.check_all()
    await engine.check_all()
    [summary, answer] = texts(engine)
    assert f"Task {finished.id} finished while there was no connection" in summary
    assert f"Task {failed.id} failed while there was no connection" in summary
    assert answer.startswith('Here\'s my answer to "What will the weather be like tomorrow?"')
    assert "Sunny and 18 degrees." in answer
    assert not await engine.db.kv_read(NOT_NOTIFIED_KEY)


async def test_a_quick_answer_that_loses_the_connection(engine):
    quick = await engine.tasks.create("What is 2+2?", origin="telegram")
    await engine.tasks.update(quick.id, kind="quick")

    async def reply(task, *images):
        no_network(engine)
        raise httpx.ConnectError("no network")

    engine.reply_quick = reply
    await engine._quick_reply(quick.id)
    with_network(engine)
    await engine.check_all()
    assert texts(engine) == [
        'I couldn\'t answer "What is 2+2?" because the connection dropped. Please ask me again.'
    ]


async def test_a_task_whose_session_fails_without_connection_is_paused(engine, monkeypatch):
    task = await engine.create_direct_task("Summarize the Lumora website", origin="telegram")

    async def broken(task_id):
        no_network(engine)
        raise httpx.ConnectError("no network")

    monkeypatch.setattr(engine, "_run", broken)
    await engine.queue.put(task.id)
    worker = asyncio.create_task(engine._worker())
    await engine.queue.join()
    worker.cancel()
    task = await engine.tasks.require(task.id)
    assert (task.state_enum, task.pause_reason) == (TaskState.PAUSED_SHUTDOWN, OFFLINE_REASON)


async def test_at_startup_without_telegram_maintenance_is_not_lost(engine):
    await engine.db.kv_put(PENDING_MAINTENANCE_KEY, "2026-10-25T18:00:00+00:00")
    channel = engine.channel
    engine.channel = None  # Telegram does not respond yet at startup
    launched: list[bool] = []

    async def maintenance(m, test):
        launched.append(True)

    engine.maintenance = maintenance
    await engine.check_all()
    await background(engine)
    assert launched == [] and await engine.db.kv_read(PENDING_MAINTENANCE_KEY)
    engine.channel = channel
    await engine.check_all()
    await background(engine)
    assert launched == [True]


async def test_without_wifi_late_scheduled_tasks_are_skipped(engine):
    engine.config.settings.no_wifi.active = True
    ago_hours = (datetime.now(UTC) - timedelta(hours=3)).isoformat(timespec="seconds")
    pid = await scheduled_task(engine, next_run=ago_hours)  # el Mac estaba dormido
    await engine.check_all()
    assert await engine.db.value("SELECT COUNT(*) FROM events WHERE kind='script'") == 0
    [event] = await engine.db.everything("SELECT * FROM events WHERE kind='scheduled_task_skipped'")
    assert event["summary"] == f"scheduled task {pid}: arrived late during the no-wifi days"
    assert messages(engine) == []


def test_more_network_errors():
    import errno

    assert is_network_error(OSError(errno.ENETUNREACH, "Network is unreachable"))
    assert is_network_error(OSError(errno.EHOSTUNREACH, "No route to host"))
    assert not is_network_error(OSError(errno.ENOENT, "No such file"))
