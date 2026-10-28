"""Part of the engine: what is decided about an already created task.

Following up a task, buttons, pausing, resuming, cancelling, the pause for Chrome and
recovery at startup.
"""

from __future__ import annotations

import asyncio
import contextlib
import re

from aide.bot.channel import Entry
from aide.core import navigation, prompts
from aide.core.context import SessionContext
from aide.core.engine_common import OFFLINE_REASON, TextWait
from aide.core.hooks import note_progress
from aide.core.states import FINAL, PAUSED, TaskState
from aide.core.tasks import Task

# Another code saved in pause_reason (core/presentation.py compares it too): not translated.
USER_PAUSE_REASON = "paused by the user"
# What the model reads on resuming when the saved reason is one of those codes.
REASONS_FOR_MODEL = {
    OFFLINE_REASON: "the internet connection dropped",
    USER_PAUSE_REASON: "the user paused the task",
    "google": "the Google authorization had expired",
}
# Without wifi, Chrome only if the user asks: “usa Chrome”, “en mi Chrome”..., but not “no
# uses Chrome”.
_CHROME_VERB = (
    r"(?:usa|usar|usando|úsalo|utiliza|utilizar|utilizando|abre|abrir|abriendo|mira|míralo|"
    r"busca|búscalo|entra|use|using|open|opening|check|look|search|browse)"
)
_CHROME = r"(?:google\s+)?chrome"
RE_REQUESTED_CHROME = re.compile(
    "|".join(
        [
            rf"\b{_CHROME_VERB}\s+(?:(?:mi|tu|el|my|your|the)\s+)?(?:{_CHROME}|navegador|browser)\b",
            rf"\b(?:en|con|desde|in|with|from|on|via|through)\s+(?:(?:mi|tu|my|your)\s+)?{_CHROME}\b",
            r"\b(?:en|con|desde|in|with|from|on)\s+(?:mi|my)\s+(?:navegador|browser)\b",
            rf"\b{_CHROME}\s+s[ií]\b",
            rf"\byes,?\s+(?:use\s+)?{_CHROME}\b",
        ]
    ),
    re.IGNORECASE,
)
RE_NEGATED_CHROME = re.compile(
    r"\b(?:no|sin|nada de|not|without|never|don[’']?t|doesn[’']?t)\s+(?:\w+\s+){0,3}"
    r"(?:(?:google\s+)?chrome|navegador|browser)\b",
    re.IGNORECASE,
)


def requested_chrome(text: str | None) -> bool:
    """Whether the user expressly asks for their Chrome."""
    text = text or ""
    return bool(RE_REQUESTED_CHROME.search(text)) and not RE_NEGATED_CHROME.search(text)


class ControlMixin:
    """Continuation, buttons, pauses, real Chrome and recovery after an outage."""

    # --- continuation, buttons and control -------------------------------------------------

    async def continue_task(self, task_id: int, text: str, entry: Entry | None = None) -> None:
        """Continues a task with a new message, or starts a new entry if it is missing."""
        task = await self.tasks.get(task_id)
        if task is None:
            await self._new_entry(text, entry or Entry("text", 0, text), can_continue=False)
            return
        if task.kind in ("quick", "message") and task.parent_task:
            task = await self.tasks.require(task.parent_task)
            task_id = task.id
        elif task.kind == "quick":
            # Replying to a quick reply is another quick question, with the previous one
            # in front; before, it was reopened as a task, with zero caps and on Opus
            # .
            previous = f"The user's question: {task.request}\n\nYour answer: {task.result or ''}"
            await self._quick_reply_with_context(text, entry or Entry("text", 0, text), previous)
            return
        if entry is not None:
            await self._link_entry(entry, task_id)
            await self._save_files(task_id, entry)
        await self.tasks.event(task_id, "user_message", text)
        if (
            self.config.settings.no_wifi.active
            and self.config.real_browser
            and not task.needs_browser
            and requested_chrome(text)
        ):
            # On these days without wifi, Chrome only if the user asks: they ask when
            # following up.
            task = await self.tasks.update(task_id, needs_browser=1)
            note_progress(self.config.paths.task(task_id), "The user asks to use their Chrome.")
        run = self.in_progress.get(task_id)
        if run and run.session is not None and run.session.in_turn:
            run.ses.user_texts.append(text)
            await run.session.inject(prompts.prompt_message_during_run(text))
            await self.notify(task_id, f"Got it. I'll take that into account in task {task_id}.")
            return
        if task.state_enum == TaskState.WAITING_PLAN:
            await self._change_plan(task_id, text)
            return
        if task.state_enum in FINAL or task.state_enum in PAUSED:
            if task.state_enum == TaskState.CANCELLED:
                await self._new_entry(text, entry or Entry("text", 0, text), can_continue=False)
                return
            entries = sorted((self.config.paths.task(task_id) / "input").glob("*"))
            next_step = prompts.prompt_tracking(text, entries)
            if self._resume_of(task) is None and task.summary:
                next_step = prompts.prompt_new_session_from_summary(
                    task.summary, self.config.paths.task(task_id), text
                ) + await self._extra_block(task)
            await self.tasks.update(task_id, next_step=next_step)
            await self.enqueue(task_id, "follow-up from the user")
            await self.notify(task_id, f"Continuing with task {task_id}.")
            return
        # Queued or planning: added to the request.
        await self.tasks.update(task_id, request=f"{task.request}\n\nAdded by the user: {text}")
        await self.notify(task_id, f"I'll add it to task {task_id}.")

    async def button(self, datum: str) -> None:
        """Handles a button press by dispatching on its colon-separated data."""
        parts = datum.split(":")
        kind = parts[0]
        if kind == "apr" and len(parts) == 3:
            if not await self.approvals.resolve(int(parts[1]), parts[2] == "yes", "telegram"):
                await self.notify(None, "That approval was already resolved.")
        elif kind == "batch" and len(parts) == 3:
            await self.approvals.resolve_batch(parts[1], parts[2])
        elif kind == "sched" and len(parts) == 3:
            await self.questions.resolve_button(int(parts[1]), parts[2])
        elif kind == "plan" and len(parts) == 3:
            await self._plan_button(int(parts[1]), parts[2])
        elif kind == "cap" and len(parts) == 3:
            await self._cap_button(int(parts[1]), parts[2])
        elif kind == "reply" and len(parts) == 3 and parts[2].isdigit():
            await self._reply_button(int(parts[2]), parts[1] == "yes")
        elif kind == "quick" and len(parts) == 3 and parts[1] == "task" and parts[2].isdigit():
            await self._do_it_as_task(int(parts[2]))
        elif self.commands is not None:
            await self.commands(self, Entry("button", 0, button_datum=datum))
        await self._notify_change()

    async def _plan_button(self, task_id: int, decision: str) -> None:
        task = await self.tasks.get(task_id)
        if task is None or task.state_enum != TaskState.WAITING_PLAN:
            await self.notify(task_id, "That plan is no longer pending.")
            return
        if decision == "yes":
            await self.tasks.event(task_id, "approved_plan", task.plan or "")
            await self.tasks.update(task_id, next_step=prompts.prompt_approved_plan())
            await self.enqueue(task_id, "plan approved")
            await self.notify(task_id, f"Plan approved. Starting on task {task_id}.")
        elif decision == "change":
            self.text_wait = TextWait("plan_change", task_id)
            await self.notify(task_id, "Write what you'd like to change in the plan.")
        else:
            await self.cancel_task(task_id, "The user cancels the plan")

    async def _cap_button(self, task_id: int, decision: str) -> None:
        task = await self.tasks.get(task_id)
        if task is None or task.state_enum != TaskState.PAUSED_CAP:
            await self.notify(task_id, "That task is no longer stopped by its cap.")
            return
        if decision == "more":
            factor = self.config.settings.caps.extension_factor
            await self.tasks.update(
                task_id,
                turn_cap=max(task.turn_cap * factor, task.turns_used + task.turn_cap),
                usd_cap=max(task.usd_cap * factor, task.usd_used + task.usd_cap),
                next_step=prompts.prompt_continue_cap(),
            )
            spent = (await self.usage.task_consumption(task_id)).total
            if task.token_cap:
                # The task's own budget: like the turns, by the factor.
                await self.tasks.update(
                    task_id,
                    token_cap=int(max(task.token_cap * factor, spent + task.token_cap)),
                )
            # Budget of tasks with Chrome: the same amount again from what was
            # already spent.
            state = await navigation.load(self.db, task_id)
            if state.actions:
                state.token_cap = spent + self.config.settings.real_browser.max_tokens
                await navigation.save(self.db, task_id, state)
            await self.tasks.event(task_id, "extended_cap", f"factor {factor}")
            await self.enqueue(task_id, "cap raised")
            await self.notify(task_id, f"Cap raised. Continuing with task {task_id}.")
        else:
            await self.cancel_task(task_id, "The user doesn't raise the cap")

    async def _pause_by_user(self, task_id: int) -> None:
        task = await self.tasks.require(task_id)
        if task.state_enum not in PAUSED and task.state_enum not in FINAL:
            await self.tasks.change_state(task_id, TaskState.PAUSED_SHUTDOWN, "paused by the user")
        done = await self.core.actions.done_descriptions(task_id)
        await self.tasks.update(
            task_id,
            pause_reason=USER_PAUSE_REASON,
            next_step=prompts.prompt_resume(done, "the user paused the task"),
        )
        note_progress(self.config.paths.task(task_id), "Paused by the user.")
        await self.notify(task_id, f"Task {task_id} paused. Use /resume {task_id} to continue.")

    async def pause_task(self, task_id: int) -> bool:
        """Pauses a task if it is active or queued; returns whether it was paused."""
        task = await self.tasks.get(task_id)
        if task is None or task.state_enum in FINAL or task.state_enum in PAUSED:
            return False
        run = self.in_progress.get(task_id)
        if run is not None and run.session is not None:
            run.paused = True
            await run.session.interrupt()
            return True
        if task.state_enum in (TaskState.IN_QUEUE, TaskState.WAITING_PLAN, TaskState.RECEIVED):
            await self._pause_by_user(task_id)
            return True
        return False

    async def resume_task(self, task_id: int) -> bool:
        """Resumes a paused task; returns False if it is not paused."""
        task = await self.tasks.get(task_id)
        if task is None or task.state_enum not in PAUSED:
            return False
        if not task.next_step and task.session_id:
            # Only if it had already worked: a task that never started uses its initial
            # prompt.
            done = await self.core.actions.done_descriptions(task_id)
            await self.tasks.update(
                task_id,
                next_step=prompts.prompt_resume(
                    done,
                    REASONS_FOR_MODEL.get(task.pause_reason or "", task.pause_reason or "pause"),
                ),
            )
        if task.previous_state == TaskState.WAITING_PLAN and task.plan:
            # Paused while waiting for the plan's approval: it is requested again.
            await self.tasks.update(task_id, next_step=None)
            await self.tasks.change_state(task_id, TaskState.WAITING_PLAN, "resumed")
            await self._send_plan(task_id, task.plan)
        elif task.phase == "plan":
            await self.tasks.update(task_id, next_step=None)
            await self.tasks.change_state(task_id, TaskState.PLANNING, "resuming the plan")
            self._launch(self._plan(task_id))
        else:
            await self.enqueue(task_id, "resumed")
        await self.tasks.update(task_id, pause_until=None, pause_reason=None)
        return True

    async def cancel_task(self, task_id: int, reason: str) -> bool:
        """Cancels a task and its approvals and questions, interrupting its session."""
        task = await self.tasks.get(task_id)
        if task is None or task.state_enum in FINAL:
            return False
        run = self.in_progress.get(task_id)
        if run is not None:
            run.cancelled = True
        await self.approvals.cancel_from_task(task_id)
        self.questions.cancel_from_task(task_id)
        if run is not None and run.session is not None:
            await run.session.interrupt()
        await self.tasks.change_state(task_id, TaskState.CANCELLED, reason)
        note_progress(self.config.paths.task(task_id), f"Canceled: {reason}.")
        await self.notify(
            task_id, f"Task {task_id} canceled. What's been done so far is in its folder."
        )
        await self._notify_change()
        return True

    # --- real Chrome ----------------------------------------------------------------------

    async def down_browser(self, ses: SessionContext) -> None:
        """The extension does not respond after a retry: the turn is cut and the task is paused."""
        await self.tasks.event(ses.task_id, "down_browser", "Chrome isn't responding")
        self._cut_turn(ses.task_id)

    def _cut_turn(self, task_id: int | None) -> None:
        """Cuts the task's turn within a second, so the tool's result reaches the model sooner. The
        interruption goes with the background tasks, which are kept until they finish."""
        run = self.in_progress.get(task_id) if task_id else None
        if run is None or run.session is None:
            return
        session = run.session

        def cut() -> None:
            if session.in_turn and run.session is session:
                self._launch(session.interrupt())

        asyncio.get_running_loop().call_later(1.0, cut)

    async def _pause_by_browser(self, task_id: int, phase: str) -> None:
        task = await self.tasks.require(task_id)
        if task.state_enum not in PAUSED and task.state_enum not in FINAL:
            await self.tasks.change_state(task_id, TaskState.PAUSED_BROWSER, "Chrome not available")
        await self.tasks.update(task_id, pause_reason="Chrome not available", phase=phase)
        note_progress(self.config.paths.task(task_id), "Paused: Chrome isn't available.")
        await self.notify(
            task_id,
            f"Task {task_id} needs your Chrome and it isn't available. Open Chrome with the "
            "Claude in Chrome extension connected and I'll resume it on my own.",
        )

    # --- startup after an outage -------------------------------------------------------------

    async def recover_to_start(self) -> list[int]:
        """Resumes what was left half-done by a shutdown or an outage and notifies the user."""
        resumed: list[int] = []
        for task in await self.tasks.list_items(
            states=[
                TaskState.IN_QUEUE,
                TaskState.RUNNING,
                TaskState.WAITING_APPROVAL,
                TaskState.REVIEWING,
                TaskState.PLANNING,
                TaskState.PAUSED_SHUTDOWN,
                TaskState.IN_TRIAGE,
                TaskState.RECEIVED,
            ],
            limit=100,
        ):
            if task.pause_reason in (USER_PAUSE_REASON, OFFLINE_REASON):
                continue  # the user resumes it with /resume
            if task.id in self._live:
                continue  # already from this startup (for example, a pending Telegram message)
            state = task.state_enum
            if task.kind == "quick" and self.reply_quick is not None:
                # A cut-off quick reply is answered again as a quick reply, not as a
                # task.
                if state == TaskState.RUNNING:
                    await self.tasks.change_state(
                        task.id, TaskState.PAUSED_SHUTDOWN, "the program was cut off"
                    )
                if state in (
                    TaskState.RUNNING,
                    TaskState.IN_QUEUE,
                    TaskState.RECEIVED,
                    TaskState.IN_TRIAGE,
                    TaskState.PAUSED_SHUTDOWN,
                ):
                    self._launch(self._quick_reply(task.id))
                    continue
            if state in (TaskState.IN_TRIAGE, TaskState.RECEIVED):
                # The message was never classified: it is classified again as if it
                # had just arrived.
                await self.tasks.change_state(
                    task.id, TaskState.CANCELLED, "reclassified at startup"
                )
                await self._new_entry(task.request, Entry("text", 0, task.request))
                continue
            if state == TaskState.IN_QUEUE:
                if task.id not in self._in_queue:
                    self._in_queue.add(task.id)
                    await self.queue.put(task.id)
                resumed.append(task.id)
                continue
            if state != TaskState.PAUSED_SHUTDOWN:
                await self.tasks.change_state(
                    task.id, TaskState.PAUSED_SHUTDOWN, "the program was cut off"
                )
                await self.tasks.update(task.id, pause_reason="the program was cut off")
            await self.tasks.update(
                task.id,
                phase="plan" if state == TaskState.PLANNING else task.phase,
            )
            if await self.resume_task(task.id):
                resumed.append(task.id)
        if resumed:
            await self.notify(
                None,
                "I'm back up and running. Resuming "
                + ("task " if len(resumed) == 1 else "tasks ")
                + ", ".join(str(i) for i in resumed)
                + " without repeating what was already done.",
            )
        with contextlib.suppress(Exception):
            await self.approvals.remember_pending()
        return resumed

    # --- queries -------------------------------------------------------------------------

    async def wait_task(
        self, task_id: int, states: set[TaskState] | None = None, max_wait: float = 1800
    ) -> Task:
        """Waits until the task reaches one of the states (by default, final or waiting)."""
        goal = states or {*FINAL, *PAUSED, TaskState.WAITING_PLAN}
        limit = asyncio.get_running_loop().time() + max_wait
        while True:
            task = await self.tasks.require(task_id)
            if (
                task.state_enum in goal
                and task_id not in self.in_progress
                and task_id not in self._planning
            ):
                return task
            if asyncio.get_running_loop().time() > limit:
                raise TimeoutError(f"task {task_id} is still {task.state}")
            await asyncio.sleep(0.5)
