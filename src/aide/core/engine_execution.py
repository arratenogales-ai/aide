"""Part of the engine: the life of a task, from preparation to delivery.

Prepares the task, manages the queue of heavy tasks and takes it through its phases (plan, execution,
review, delivery) with one SDK session per task.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aide.bot.channel import Button, Entry
from aide.bot.formatting import basic_markdown_to_html, bold, truncate
from aide.common.config import Cap, ModelRole
from aide.core import prompts
from aide.core.connection import is_network_error
from aide.core.context import SessionContext
from aide.core.db import now, to_date
from aide.core.delivery import deliver, extract_block
from aide.core.engine_common import (
    NOT_NOTIFIED_KEY,
    OFFLINE_REASON,
    TaskRun,
    Triage,
    log,
    short_day,
)
from aide.core.hooks import note_progress
from aide.core.orchestrator import AgentSession, TurnResult
from aide.core.projects import existing_projects, no_context
from aide.core.session import orchestrator_options
from aide.core.states import FINAL, PAUSED, TaskState
from aide.core.tasks import Task

ERROR_RETRIES = 2
# Sizes are saved with their code; in progress.md they go in English.
SIZES = {"small": "small", "medium": "medium", "large": "large"}
NO_WIFI_NOTE = (
    "\n\nThese days the user has no wifi and the Mac connects now and then through their phone's "
    "data. They don't want you to use their Chrome unless they ask, and this task doesn't have it. "
    "If you really need it (their session on a website), don't try another way: finish and tell "
    'them that, if they want, they can ask for it by replying "use Chrome".'
)


class ExecutionMixin:
    """Preparation, queue, model, upfront plan and execution of each task."""

    # --- preparation ---------------------------------------------------------------------

    async def _extra_block(self, task: Task) -> str:
        """What is added to the task prompt: these days without wifi, in a scheduled task, don't
        use Chrome. What the user launches goes as usual."""
        block = ""
        if (
            self.config.settings.no_wifi.active
            and task.origin == "scheduled_task"
            and not task.needs_browser
        ):
            block += NO_WIFI_NOTE
        context = await self._answered_context(task.id)
        if context:
            # They asked by replying to an agent message: it goes as data, not as their
            # request.
            block += prompts.block_answered_message(context)
        return block

    async def _prepare_task(
        self, task: Task, triage: Triage, entry: Entry, caps: Cap | None = None
    ) -> None:
        size = triage.size if triage.size in ("small", "medium", "large") else "medium"
        cap = caps or getattr(self.config.settings.caps, size)
        forced = self.next_model
        if forced:
            self.next_model = None
            await self.db.kv_delete("next_model")
        # A small task doesn't get a plan just for having an external action: that action
        # already asks for approval. Large and ambiguous ones, and medium ones with external
        # actions, do.
        needs_plan = bool(
            triage.needs_plan or size == "large" or (triage.external_actions and size != "small")
        )
        browser = bool(triage.needs_real_browser)
        # These days without wifi, a scheduled task doesn't use Chrome. What the user
        # launches (via the bot, the panel or Claude Code) goes as usual: if they launch it,
        # they have a connection.
        no_chrome = (
            browser and self.config.settings.no_wifi.active and task.origin == "scheduled_task"
        )
        task = await self.tasks.update(
            task.id,
            kind="task",
            title=triage.title,
            size=size,
            needs_plan=int(needs_plan),
            needs_browser=int(browser and not no_chrome),
            external_actions=int(bool(triage.external_actions)),
            turn_cap=cap.turns,
            usd_cap=cap.usd,
            token_cap=cap.tokens,
            forced_model=forced,
        )
        self.config.paths.create_task_folder(task.id)
        await self._link_entry(entry, task.id)
        await self._save_files(task.id, entry)
        note_progress(self.config.paths.task(task.id), f"Task received ({SIZES.get(size, size)}).")
        if no_chrome:
            note_progress(
                self.config.paths.task(task.id),
                "No Chrome: while there's no wifi, scheduled tasks don't use it.",
            )
        if needs_plan:
            await self.tasks.change_state(task.id, TaskState.PLANNING, "needs a plan first")
            self._launch(self._plan(task.id))
        else:
            await self.enqueue(task.id, "ready to run")
        await self._notify_change()

    async def create_direct_task(
        self,
        text: str,
        *,
        origin: str = "simulator",
        size: str = "small",
        needs_plan: bool = False,
        needs_browser: bool = False,
        external_actions: bool = False,
        scheduled_task_id: int | None = None,
        caps: Cap | None = None,
        project: str | None = None,
        files: list[Path] | None = None,
    ) -> Task:
        """Creates a task without triage (scheduler, tests, /plan). A scheduled task can set
        its caps and its project, and carry input files."""
        task = await self.tasks.create(text, origin=origin, scheduled_task_id=scheduled_task_id)
        if project:
            task = await self.tasks.update(task.id, project=project)
        triage = Triage(
            "new_task",
            size,
            external_actions,
            None,
            needs_plan,
            needs_browser,
        )
        entry = Entry("text", 0, text, files=list(files or []))
        await self._prepare_task(task, triage, entry, caps)
        return await self.tasks.require(task.id)

    # --- cola ------------------------------------------------------------------------

    async def enqueue(self, task_id: int, reason: str) -> None:
        """Puts a task in the queue and sets its state, if not already queued."""
        task = await self.tasks.require(task_id)
        if task.state_enum != TaskState.IN_QUEUE:
            await self.tasks.change_state(task_id, TaskState.IN_QUEUE, reason)
        if task_id not in self._in_queue:
            self._in_queue.add(task_id)
            await self.queue.put(task_id)

    async def _worker(self) -> None:
        while True:
            task_id = await self.queue.get()
            self._in_queue.discard(task_id)
            try:
                await self._run(task_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                log.exception("error running task %s", task_id)
                if is_network_error(e) or not await self.connection.check(force=True):
                    # No connection: paused, like a turn that loses it.
                    task = await self.tasks.get(task_id)
                    phase = (task.phase if task else None) or "run"
                    await self._pause_offline(task_id, phase)
                else:
                    await self._fail(task_id, f"internal error: {e}")
            finally:
                self.queue.task_done()
                await self._notify_change()

    # --- model -------------------------------------------------------------------------

    async def opus_limit_active(self) -> bool:
        until = to_date(await self.db.kv_read("limit_opus_until"))
        return bool(until and until > datetime.now(UTC))

    async def temporary_sonnet_active(self) -> bool:
        """The user accepted, at the 90% weekly limit warning, to use Sonnet until the reset."""
        until = to_date(await self.db.kv_read("temporary_sonnet_until"))
        return bool(until and until > datetime.now(UTC))

    async def model_of(self, task: Task) -> tuple[ModelRole, bool]:
        """Orchestrator model for the task (kept for the whole task)."""
        models = self.config.models
        # Small tasks use a cheaper model.
        own = "orchestrator_small" if task.size == "small" else "orchestrator"
        if task.forced_model and task.forced_model in models.forceable:
            role = models.forceable[task.forced_model]
        else:
            role = self.config.role(own)
        fallback = await self.opus_limit_active()
        if not task.forced_model and await self.temporary_sonnet_active():
            fallback = True  # the orchestrator (and the reviewer) switch to Sonnet until the reset
        if fallback and role.alias == "opus":
            r = models.opus_limit_fallback
            role = ModelRole(alias=r.alias, id=r.id, effort=role.effort)
        if task.model and task.model != role.id and not fallback:
            # A task already started keeps its model (the cache depends on it).
            for candidate in [
                *models.forceable.values(),
                self.config.role("orchestrator"),
                self.config.role("orchestrator_small"),
            ]:
                if candidate.id == task.model:
                    role = candidate
                    break
        return role, fallback

    def _context(self, task: Task, phase: str) -> SessionContext:
        chrome = bool(task.needs_browser) and self.config.real_browser
        return SessionContext(
            core=self.core,
            task_id=task.id,
            folder=self.config.paths.create_task_folder(task.id),
            role="orchestrator",
            phase=phase,
            chrome=chrome,
            user_texts=[task.request],
            small=task.size == "small",
        )

    def _resume_of(self, task: Task) -> str | None:
        if not task.session_id:
            return None
        used = to_date(task.session_used)
        hours = self.config.settings.resume_session_hours
        if used and datetime.now(UTC) - used > timedelta(hours=hours):
            return None
        return task.session_id

    # --- prior plan --------------------------------------------------------------------

    async def _plan(self, task_id: int, prompt: str | None = None) -> None:
        if task_id in self._planning:
            return
        self._planning.add(task_id)
        try:
            task = await self.tasks.require(task_id)
            if task.state_enum != TaskState.PLANNING:
                await self.tasks.change_state(task_id, TaskState.PLANNING, "planning")
            role, fallback = await self.model_of(task)
            ses = self._context(task, "plan")
            await self._mark_answered(task_id, ses)
            entries = [Path(a["path"]) for a in await self.tasks.files(task_id, "entry")]
            prompt_text = prompt or prompts.prompt_plan(
                task.request,
                ses.folder,
                task_id,
                entries,
                existing_projects(self.config.paths.projects),
                extra=await self._extra_block(task),
            )
            plan_cap = self.config.settings.caps.small
            options = orchestrator_options(
                ses,
                role,
                resume=self._resume_of(task),
                max_turns=min(plan_cap.turns, max(1, task.remaining_turns)),
                max_budget_usd=min(plan_cap.usd, max(0.05, task.remaining_usd)),
                opus_fallback=fallback,
            )
            await self.tasks.update(task_id, model=role.id, phase="plan")
            run = TaskRun(task_id, ses)
            self.in_progress[task_id] = run
            async with AgentSession(ses, options, "plan") as session:
                run.session = session
                r = await session.turn(prompt_text)
            await self.tasks.update(task_id, session_id=r.session_id, session_used=now())
            if run.cancelled:
                return
            if run.paused:
                await self._pause_by_user(task_id)
                await self.tasks.update(task_id, next_step=None)
                return
            if r.kind != "ok":
                await self._handle_turn_failure(task_id, r, "plan")
                return
            plan = extract_block(r.text, "plan") or r.text.strip()
            await self.tasks.update(task_id, plan=plan)
            await self.tasks.change_state(task_id, TaskState.WAITING_PLAN, "plan proposed")
            note_progress(ses.folder, "Plan proposed to the user.")
            await self._send_plan(task_id, plan)
        finally:
            self._planning.discard(task_id)
            self.in_progress.pop(task_id, None)
            await self._notify_change()

    async def _send_plan(self, task_id: int, plan: str) -> None:
        buttons = [
            [
                Button("Approve", f"plan:{task_id}:yes"),
                Button("Change", f"plan:{task_id}:change"),
                Button("Cancel", f"plan:{task_id}:no"),
            ]
        ]
        text = "\n\n".join(
            [bold(f"Plan for task {task_id}"), basic_markdown_to_html(truncate(plan, 3300))]
        )
        await self.notify(task_id, text, buttons=buttons, html=True)

    async def _change_plan(self, task_id: int, changes: str) -> None:
        task = await self.tasks.get(task_id)
        if task is None or task.state_enum != TaskState.WAITING_PLAN:
            await self.notify(task_id, "That task is no longer waiting for a plan.")
            return
        await self.tasks.event(task_id, "user_message", changes)
        await self.tasks.change_state(
            task_id, TaskState.PLANNING, "The user asks for changes to the plan"
        )
        self._launch(self._plan(task_id, prompts.prompt_change_plan(changes)))

    # --- execution -----------------------------------------------------------------------

    async def _run(self, task_id: int) -> None:
        task = await self.tasks.require(task_id)
        if task.state_enum != TaskState.IN_QUEUE:
            return
        role, fallback = await self.model_of(task)
        ses = self._context(task, "run")
        await self._mark_answered(task_id, ses)
        if ses.chrome and not await self.ready_chrome():
            # Without Chrome it doesn't start: it pauses and resumes by itself when
            # Chrome is back.
            await self._pause_by_browser(task_id, "run")
            return
        prompt = task.next_step or self._initial_prompt(task, ses, await self._extra_block(task))
        resume = self._resume_of(task)
        if task.session_id and resume is None and task.summary and not task.next_step:
            prompt = prompts.prompt_new_session_from_summary(task.summary, ses.folder, task.request)
        await self.tasks.change_state(task_id, TaskState.RUNNING, "running")
        await self.tasks.update(task_id, model=role.id, phase="run", next_step=None)
        run = TaskRun(task_id, ses, async_task=asyncio.current_task())
        self.in_progress[task_id] = run
        await self.awake.acquire()
        await self._notify_change()
        attempts = 0
        try:
            while True:
                task = await self.tasks.require(task_id)
                options = orchestrator_options(
                    ses,
                    role,
                    resume=resume,
                    max_turns=task.remaining_turns,
                    max_budget_usd=task.remaining_usd,
                    opus_fallback=fallback,
                )
                async with AgentSession(ses, options, "orchestrator") as session:
                    run.session = session
                    r = await session.turn(prompt)
                    if r.kind == "opus_limit" and role.alias == "opus":
                        # Opus limit: continues with Sonnet in the same
                        # session.
                        r = await self._continue_with_fallback(task_id, session, r)
                        role = ModelRole(
                            alias="sonnet", id=session.current_model or role.id, effort=role.effort
                        )
                    # The work is done: the token budget no longer cuts the
                    # closing turns (delivery, review fixes, CONTEXT.md).
                    ses.done_delivery = r.kind == "ok"
                    if r.kind == "ok" and not ses.expired_google and "<delivery>" not in r.text:
                        # Without the closing format, the agent hasn't
                        # finished: it is asked once.
                        await self.tasks.event(task_id, "missing_delivery", r.text[:300])
                        r = await session.turn(prompts.prompt_missing_delivery())
                    if r.kind == "ok" and not ses.expired_google:
                        r = await self._review_and_correct(task_id, session, r)
                    if r.kind == "ok" and not ses.expired_google:
                        r = await self._ensure_project_context(task_id, session, r)
                run.session = None
                await self.tasks.update(task_id, session_id=r.session_id, session_used=now())
                resume = r.session_id
                if run.cancelled or self._stopping:
                    return  # on shutdown the task was already left in paused_shutdown
                if run.paused:
                    await self._pause_by_user(task_id)
                    return
                if ses.expired_google:
                    await self._pause_by_google(task_id)
                    return
                if r.kind == "ok":
                    await self._complete(task_id, r)
                    return
                if r.kind == "error" and not await self.connection.check(force=True):
                    # The connection dropped midway: paused until the user
                    # resumes it, with no retries that burn tokens on a flaky
                    # connection.
                    await self._pause_offline(task_id, "run")
                    return
                if r.kind == "error" and attempts < ERROR_RETRIES:
                    attempts += 1
                    wait = 10 * 3**attempts
                    log.warning(
                        "task %s: transient error (%s); retrying in %ss",
                        task_id,
                        r.detail,
                        wait,
                    )
                    await self.tasks.event(task_id, "retry", r.detail[:300], {"attempt": attempts})
                    await asyncio.sleep(wait)
                    prompt = prompts.prompt_resume(
                        await self.core.actions.done_descriptions(task_id), "API error"
                    )
                    continue
                await self._handle_turn_failure(task_id, r, "run")
                return
        finally:
            self.in_progress.pop(task_id, None)
            await self.awake.release()
            await self._notify_change()

    def _initial_prompt(self, task: Task, ses: SessionContext, extra: str = "") -> str:
        entries = sorted((ses.folder / "input").glob("*")) if ses.folder else []
        return prompts.prompt_run(
            task.request,
            ses.folder,
            task.id,
            entries,
            task.plan,
            existing_projects(self.config.paths.projects),
            task.size,
            extra=extra,
        )

    async def _ensure_project_context(
        self, task_id: int, session: AgentSession, r: TurnResult
    ) -> TurnResult:
        """If the task touched a project without updating its CONTEXT.md, it is requested before delivering."""
        root = self.config.paths.projects
        pending = no_context(session.ses.written_files, root)
        if not pending:
            return r
        await self.tasks.event(task_id, "pending_context", ", ".join(pending))
        note_progress(
            session.ses.folder,
            "CONTEXT.md still needs updating for: " + ", ".join(pending) + ".",
        )
        r_2 = await session.turn(
            prompts.prompt_update_context([root / n / "CONTEXT.md" for n in pending])
        )
        if r_2.kind != "ok":
            # The work is already done: it is delivered anyway and a note is made.
            await self.tasks.event(task_id, "context_no_updated", r_2.kind)
            return r
        return r_2

    async def _continue_with_fallback(
        self, task_id: int, session: AgentSession, r: TurnResult
    ) -> TurnResult:
        fallback = self.config.models.opus_limit_fallback
        until = r.reset or datetime.now(UTC) + timedelta(hours=5)
        await self.db.kv_put("limit_opus_until", until.isoformat(timespec="seconds"))
        await session.change_model(fallback.id)
        await self.tasks.event(
            task_id, "model_change", f"Opus limit: continuing with {fallback.id}"
        )
        await self.notify(
            task_id, f"The Opus limit has been reached. Task {task_id} continues with Sonnet."
        )
        return await session.turn(
            "The usage limit of the previous model has been reached. Continue the task where you left off."
        )

    async def _review_and_correct(
        self, task_id: int, session: AgentSession, r: TurnResult
    ) -> TurnResult:
        """Final review driven by the core, with a maximum number of rounds."""
        task = await self.tasks.require(task_id)
        if self.review is None:
            return r
        from aide.core.review import review_level

        if (await review_level(self.core, task, r.text))[0] is None:
            return r  # small task with nothing to review
        rounds = self.config.settings.review_max_rounds
        for round_number in range(1, rounds + 1):
            await self.tasks.change_state(task_id, TaskState.REVIEWING, f"review {round_number}")
            failures = await self.review(task, r.text)
            await self.tasks.update(task_id, reviews=round_number)
            if not failures:
                await self.tasks.change_state(task_id, TaskState.RUNNING, "review passed")
                return r
            await self.tasks.change_state(task_id, TaskState.RUNNING, f"fix {len(failures)} issues")
            if round_number == rounds:
                r.detail = "\n".join(failures)
                return TurnResult(
                    "ok",
                    text=r.text,
                    session_id=r.session_id,
                    detail="failures:" + "\n".join(failures),
                )
            r = await session.turn(prompts.prompt_correct(failures, round_number))
            if r.kind != "ok":
                return r
        return r

    async def _complete(self, task_id: int, r: TurnResult) -> None:
        task = await self.tasks.require(task_id)
        failures = (
            r.detail.removeprefix("failures:").splitlines()
            if r.detail.startswith("failures:")
            else None
        )
        await deliver(
            self.core,
            task,
            r.text,
            review_failures=failures,
        )
        await self.tasks.change_state(task_id, TaskState.COMPLETED, "delivered")
        delivered = await self.db.value(
            "SELECT COUNT(*) FROM telegram_messages WHERE task_id=? AND address='delivery'",
            (task_id,),
        )
        if not delivered and self.channel is not None and not self.connection.in_line:
            await self._not_notified(task_id, "finished")

    async def _handle_turn_failure(self, task_id: int, r: TurnResult, phase: str) -> None:
        task = await self.tasks.require(task_id)
        if r.kind in ("turn_cap", "usd_cap", "token_cap"):
            await self.tasks.change_state(task_id, TaskState.PAUSED_CAP, r.detail)
            await self.tasks.update(task_id, pause_reason=r.detail, phase=phase)
            buttons = [
                [
                    Button("Continue with a higher cap", f"cap:{task_id}:more"),
                    Button("Cancel", f"cap:{task_id}:no"),
                ]
            ]
            consumption = await self.usage.task_consumption(task_id)
            await self.notify(
                task_id,
                f"Task {task_id} has reached its usage cap ({r.detail}; it has used "
                f"{consumption.text()} and {task.turns_used} turns). Should I continue with a "
                "higher cap?",
                buttons=buttons,
            )
        elif r.kind in ("limit", "opus_limit"):
            until = r.reset or datetime.now(UTC) + timedelta(hours=1)
            await self.tasks.change_state(task_id, TaskState.PAUSED_LIMIT, r.detail)
            await self.tasks.update(
                task_id,
                pause_until=until.isoformat(timespec="seconds"),
                pause_reason=r.detail,
                phase=phase,
            )
            local = until.astimezone()
            await self.notify(
                task_id,
                f"A plan limit has been reached. Task {task_id} is paused and will resume on "
                f"its own around {local:%H:%M} on {short_day(local)}.",
            )
        elif r.kind == "login":
            from aide.core.authentication import credential_name, credential_steps

            credential = credential_name(self.config)
            reason = (
                f"{credential} rejected" if self.config.uses_api_key else f"{credential} expired"
            )
            await self.tasks.change_state(task_id, TaskState.PAUSED_LOGIN, reason)
            await self.tasks.update(task_id, pause_reason=reason, phase=phase)
            steps = "\n".join(f"{i}. {p}" for i, p in enumerate(credential_steps(self.config), 1))
            state = "was rejected" if self.config.uses_api_key else "has expired"
            await self.notify(
                task_id,
                f"The {credential} {state} and task {task_id} is paused. To fix it:\n{steps}",
            )
        elif r.kind == "browser":
            await self._pause_by_browser(task_id, phase)
        elif r.kind == "interrupted":
            if task.state_enum not in FINAL and task.state_enum not in PAUSED:
                await self.tasks.change_state(
                    task_id, TaskState.PAUSED_SHUTDOWN, "turn interrupted"
                )
        elif r.kind == "error" and not await self.connection.check(force=True):
            await self._pause_offline(task_id, phase)
        else:
            await self._fail(task_id, r.detail or "unknown error")

    async def _pause_offline(self, task_id: int, phase: str) -> None:
        """The connection dropped midway through the task: it stays paused, without retries, until
        the user resumes it with /resume. They are told when the connection is back."""
        task = await self.tasks.require(task_id)
        if task.state_enum in FINAL:
            return
        if task.state_enum not in PAUSED:
            await self.tasks.change_state(
                task_id, TaskState.PAUSED_SHUTDOWN, "no internet connection"
            )
        done = await self.core.actions.done_descriptions(task_id)
        await self.tasks.update(
            task_id,
            pause_reason=OFFLINE_REASON,
            phase=phase,
            next_step=None
            if phase == "plan"
            else prompts.prompt_resume(done, "the internet connection dropped"),
        )
        note_progress(self.config.paths.task(task_id), "Paused: the internet connection dropped.")
        await self.tasks.event(task_id, "no_connection", "paused until the user resumes it")
        await self._not_notified(task_id, "paused")

    async def _not_notified(self, task_id: int, that: str) -> None:
        """Notes what was left undelivered from one of their tasks for lack of connection: they are told
        in a single message when it returns."""
        pending = dict(await self.db.kv_read(NOT_NOTIFIED_KEY) or {})
        pending[str(task_id)] = that
        await self.db.kv_put(NOT_NOTIFIED_KEY, pending)

    async def _fail(self, task_id: int, reason: str) -> None:
        task = await self.tasks.get(task_id)
        if task is None or task.state_enum in FINAL:
            return
        await self.tasks.update(task_id, error=reason[:2000])
        await self.tasks.change_state(task_id, TaskState.FAILED, reason[:200])
        notified = await self.notify(task_id, f"Task {task_id} failed: {truncate(reason, 800)}")
        if not notified and self.channel is not None and not self.connection.in_line:
            await self._not_notified(task_id, "failed")
