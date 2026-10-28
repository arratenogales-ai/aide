"""Part of the engine: the periodic checks and the background loops.

What is looked at every 30 seconds and every few minutes: connection, pauses that were
resolved, login, plan limits, email, calendar and morning summary.
"""

from __future__ import annotations

import asyncio
import contextlib
from datetime import UTC, datetime, timedelta
from typing import Any

from aide.bot.formatting import basic_markdown_to_html, esc, truncate
from aide.core.connection import is_network_error
from aide.core.db import to_date
from aide.core.engine_common import (
    NOT_NOTIFIED_KEY,
    OFFLINE_REASON,
    PENDING_MAINTENANCE_KEY,
    log,
)
from aide.core.states import TaskState
from aide.core.tasks import Task

# After requesting limits data, it is not requested again before this time (seconds).
LIMIT_ATTEMPTS_WAIT = 120.0
# The calendar is reread every half hour while there is a connection.
CALENDAR_MINUTES = 30
# The morning summary cannot take longer than this.
MAX_SUMMARY_SECONDS = 300


class ChecksMixin:
    """Periodic checks, login and plan limits, and background loops."""

    # --- periodic checks -----------------------------------------------------------------

    async def check_browser(self) -> list[int]:
        """Resumes the tasks paused by Chrome when it becomes available again."""
        paused = await self.tasks.list_items(states=[TaskState.PAUSED_BROWSER], limit=50)
        if not paused or not await self.ready_chrome():
            return []
        ids = [t.id for t in paused]
        for task_id in ids:
            await self.resume_task(task_id)
        await self.notify(
            None,
            "Chrome is available again. Resuming "
            + ("task " if len(ids) == 1 else "tasks ")
            + ", ".join(str(i) for i in ids)
            + ".",
        )
        return ids

    async def _checks_loop(self) -> None:
        while True:
            await asyncio.sleep(self.checks_interval)
            await self.check_all()

    async def check_all(self) -> None:
        """Periodic checks: quick ones every 30 s and slow ones every few minutes.

        With no connection only the scheduled tasks are looked at, and they are skipped if
        they come due; everything else waits without trying, so there are no looping errors. Since
        the clocks of what is waiting do not advance, when the connection returns it is looked at
        right away."""
        in_line = await self.connection.check()
        for check in (
            (
                self.check_scheduled_tasks,
                self.check_pending_scripts,
                self.check_limits,
                self.check_browser,
            )
            if in_line
            else (self.check_scheduled_tasks,)
        ):
            try:
                await check()
            except Exception:
                log.exception("error in %s", check.__name__)
        if not in_line:
            return
        try:
            await self._pending_after_reconnect()
        except Exception:
            log.exception("error in what was waiting for the connection")
        loop = asyncio.get_running_loop()
        if loop.time() - self._last_slow >= self.config.settings.periodic_check_minutes * 60:
            self._last_slow = loop.time()
            await self.check_pauses(browser=False)
        if loop.time() - self._last_expiry >= 3600:
            self._last_expiry = loop.time()
            with contextlib.suppress(Exception):
                await self.check_google_expiry()
            try:
                # The warnings for your documents' dates, once a day from 9:00
                # .
                from aide.paperwork import reminders

                await reminders.review(self)
            except Exception:
                log.exception("error in the document reminders")
            try:
                # The warning 2 days before the expected period, from 9:00.
                from aide.health import cycle

                await cycle.review_notice(self)
            except Exception:
                log.exception("error in the cycle notice")
        if self.config.settings.morning_summary.active and not self._summary_in_running:
            # The morning summary: by code, once a day from 7:30. In the
            # background (it checks the weather and maybe Google) and never two at once.
            self._summary_in_running = True
            self._launch(self._morning_summary())
        if loop.time() - self._last_calendar >= CALENDAR_MINUTES * 60:
            # Today's calendar and the next 7 days, for the panel's “Today” and the
            # morning summary: by code, with the Google permission it already
            # has.
            self._last_calendar = loop.time()
            self._launch(self._refresh_calendar())
        if (
            self.config.settings.email.active
            and loop.time() - self._last_email >= self.email_minutes() * 60
        ):
            self._last_email = loop.time()
            self._launch(self.check_email())

    async def _morning_summary(self) -> None:
        from aide import morning

        try:
            # With a maximum time: Google and Open-Meteo have their own, but this way
            # the flag is never left set.
            await asyncio.wait_for(morning.review(self), timeout=MAX_SUMMARY_SECONDS)
        except Exception:
            log.exception("error in the morning summary")
        finally:
            self._summary_in_running = False

    async def _refresh_calendar(self) -> None:
        from aide import life

        if self.core.services.get("google") is None:
            return
        try:
            await life.refresh_calendar(self.core)
        except Exception:
            log.exception("couldn't read the calendar")

    def email_minutes(self) -> float:
        """How often, in minutes, email is checked: on these days without wifi, every 30."""
        settings = self.config.settings
        return settings.no_wifi.email_minutes if settings.no_wifi.active else settings.email.minutes

    async def _pending_after_reconnect(self) -> None:
        """What was waiting for the connection to return: the maintenance that came due
        without it and, in a single message, what from their tasks did not reach them."""
        if self.channel is None:
            return  # at startup, until Telegram responds
        if await self.db.kv_read(PENDING_MAINTENANCE_KEY) and not self._maintenance_in_progress:
            self._launch(self.run_maintenance())
        pending = dict(await self.db.kv_read(NOT_NOTIFIED_KEY) or {})
        if not pending:
            return
        lines: list[str] = []
        replies: list[Task] = []
        for key, that in sorted(pending.items(), key=lambda pair: int(pair[0])):
            task = await self.tasks.get(int(key))
            if task is None:
                continue
            n = task.id
            if (
                that == "paused"
                and task.state_enum == TaskState.PAUSED_SHUTDOWN
                and task.pause_reason == OFFLINE_REASON
            ):
                lines.append(
                    f"Task {n} stopped because the connection dropped. To pick it up where it "
                    f"left off, type /resume {n}."
                )
            elif that == "finished" and task.state_enum == TaskState.COMPLETED:
                lines.append(
                    f"Task {n} finished while there was no connection. Use /task {n} to see "
                    "the result."
                )
            elif that == "failed" and task.state_enum == TaskState.FAILED:
                if task.kind == "quick":
                    lines.append(
                        f'I couldn\'t answer "{truncate(task.request, 80)}" because the '
                        "connection dropped. Please ask me again."
                    )
                else:
                    lines.append(
                        f"Task {n} failed while there was no connection. Use /task {n} to see why."
                    )
            elif that == "reply" and task.result:
                replies.append(task)
        sent = True
        if lines:
            sent = bool(await self.notify(None, "\n".join(lines)))
        for task in replies:
            if not sent:
                break
            header = esc(
                f"Here's my answer to \"{truncate(task.request, 100)}\", which didn't reach "
                "you because there was no connection."
            )
            sent = bool(
                await self.notify(
                    task.id,
                    f"<i>{header}</i>\n\n{basic_markdown_to_html(task.result or '')}",
                    html=True,
                )
            )
        if sent or self.connection.in_line:
            # Sent, or rejected for something other than the connection: not repeated in
            # a loop.
            await self.db.kv_delete(NOT_NOTIFIED_KEY)

    async def check_email(self) -> None:
        """Background Gmail check: read-only and only if active. With no connection or with Google
        authorization expired the error log is not filled: one line, and it is looked at again when
        the connection returns or when it is renewed."""
        from aide.email import review
        from aide.tools.google_auth import GoogleNotAuthorized

        try:
            await review.review(self)
        except GoogleNotAuthorized as e:
            if not self._email_no_google:
                self._email_no_google = True
                log.warning("not checking email: %s", e)
            # Once per expiry, the link to renew it (if it was not already sent).
            await self.request_google_authorization()
            return
        except Exception as e:
            if not is_network_error(e):
                log.exception("error checking email")
                return
            # A network failure: the connection is checked and looked at again in two
            # minutes (or as soon as it returns), with a single log line until it works.
            self.connection.suspect()
            loop = asyncio.get_running_loop()
            self._last_email = loop.time() - self.email_minutes() * 60 + 120
            if not self._email_no_network:
                self._email_no_network = True
                log.warning("couldn't check email: offline (%s)", type(e).__name__)
            return
        self._email_no_google = False
        self._email_no_network = False

    async def check_pauses(self, browser: bool = True) -> None:
        """Login, Google and Chrome: tasks whose problem was resolved resume on their own."""
        for check in (
            self.check_login,
            self.check_google,
            *((self.check_browser,) if browser else ()),
        ):
            try:
                await check()
            except Exception:
                log.exception("error in %s", check.__name__)

    # --- login and plan limits -----------------------------------------------------------

    async def check_limits(self) -> list[int]:
        """Resumes the tasks paused by a plan limit when their reset time arrives."""
        moment = datetime.now(UTC)
        lists = [
            t
            for t in await self.tasks.list_items(states=[TaskState.PAUSED_LIMIT], limit=50)
            if (to_date(t.pause_until) or moment) <= moment
        ]
        for task in lists:
            await self.resume_task(task.id)
        if lists:
            await self.notify(
                None,
                "The plan limit has reset. Resuming "
                + ("task " if len(lists) == 1 else "tasks ")
                + ", ".join(str(t.id) for t in lists)
                + ".",
            )
        return [t.id for t in lists]

    async def check_login(self) -> list[int]:
        """Resumes tasks paused for login once sign-in is valid again, then notifies."""
        paused = [
            t
            for t in await self.tasks.list_items(states=[TaskState.PAUSED_LOGIN], limit=50)
            if t.pause_reason != "google"
        ]
        if not paused or not await self.valid_login():
            return []
        for task in paused:
            await self.resume_task(task.id)
        from aide.core.authentication import credential_name

        await self.notify(
            None,
            f"The {credential_name(self.config)} works again. Resuming "
            + ("task " if len(paused) == 1 else "tasks ")
            + ", ".join(str(t.id) for t in paused)
            + ".",
        )
        return [t.id for t in paused]

    async def _valid_login(self) -> bool:
        result = await self._minimal_request(browsers=True)
        return result.ok

    async def _minimal_request(self, browsers: bool = False) -> Any:
        """Minimal request with Haiku: checks the login and saves the limits it brings."""
        from aide.core.authentication import check_authentication

        result = await check_authentication(
            self.config, at_limit=self.usage.log_limit, browsers=browsers
        )
        self.core.services["authentication"] = result
        return result

    async def refresh_limits(self, max_age: timedelta = timedelta(minutes=10)) -> bool:
        """Requests new plan limits data if the saved data is old.

        The data arrives with each model request; if there has been none recently (for example,
        right after startup), a minimal request is made with Haiku. Returns whether new data was
        requested.
        """
        async with self._limits_lock:
            age = await self.usage.limits_age()
            if age is not None and age <= max_age:
                return False
            # If the last attempt was moments ago (and brought no data), it is not
            # repeated at every query.
            loop = asyncio.get_running_loop()
            previous = self._last_attempt_limits
            if previous is not None and loop.time() - previous < LIMIT_ATTEMPTS_WAIT:
                return False
            self._last_attempt_limits = loop.time()
            try:
                await asyncio.wait_for(self.request_limits(), timeout=45)
            except Exception:
                log.warning("couldn't update the plan limits", exc_info=True)
                return False
            return True

    # --- background loops --------------------------------------------------------------

    async def _typing_loop(self) -> None:
        while True:
            await asyncio.sleep(4)
            if self.channel is None or not self.in_progress:
                continue
            active = [
                e for e in self.in_progress.values() if e.session is not None and e.session.in_turn
            ]
            if active:
                with contextlib.suppress(Exception):
                    await self.channel.writing()

    async def _reminders_loop(self) -> None:
        while True:
            await asyncio.sleep(600)
            if not self.connection.in_line:
                continue  # with no connection nothing is remembered
            with contextlib.suppress(Exception):
                await self.approvals.remember_pending()
