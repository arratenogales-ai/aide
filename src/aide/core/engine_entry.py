"""Part of the engine: what arrives from the user.

Receives inputs (Telegram or simulator), routes them by code and handles what doesn't
amount to a task: files without a caption, replies to agent messages and quick
replies. What is a task continues in `engine_execution.py`.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime
from pathlib import Path

from aide.bot.channel import Button, Entry
from aide.bot.formatting import basic_markdown_to_html, truncate
from aide.core.context import SessionContext
from aide.core.engine_common import (
    CORRECTION_WAIT,
    PHOTO_EXTENSIONS,
    PHOTO_WAIT,
    PendingPhoto,
    Triage,
    log,
)
from aide.core.injection import search_instructions
from aide.core.states import FINAL, PAUSED, TaskState

# Replies to agent messages that look like a request, waiting for their button.
REPLY_KEY = "reply:pending:"
NEXT_REPLY_KEY = "reply:next_step"
# “Do it as a task” under a quick reply: the task that came out of each one.
QUICK_TO_TASK_KEY = "quick:task:"
# A quick reply that says the request is a job for a task carries the button.
RE_AS_TASK = re.compile(r"\bcomo (una )?tarea\b|\bas (a )?(separate )?task\b", re.IGNORECASE)


class EntryMixin:
    """Receiving and routing inputs, and quick replies."""

    # --- entry -------------------------------------------------------------------------

    async def receive(self, entry: Entry) -> None:
        """Handles an incoming entry, marking the connection online and logging errors."""
        # Something arrives from the user: there is a connection, even if the last check
        # said there wasn't.
        self.connection.mark_online()
        if entry.kind != "button" and self.started:
            # When the user writes, pauses for login or browser are also checked (12.9).
            self._launch(self.check_pauses())
        try:
            await self._receive(entry)
        except Exception:
            log.exception("error handling an incoming message")
            await self.notify(
                None,
                "Something went wrong on my side while handling your message. It's been logged.",
            )

    async def _receive(self, entry: Entry) -> None:
        if entry.kind == "button" and entry.button_datum:
            await self.button(entry.button_datum)
            return
        if entry.kind == "command":
            if self.commands is not None:
                await self.commands(self, entry)
            else:
                await self.notify(None, "Commands aren't available yet.")
            return
        if entry.kind == "voice":
            voice_text = await self._transcribe(entry)
            if voice_text is None:
                return
            entry = Entry(
                "text",
                entry.user_id,
                text=voice_text,
                message_id=entry.message_id,
                replies_to=entry.replies_to,
                replies_text=entry.replies_text,
                replies_to_bot=entry.replies_to_bot,
            )
        text = entry.text.strip()
        if (
            entry.kind in ("image", "document")
            and not text
            and entry.files
            and not entry.replies_to
        ):
            # A PDF or a photo without a caption: no task is created; buttons ask what
            # to do. Typing it also works: the next message is used.
            await self._ask_files([Path(a) for a in entry.files])
            return
        if not text and entry.files:
            names = ", ".join(Path(a).name for a in entry.files)
            text = f"The user sent: {names}"
        # A paper with “documento” at the start of the caption always goes to your
        # documents, without triage, even if it replies to another message, and a
        # photo with “breakfast”, “dinner”... goes to your meals.
        if await self._document(entry, text) or await self._meal(entry, text):
            return
        # A reply to a meal message is its correction: handled by code, and it never
        # goes to triage.
        if entry.replies_to and text and not entry.files:
            from aide.health import meals

            if await meals.reply(self, entry.replies_to, text):
                return
        # Free-text reply to an agent question.
        if text and await self.questions.receive_text(text):
            return
        # Text the user writes after pressing "Change" on a plan or “Edit” on a
        # document.
        if self.text_wait is not None and text:
            wait, self.text_wait = self.text_wait, None
            if wait.kind == "plan_change":
                await self._change_plan(wait.task_id, text)
                return
            if wait.kind == "document_correction":
                # What changes in a proposed document after “Edit”:
                # here, the id is its own. A file, a message that doesn't start with
                # a document field, or one that arrives after half an hour is not
                # the correction: it follows its normal path.
                from aide.paperwork import flow as paperwork_flow

                recent = datetime.now(UTC) - wait.hour <= CORRECTION_WAIT
                if recent and not entry.files and paperwork_flow.is_correction(text):
                    await paperwork_flow.correct(self, wait.task_id, text)
                    return
        # Reply to a task message: continuation without triage.
        if entry.replies_to:
            task_id = await self.tasks.task_from_message(entry.replies_to)
            if task_id:
                await self.continue_task(task_id, text, entry)
                return
        # What the user wants to do with a photo that arrived without text.
        photo, self.pending_photo = self.pending_photo, None
        recent = photo is not None and datetime.now(UTC) - photo.hour <= PHOTO_WAIT
        if photo is not None and recent and not entry.files and text:
            photos = all(r.suffix.lower() in PHOTO_EXTENSIONS for r in photo.paths)
            entry = Entry(
                "image" if photos else "document",
                entry.user_id,
                text=text,
                message_id=entry.message_id,
                replies_to=entry.replies_to,
                files=list(photo.paths),
            )
        # The photo that arrived without text, with “documento” or the meal (“dinner”) in
        # the next message.
        if await self._document(entry, text) or await self._meal(entry, text):
            return
        # Gym entries (“squat 4x8 60”) and questions about the cycle (“when is my next
        # period?”) are handled by code: health data does not go to triage.
        if text and not entry.files and not entry.replies_to:
            from aide.health import telegram as health

            if await health.free_text(self, text):
                return
        # Reply to an agent message that belongs to no task (a mail alert, a proposal, a
        # report): it does not launch a task without asking.
        if entry.replies_to and text and not entry.files and entry.replies_to_bot is not False:
            await self._reply_to_notice(text, entry)
            return
        await self._new_entry(text, entry)

    async def _ask_files(self, paths: list[Path]) -> None:
        key = secrets.token_hex(4)
        self.pending_photo = PendingPhoto(paths, datetime.now(UTC), key)
        if len(paths) == 1:
            that = (
                "the photo" if paths[0].suffix.lower() in PHOTO_EXTENSIONS else f'"{paths[0].name}"'
            )
            text = f"I got {that}. What should I do with it?"
        else:
            text = f"I got {len(paths)} files. What should I do with them?"
        buttons = [
            [
                Button("Save as a document", f"pending:doc:{key}"),
                Button("Something else", f"pending:other:{key}"),
            ],
        ]
        if all(r.suffix.lower() in PHOTO_EXTENSIONS for r in paths):
            # A photo may be of a meal.
            buttons.insert(0, [Button("It's food", f"pending:com:{key}")])
        await self.notify(None, text, buttons=buttons)

    async def files_button(self, action: str, key: str) -> None:
        """What the user chooses for files that arrived without a caption: save them as
        documents or “Something else”, which waits for them to say what."""
        photo = self.pending_photo
        if photo is None or photo.key != key:
            await self.notify(None, "Those files are no longer waiting: send them to me again.")
            return
        if action == "other":
            photo.hour = datetime.now(UTC)
            how_many = "it" if len(photo.paths) == 1 else "them"
            await self.notify(
                None, f"Tell me what you'd like me to do with {how_many} and I'll take a look."
            )
            return
        if action == "com":
            # “It's food”: which meal is still unknown; the photo stays pending
            # until the button.
            from aide.health import telegram as health

            photo.hour = datetime.now(UTC)
            await self.notify(None, "Which meal is it?", buttons=health.kind_buttons(key))
            return
        self.pending_photo = None
        if action == "doc":
            from aide.paperwork import flow as paperwork_flow

            await paperwork_flow.since_telegram(self, "document", photo.paths)

    async def _meal(self, entry: Entry, text: str) -> bool:
        """A photo with “breakfast”, “lunch”, “dinner” or “snack” (or in Spanish) at the start of the
        caption: goes to your meals."""
        if not (entry.files and text and entry.kind in ("image", "document")):
            return False
        from aide.health import telegram as health

        return await health.photo(self, text, entry.files)

    async def _document(self, entry: Entry, text: str) -> bool:
        """A PDF or a photo with “documento” at the start of the caption: goes to your documents."""
        if not (entry.files and text and entry.kind in ("image", "document")):
            return False
        from aide.paperwork import flow

        return await flow.since_telegram(self, text, [Path(a) for a in entry.files])

    async def _transcribe(self, entry: Entry) -> str | None:
        """Voice note: transcribed locally and the transcript is shown."""
        if self.transcribe is None or not entry.files:
            await self.notify(
                None, "I can't transcribe voice notes right now. Please type it out for me."
            )
            return None
        try:
            text = (await self.transcribe(Path(entry.files[0]))).strip()
        except Exception as e:
            log.exception("error transcribing a voice note")
            await self.notify(None, f"I couldn't transcribe the voice note ({type(e).__name__}).")
            return None
        if not text:
            await self.notify(None, "I couldn't make out any words in the voice note.")
            return None
        await self.notify(None, f'Transcript: "{text}"')
        return text

    async def _new_entry(self, text: str, entry: Entry, can_continue: bool = True) -> None:
        """A message that doesn't reply to a task. `can_continue` is false when it comes from a
        task that can no longer be continued, so it doesn't loop back to it."""
        recent = await self.tasks.list_items(limit=8, kind="task")
        triage = Triage()
        if self.triage is not None:
            triage_task = await self.tasks.create(
                text, origin=self._origin(entry), state=TaskState.RECEIVED
            )
            await self.tasks.change_state(triage_task.id, TaskState.IN_TRIAGE, "triage")
            # With a photo, triage knows the question is about an image.
            photo_notice = (
                "[The user sends a photo with this text] " if entry.kind == "image" else ""
            )
            triage = await self.triage(photo_notice + text, recent, triage_task.id)
            await self.tasks.event(triage_task.id, "triage", triage.kind, triage.__dict__)
            task = triage_task
        else:
            task = await self.tasks.create(
                text, origin=self._origin(entry), state=TaskState.RECEIVED
            )

        if triage.kind == "continuation" and triage.related_task:
            related = await self.tasks.get(triage.related_task)
            if (
                related is None
                or related.state_enum in FINAL
                or related.state_enum in PAUSED
                or not can_continue
            ):
                # A finished or paused task is only continued by replying to one of
                # its messages: the message is a new task.
                triage.kind, triage.related_task, related = "new_task", None, None
            if related and related.kind == "task":
                # The message is recorded as routed to the related task.
                await self.tasks.update(
                    task.id,
                    kind="message",
                    parent_task=related.id,
                    summary=f"Continuation of task {related.id}",
                )
                await self.tasks.change_state(task.id, TaskState.COMPLETED, "continuation")
                await self.continue_task(related.id, text, entry)
                return
        if triage.kind in ("conversation", "quick_question") and self.reply_quick is not None:
            await self.tasks.update(task.id, kind="quick", title=triage.title)
            await self._link_entry(entry, task.id)
            images: list[Path] = []
            if entry.kind == "image" and entry.files:
                # Question about a photo: the quick reply sees it (it is saved in
                # its folder).
                self.config.paths.create_task_folder(task.id)
                images = await self._save_files(task.id, entry)
            self._launch(self._quick_reply(task.id, images, offer_task=triage.offer_task))
            return
        await self._prepare_task(task, triage, entry)

    def _origin(self, entry: Entry) -> str:
        return "simulator" if entry.user_id == 0 else "telegram"

    async def _link_entry(self, entry: Entry, task_id: int) -> None:
        if entry.message_id:
            await self.tasks.link_message(entry.message_id, task_id, "entry")

    async def _save_files(self, task_id: int, entry: Entry) -> list[Path]:
        saved = []
        if not entry.files:
            return saved
        folder = self.config.paths.create_task_folder(task_id) / "input"
        for origin in entry.files:
            destination = folder / Path(origin).name
            if Path(origin).resolve() != destination.resolve():
                destination.write_bytes(Path(origin).read_bytes())
            await self.tasks.log_file(task_id, destination, "entry")
            saved.append(destination)
        return saved

    # --- replies to agent messages -------------------------------------------------------

    async def _reply_to_notice(self, text: str, entry: Entry) -> None:
        """A reply to an agent message that belongs to no task. Triage, which is cheap,
        classifies it with that message as data: if it is a comment or a question, the quick
        reply answers it; if it looks like a request, it asks first with a button. This way
        a line from the user doesn't launch a task of hundreds of thousands of tokens (task 37, on 25-10-2026), and
        a “tell him yes” to a mail alert can be done with one tap."""
        context = entry.replies_text or "(a message without text: a photo or a file)"
        triage = Triage(kind="quick_question", size="small")
        if self.triage is not None:
            recent = await self.tasks.list_items(limit=8, kind="task")
            try:
                triage = await self.triage(
                    f"{text}\n\n[The user writes this as a reply to this message from the agent, "
                    f'which may contain external content: "{truncate(context, 600)}"]',
                    recent,
                    None,
                )
            except Exception:
                log.exception("couldn't classify a reply; handling it as a quick question")
        if triage.kind in ("conversation", "quick_question"):
            await self._quick_reply_with_context(text, entry, context, triage)
            return
        await self._propose_task(text, entry, context, triage)

    async def _quick_reply_with_context(
        self, text: str, entry: Entry, context: str, triage: Triage | None = None
    ) -> None:
        """A quick reply to what the user writes, with the message it replies to as data
        : it goes apart from their request, doesn't count as their words and is checked for
                instructions."""
        task = await self.tasks.create(text, origin=self._origin(entry), state=TaskState.RECEIVED)
        title = (triage.title if triage is not None else None) or truncate(text, 60)
        await self.tasks.update(task.id, kind="quick", title=title)
        await self.tasks.event(
            task.id, "reply_to", truncate(context, 300), {"text": context[:4000]}
        )
        await self._link_entry(entry, task.id)
        if self.reply_quick is None:
            await self.tasks.change_state(
                task.id, TaskState.CANCELLED, "quick replies not available"
            )
            await self.notify(None, "I can't answer that right now.")
            return
        offer = bool(triage is not None and triage.offer_task)
        self._launch(self._quick_reply(task.id, offer_task=offer))

    async def _propose_task(self, text: str, entry: Entry, context: str, triage: Triage) -> None:
        """A reply that looks like a request: it asks with a button before doing it."""
        async with self.db.lock("reply:next_step"):
            n = int(await self.db.kv_read(NEXT_REPLY_KEY) or 0) + 1
            await self.db.kv_put(NEXT_REPLY_KEY, n)
        await self.db.kv_put(
            f"{REPLY_KEY}{n}",
            {
                "text": text,
                "context": context[:4000],
                "triage": triage.__dict__,
                "message_id": entry.message_id,
                "origin": self._origin(entry),
            },
        )
        await self.notify(
            None,
            f'This looks like a job: "{truncate(text, 150)}". Should I do it as a task?',
            buttons=[[Button("Yes, do it", f"reply:yes:{n}"), Button("No need", f"reply:no:{n}")]],
        )

    async def _reply_button(self, n: int, do: bool) -> None:
        """“Yes, do it” or “No need” for a reply that looked like a request."""
        key = f"{REPLY_KEY}{n}"
        async with self.db.lock(key):
            pending = await self.db.kv_read(key)
            if pending:
                await self.db.kv_delete(key)
        if not pending:
            await self.notify(None, "That was already decided.")
            return
        if not do:
            await self.notify(None, "OK, I won't do anything.")
            return
        text = pending["text"]
        entry = Entry("text", 0, text=text, message_id=pending.get("message_id"))
        fields = Triage.__dataclass_fields__
        triage = Triage(**{k: v for k, v in pending["triage"].items() if k in fields})
        if triage.kind == "continuation" and triage.related_task:
            await self.continue_task(triage.related_task, text, entry)
            return
        task = await self.tasks.create(
            text, origin=pending.get("origin") or "telegram", state=TaskState.RECEIVED
        )
        context = str(pending.get("context") or "")
        await self.tasks.event(task.id, "reply_to", truncate(context, 300), {"text": context})
        triage.kind = "new_task"
        await self._prepare_task(task, triage, entry)

    async def _do_it_as_task(self, quick_id: int) -> None:
        """“Do it as a task” under a quick reply: the same request, as a task with
        its folder and what triage saw. Only once per reply."""
        key = f"{QUICK_TO_TASK_KEY}{quick_id}"
        async with self.db.lock(key):
            done = await self.db.kv_read(key)
            quick = await self.tasks.get(quick_id)
            if done:
                await self.notify(None, f"It's already underway as task {done}.")
                return
            if quick is None or quick.kind != "quick":
                await self.notify(None, "That reply can no longer be turned into a task.")
                return
            task = await self.tasks.create(
                quick.request, origin=quick.origin, state=TaskState.RECEIVED
            )
            await self.db.kv_put(key, task.id)
        events = await self.tasks.events(quick_id, ["triage"])
        data = (events[-1].get("data") or {}) if events else {}
        fields = {k: v for k, v in data.items() if k in Triage.__dataclass_fields__}
        triage = Triage(**{**fields, "kind": "new_task", "offer_task": False})
        triage.title = triage.title or quick.title
        context = await self._answered_context(quick_id)
        if context:
            await self.tasks.event(task.id, "reply_to", truncate(context, 300), {"text": context})
        await self.tasks.event(task.id, "since_quick", str(quick_id))
        await self._prepare_task(task, triage, Entry("text", 0, text=quick.request))

    async def _answered_context(self, task_id: int) -> str | None:
        """The message the user was replying to when asking for this task or question, if any."""
        events = await self.tasks.events(task_id, ["reply_to"])
        if not events:
            return None
        data = events[-1].get("data") or {}
        return str(data.get("text") or events[-1].get("summary") or "") or None

    async def _mark_answered(self, task_id: int, ses: SessionContext) -> None:
        """If the message being replied to contains something that looks like an instruction, the session
        knows (approvals flag it, as with any external content)."""
        context = await self._answered_context(task_id)
        suspicion = search_instructions(context or "")
        if suspicion:
            ses.suspicious_injection = f'the message the user was replying to: "{suspicion}"'

    # --- quick reply ---------------------------------------------------------------------

    async def _quick_reply(
        self, task_id: int, images: list[Path] | None = None, offer_task: bool = False
    ) -> None:
        task = await self.tasks.require(task_id)
        self._quick.add(task_id)
        try:
            await self.tasks.change_state(task_id, TaskState.RUNNING, "quick reply")
            assert self.reply_quick is not None
            # The message the user was replying to, if any, goes separately as data
            # .
            context = await self._answered_context(task_id)
            extra = {"context": context} if context else {}
            if images:
                text = await self.reply_quick(task, images, **extra)
            else:
                text = await self.reply_quick(task, **extra)
            await self.tasks.update(task_id, result=text, summary=truncate(text, 400))
            await self.tasks.index_history(await self.tasks.require(task_id), [])
            await self.tasks.change_state(task_id, TaskState.COMPLETED, "answered")
            # If triage was unsure, or the reply says it is a job for a task, it goes
            # with the “Do it as a task” button.
            buttons = (
                [[Button("Do it as a task", f"quick:task:{task_id}")]]
                if offer_task or RE_AS_TASK.search(text)
                else None
            )
            sent = await self.notify(
                task_id, basic_markdown_to_html(text), html=True, buttons=buttons
            )
            if not sent and self.channel is not None and not self.connection.in_line:
                await self._not_notified(task_id, "reply")
        except Exception as e:
            log.exception("error in quick reply %s", task_id)
            await self._fail(task_id, f"I couldn't answer: {e}")
        finally:
            self._quick.discard(task_id)
            await self._notify_change()
