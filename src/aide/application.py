"""Application assembly: connects the services to the engine and starts the bot, panel and
scheduler."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from typing import Any

from aide.common.config import Config
from aide.common.environment import EnvironmentReport
from aide.core.engine import Engine

log = logging.getLogger(__name__)

# With no connection at startup, how often to retry Telegram (seconds).
STARTUP_WAIT = 60.0


async def configure_engine(engine: Engine, simulated: bool = False) -> None:
    """Connects the services of each phase (triage, quick replies, review...) to the engine."""
    from aide.bot.commands import run_command

    engine.commands = run_command
    try:
        from aide.core.triage import classify

        async def triage(text, recent, task_id=None) -> Any:
            return await classify(engine.core, text, recent, task_id)

        engine.triage = triage
    except ModuleNotFoundError:
        pass
    try:
        from aide.core.quick import QuickConversation

        conversation = QuickConversation(engine.core)
        engine.reply_quick = conversation.reply
    except ModuleNotFoundError:
        pass
    try:
        from aide.core.review import review

        async def review_result(task, final_text) -> Any:
            return await review(engine.core, task, final_text)

        engine.review = review_result
    except ModuleNotFoundError:
        pass
    credentials = engine.config.paths.root / engine.config.environment.GOOGLE_CREDENTIALS_PATH
    if credentials.exists():
        from aide.tools.google_auth import GoogleAuthorization

        engine.core.services["google"] = GoogleAuthorization(engine.config)
        engine.core.services["expired_google"] = engine.expired_google
    try:
        from aide.bot.voice import Transcriber

        engine.transcribe = Transcriber(engine.config).transcribe
    except ModuleNotFoundError:
        pass
    # Weekly maintenance and immediate warnings of the weekly limit (70% and 90%).
    from aide.maintenance import consumption, report

    engine.maintenance = report.run

    async def thresholds(raw) -> Any:
        await consumption.review_thresholds(engine, raw)

    engine.usage.to_log = thresholds
    if not simulated:
        await report.ensure_scheduled_task(engine)


async def start_bot(bot, engine: Engine) -> Any:
    """Starts the Telegram bot. With no connection it does not exit with an error, which
    launchd would restart every 30 seconds in a loop: it waits, trying every minute, until
    there is one."""
    from aide.core.connection import is_network_error

    notified = False
    while True:
        try:
            return await bot.start()
        except Exception as e:
            if not is_network_error(e):
                raise
            if not notified:
                log.warning(
                    "Telegram is not responding at startup (no connection): trying every minute"
                )
                notified = True
            engine.connection.mark_offline()
            await bot.stop()
            await asyncio.sleep(STARTUP_WAIT)


async def run(config: Config, report: EnvironmentReport) -> int:
    """Full startup: engine, Telegram bot and services; clean shutdown with SIGTERM or SIGINT."""
    from aide.bot.channel import GuardedChannel
    from aide.bot.telegram import TelegramBot
    from aide.core.authentication import check_authentication

    engine = Engine(config)
    await engine.start()
    await configure_engine(engine)

    token = config.environment.TELEGRAM_BOT_TOKEN
    user = config.environment.TELEGRAM_ALLOWED_USER_ID
    if token is None or user is None:
        log.error(
            "TELEGRAM_BOT_TOKEN or TELEGRAM_ALLOWED_USER_ID is missing in .env; the bot can't start"
        )
        await engine.stop()
        return 2
    bot = TelegramBot(token.get_secret_value(), user, engine, config.paths.agent_cache / "telegram")
    engine.channel = GuardedChannel(await start_bot(bot, engine), engine.connection)
    log.info("Telegram bot running")

    stopped: list[Callable[[], Awaitable[None]]] = []
    from aide.panel.app import Panel

    panel = Panel(engine)
    engine.core.services["panel_link"] = panel.link
    engine.core.services["link_panel_mobile"] = panel.mobile_link
    engine.core.services["google_link"] = lambda: panel.link("/google/authorize")
    try:
        stopped.append(await panel.start())
    except Exception:
        log.exception("could not start the panel")

    # Startup warnings: only if there is something to fix.
    for notice in report.notices():
        await engine.notify(None, notice)
    # The check request also brings the plan limits: they are saved.
    authentication = await check_authentication(config, at_limit=engine.usage.log_limit)
    engine.core.services["authentication"] = authentication
    if not authentication.ok or authentication.notices:
        await engine.notify(None, authentication.text())

    # What was left half-done by a shutdown or an outage, and the scheduled tasks that came due
    # with the Mac off.
    try:
        await engine.recover_to_start()
        await engine.check_all()
    except Exception:
        log.exception("error recovering tasks at startup")

    end = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, end.set)
    await end.wait()

    log.info("clean shutdown")
    for stop in reversed(stopped):
        with contextlib.suppress(Exception):
            await stop()
    await bot.stop()
    await engine.stop()
    return 0
