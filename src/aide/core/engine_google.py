"""Part of the engine: Google authorization."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from aide.core import prompts
from aide.core.context import SessionContext
from aide.core.db import now
from aide.core.hooks import note_progress
from aide.core.states import FINAL, PAUSED, TaskState

# Google's reminder the day before it expires, from this time.
GOOGLE_NOTICE_HOUR = 9


class GoogleMixin:
    """Expiry and renewal of Google authorization."""

    async def expired_google(self, ses: SessionContext) -> None:
        """Called by the Google tools: the turn is cut and only this task is paused."""
        if ses.expired_google:
            return
        ses.expired_google = True
        await self.tasks.event(
            ses.task_id, "expired_google", "the Google authorization isn't valid"
        )
        self._cut_turn(ses.task_id)

    async def _pause_by_google(self, task_id: int) -> None:
        task = await self.tasks.require(task_id)
        if task.state_enum not in PAUSED and task.state_enum not in FINAL:
            await self.tasks.change_state(
                task_id, TaskState.PAUSED_LOGIN, "Google authorization expired"
            )
        done = await self.core.actions.done_descriptions(task_id)
        await self.tasks.update(
            task_id,
            pause_reason="google",
            next_step=prompts.prompt_resume(done, "the Google authorization had expired"),
        )
        note_progress(self.config.paths.task(task_id), "Paused: the Google authorization expired.")
        await self.request_google_authorization(task_id)

    async def request_google_authorization(
        self,
        task_id: int | None = None,
        preventive: bool = False,
        expires: datetime | None = None,
        moment: datetime | None = None,
    ) -> None:
        """Sends the user the link to renew the authorization (once per expiry). The preventive
        warning is the day-before reminder: when it expires and to type /google."""
        key = "google_preventive_notice" if preventive else "google_notice_sent"
        if await self.db.kv_read(key):
            if task_id:
                await self.notify(
                    task_id,
                    f"Task {task_id} is waiting for you to renew the Google authorization.",
                )
            return
        link = self.core.services.get("google_link")
        url = link() if callable(link) else None
        if preventive:
            zone = ZoneInfo(self.config.time_zone)
            local_now = (moment or datetime.now(UTC)).astimezone(zone)
            local = (expires or local_now + timedelta(days=1)).astimezone(zone)
            timing = "Today" if local.date() == local_now.date() else "Tomorrow"
            text = (
                f"{timing} at {local:%H:%M} the Google authorization (Gmail and Calendar) "
                "expires. Type /google to renew it and I'll send you the link to open on the Mac."
            )
            if await self.notify(task_id, text):
                await self.db.kv_put(key, now())
            return
        else:
            text = (
                "The Google authorization (Gmail and Calendar) has expired; tasks that use it are "
                "paused."
            )
        if url:
            text += f"\nRenew it by opening this link on the Mac (sign in with your Google account):\n{url}"
        else:
            text += "\nRenew it on the Mac with: uv run python -m aide.tools.google_auth"
        # Only what arrived counts as notified: with no connection, it tries again.
        if await self.notify(task_id, text):
            await self.db.kv_put(key, now())

    async def reauthorized_google(self, account: str) -> list[int]:
        """After renewing the authorization: tasks paused by Google are resumed."""
        await self.db.kv_delete("google_notice_sent")
        await self.db.kv_delete("google_preventive_notice")
        paused = [
            t
            for t in await self.tasks.list_items(states=[TaskState.PAUSED_LOGIN], limit=50)
            if t.pause_reason == "google"
        ]
        for task in paused:
            await self.resume_task(task.id)
        # The “Today” calendar is re-read now, without waiting for the next half hour.
        self._last_calendar = asyncio.get_running_loop().time()
        self._launch(self._refresh_calendar())
        text = f"Google authorization renewed for {account}."
        if paused:
            text += (
                (" Resuming task " if len(paused) == 1 else " Resuming tasks ")
                + ", ".join(str(t.id) for t in paused)
                + "."
            )
        await self.notify(None, text)
        return [t.id for t in paused]

    async def check_google_expiry(self, moment: datetime | None = None) -> None:
        """Preventive warning that the authorization is expiring (the Google app is still in “Testing” and
        expires after 7 days): the day before, from 9:00, without a model. If there was no
        connection that day, it goes out as soon as there is one, as long as it hasn't expired."""
        auth = self.core.services.get("google")
        if auth is None:
            return
        expires = await asyncio.to_thread(auth.expires_in)
        if expires is None:
            return
        zone = ZoneInfo(self.config.time_zone)
        local_now = (moment or datetime.now(UTC)).astimezone(zone)
        day = expires.astimezone(zone).date()
        if local_now >= expires or local_now.date() < day - timedelta(days=1):
            return
        if local_now.date() == day - timedelta(days=1) and local_now.hour < GOOGLE_NOTICE_HOUR:
            return
        await self.request_google_authorization(preventive=True, expires=expires, moment=local_now)

    async def check_google(self) -> list[int]:
        """If the user renewed the authorization another way (terminal), tasks are resumed."""
        auth = self.core.services.get("google")
        if auth is None:
            return []
        paused = [
            t
            for t in await self.tasks.list_items(states=[TaskState.PAUSED_LOGIN], limit=50)
            if t.pause_reason == "google"
        ]
        if not paused:
            return []
        try:
            await asyncio.to_thread(auth.credentials)
        except Exception:
            return []
        account = (await asyncio.to_thread(auth.state)).get("account") or "your account"
        return await self.reauthorized_google(account)
