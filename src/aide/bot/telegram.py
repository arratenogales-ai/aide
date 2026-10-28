"""Telegram bot: long polling, only the user's own user.

- The user's messages are processed in order (also the pending ones at startup, which
  Telegram keeps for up to 24 hours).
- Buttons are handled without waiting for the previous message to finish, so that an
  approval is never blocked behind something else.
- Any other sender is ignored and logged.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LinkPreviewOptions,
    Update,
)
from telegram.constants import ChatAction, ParseMode
from telegram.error import BadRequest, NetworkError, RetryAfter, TimedOut
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from aide.bot.channel import Button, Entry, remove_html
from aide.bot.formatting import TELEGRAM_LIMIT

log = logging.getLogger(__name__)

COMMANDS = [
    ("start", "Introduction and list of commands"),
    ("help", "List of commands"),
    ("status", "Tasks running, waiting for you or paused, and plan limits"),
    ("tasks", "Last ten tasks"),
    ("task", "Progress and details of a task: /task 12"),
    ("stop", "Cancel a task: /stop 12"),
    ("pause", "Pause a task: /pause 12"),
    ("resume", "Resume a task: /resume 12"),
    ("plan", "Task with a plan first: /plan text"),
    ("opus", "Use Opus for the next task"),
    ("sonnet", "Use Sonnet for the next task"),
    ("max", "Use the top model (Fable) for the next task"),
    ("usage", "Plan limits and usage per task"),
    ("memory", "See the saved memory"),
    ("forget", "Delete a fact from memory: /forget text"),
    ("schedule", "Schedule a task: /schedule text"),
    ("scheduled", "Scheduled tasks"),
    ("profile", "See your current profile"),
    ("panel", "Address of the local panel"),
    ("google", "Renew the Google authorization now"),
    ("undo_improvement", "Undo an applied improvement: /undo_improvement 3"),
    ("documents", "Read the new documents in the inbox"),
    ("period", "Your cycle; /period today, /period 24-10 or /period end"),
    ("gym", "Your week at the gym"),
    ("meals", "What you ate today; /meals week"),
    ("water", "Log water: /water or /water 500"),
    ("sleep", "Log sleep: /sleep 7.5 or /sleep 23:30-7:00"),
    ("steps", "Log steps: /steps 8000"),
    ("weight", "Log your weight: /weight 61.5"),
    ("health", "Your day against your targets, and your health profile"),
]


# An album (several files at once) is considered complete when it goes this long without new parts.
ALBUM_WAIT = 2.0


def join(parts: list[Entry]) -> Entry:
    """The parts of an album, in a single entry: all the files and the caption, which Telegram puts on
    only one of them."""
    parts = sorted(parts, key=lambda e: e.message_id or 0)
    with_caption = next((p for p in parts if p.text.strip()), parts[0])
    answered = next((p for p in parts if p.replies_to), with_caption)
    return Entry(
        "image" if all(p.kind == "image" for p in parts) else "document",
        parts[0].user_id,
        text=with_caption.text,
        message_id=with_caption.message_id,
        replies_to=answered.replies_to,
        replies_text=answered.replies_text,
        replies_to_bot=answered.replies_to_bot,
        files=[a for p in parts for a in p.files],
        file_name=parts[0].file_name,
    )


class Albums:
    """Groups the parts of each album. A part counts from when it arrives, before downloading it, so as
    not to close the album while the next one downloads."""

    def __init__(self, deliver: Any, wait: float = ALBUM_WAIT) -> None:
        self._deliver = deliver
        self._wait = wait
        self._parts: dict[str, list[Entry]] = {}
        self._pending: dict[str, int] = {}
        self._last: dict[str, float] = {}
        self._tasks: set[asyncio.Task] = set()

    def starts(self, media_group: str) -> None:
        """Marks a new part of a group as pending and starts its closing task if new."""
        self._pending[media_group] = self._pending.get(media_group, 0) + 1
        self._last[media_group] = time.monotonic()
        if media_group not in self._parts:
            self._parts[media_group] = []
            task = asyncio.ensure_future(self._close(media_group))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

    def ends(self, media_group: str, entry: Entry | None) -> None:
        """Marks a part of a group as done, storing its entry if there is one."""
        if entry is not None:
            self._parts.setdefault(media_group, []).append(entry)
        self._pending[media_group] = max(0, self._pending.get(media_group, 1) - 1)
        self._last[media_group] = time.monotonic()

    async def _close(self, media_group: str) -> None:
        while True:
            await asyncio.sleep(self._wait)
            still = time.monotonic() - self._last.get(media_group, 0) >= self._wait
            if self._pending.get(media_group, 0) == 0 and still:
                break
        parts = self._parts.pop(media_group, [])
        self._pending.pop(media_group, None)
        self._last.pop(media_group, None)
        if parts:
            await self._deliver(join(parts))


def _keyboard(buttons: list[list[Button]] | None) -> InlineKeyboardMarkup | None:
    if not buttons:
        return None
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(b.text, callback_data=b.datum) for b in row] for row in buttons]
    )


class TelegramChannel:
    """Implements `Channel` over the Telegram bot API (HTML mode)."""

    def __init__(self, bot, chat_id: int) -> None:
        self.bot = bot
        self.chat_id = chat_id

    async def _with_retries(self, call, *args, **kwargs) -> Any:
        for attempt in range(4):
            try:
                return await call(*args, **kwargs)
            except BadRequest:
                # Request error (HTML, message not modified...): retrying is
                # useless. It goes before NetworkError because BadRequest is a
                # subclass of it.
                raise
            except RetryAfter as e:
                wait = float(getattr(e, "retry_after", 3) or 3)
                await asyncio.sleep(min(wait, 60))
            except (TimedOut, NetworkError):
                if attempt == 3:
                    raise
                await asyncio.sleep(2**attempt)
        raise RuntimeError("Telegram did not respond")

    async def send(self, text: str, *, buttons=None, replies_to=None) -> int:
        """Sends an HTML message to the chat, truncating text over the Telegram limit."""
        if len(text) > TELEGRAM_LIMIT:
            text = text[: TELEGRAM_LIMIT - 20] + "\n[...]"
        try:
            message = await self._with_retries(
                self.bot.send_message,
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(buttons),
                reply_to_message_id=replies_to,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except BadRequest as e:
            if not _error_of_format(e):
                raise
            # Malformed HTML: sent as plain text so the message is not lost.
            log.warning("Telegram rejected the HTML (%s); sending plain text", e)
            message = await self._with_retries(
                self.bot.send_message,
                chat_id=self.chat_id,
                text=remove_html(text)[:TELEGRAM_LIMIT],
                reply_markup=_keyboard(buttons),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        return message.message_id

    async def edit(self, message_id: int, text: str, *, buttons=None) -> None:
        """Edits a chat message, truncating it and logging errors other than not modified."""
        try:
            await self._with_retries(
                self.bot.edit_message_text,
                text=text[:TELEGRAM_LIMIT],
                chat_id=self.chat_id,
                message_id=message_id,
                parse_mode=ParseMode.HTML,
                reply_markup=_keyboard(buttons),
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
        except BadRequest as e:
            if "not modified" not in str(e).lower():
                log.warning("could not edit message %s: %s", message_id, e)

    async def send_file(self, path: Path, text: str | None = None) -> int:
        """Sends a file as a document with a caption cut to 1000 characters; returns its id."""
        with Path(path).open("rb") as f:
            message = await self._with_retries(
                self.bot.send_document,
                chat_id=self.chat_id,
                document=f,
                filename=Path(path).name,
                caption=(text or "")[:1000] or None,
            )
        return message.message_id

    async def send_image(self, path: Path, text: str | None = None) -> int:
        """Sends an image as a photo, or as a file if over about 9.5 MB; returns its id."""
        if Path(path).stat().st_size > 9_500_000:
            return await self.send_file(path, text)
        with Path(path).open("rb") as f:
            message = await self._with_retries(
                self.bot.send_photo,
                chat_id=self.chat_id,
                photo=f,
                caption=(text or "")[:1000] or None,
            )
        return message.message_id

    async def writing(self) -> None:
        await self.bot.send_chat_action(chat_id=self.chat_id, action=ChatAction.TYPING)


def _error_of_format(error: Exception) -> bool:
    text = str(error).lower()
    return any(s in text for s in ("parse", "entities", "can't find end", "unsupported start tag"))


def is_allowed(update: Update, allowed_user: int | None) -> bool:
    user = update.effective_user
    return allowed_user is not None and user is not None and user.id == allowed_user


def answered_text(message: Any) -> str | None:
    """The text of the message it replies to, trimmed."""
    answered = getattr(message, "reply_to_message", None)
    if answered is None:
        return None
    text = (answered.text or answered.caption or "").strip()
    return text[:1000] or None


def answered_is_from_bot(message: Any) -> bool | None:
    """Whether the message it replies to was sent by the bot (None if unknown)."""
    answered = getattr(message, "reply_to_message", None)
    author = getattr(answered, "from_user", None) if answered is not None else None
    return None if author is None else bool(getattr(author, "is_bot", False))


def entry_from_message(update: Update) -> Entry | None:
    """Converts a text message or a command into an Entry (without downloading files)."""
    message = update.effective_message
    if message is None or update.effective_user is None:
        return None
    replies_to = message.reply_to_message.message_id if message.reply_to_message else None
    text = message.text or message.caption or ""
    base = dict(
        user_id=update.effective_user.id,
        message_id=message.message_id,
        replies_to=replies_to,
        replies_text=answered_text(message),
        replies_to_bot=answered_is_from_bot(message),
    )
    if text.startswith("/"):
        head, _, rest = text.partition(" ")
        command = head[1:].split("@")[0].lower()
        return Entry("command", text=text, command=command, arguments=rest.strip(), **base)
    return Entry("text", text=text, **base)


class TelegramBot:
    """The Telegram bot that holds the app, allowed user, engine and album handling."""

    def __init__(
        self, token: str, allowed_user: int | None, engine, downloads_folder: Path
    ) -> None:
        self.allowed_user = allowed_user
        self.engine = engine
        self.downloads = downloads_folder
        self.app: Application = ApplicationBuilder().token(token).concurrent_updates(True).build()
        self._order = asyncio.Lock()
        self._albums = Albums(self._in_order)
        self.channel: TelegramChannel | None = None
        self.ignored = 0
        self._polling_notice = -1e9  # when the last polling network error was logged
        self._logs: set[asyncio.Future] = set()
        self.app.add_handler(CallbackQueryHandler(self._button))
        self.app.add_handler(MessageHandler(filters.COMMAND, self._message))
        self.app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO, self._voice))
        self.app.add_handler(MessageHandler(filters.Document.ALL, self._document))
        self.app.add_handler(MessageHandler(filters.PHOTO, self._photo))
        self.app.add_handler(MessageHandler(filters.TEXT, self._message))
        self.app.add_handler(MessageHandler(filters.ALL, self._other))

    async def start(self) -> TelegramChannel:
        """Starts the Telegram app and returns its channel, requiring an allowed user id."""
        await self.app.initialize()
        if self.allowed_user is None:
            raise RuntimeError("TELEGRAM_ALLOWED_USER_ID is missing in .env")
        self.channel = TelegramChannel(self.app.bot, self.allowed_user)
        await self.app.start()
        with contextlib_suppress():
            await self.app.bot.set_my_commands([BotCommand(n, d) for n, d in COMMANDS])
        # Without discarding the pending ones: Telegram keeps them up to 24 hours and they
        # are processed in order. On these days without wifi, each request waits longer (up
        # to 50 s) if there are no messages: they still arrive instantly, with far fewer
        # requests and less data.
        config = getattr(self.engine, "config", None)
        no_wifi = config.settings.no_wifi if config is not None else None
        await self.app.updater.start_polling(
            drop_pending_updates=False,
            allowed_updates=["message", "callback_query"],
            timeout=no_wifi.telegram_polling_seconds if no_wifi and no_wifi.active else 10,
            error_callback=self._error_polling,
        )
        return self.channel

    def _error_polling(self, error: Exception) -> None:
        """An error when requesting messages from Telegram. With no connection, a single line
        until it returns, instead of a traceback every 30 seconds; anything else, with its
        traceback."""
        try:
            from aide.core.connection import is_network_error

            if not is_network_error(error):
                log.error("error fetching messages from Telegram: %s", error, exc_info=error)
                return
            # A single failure does not mark the connection as lost: the next check
            # really tests it. The log line, at most one every five minutes.
            connection = getattr(self.engine, "connection", None)
            if connection is not None:
                connection.suspect()
            now = time.monotonic()
            if now - self._polling_notice >= 300:
                self._polling_notice = now
                log.warning("Telegram is not responding: offline (%s)", type(error).__name__)
        except Exception:  # the Telegram loop would stop if this failed
            pass

    async def stop(self) -> None:
        """Stops the updater, the app and then shuts it down, ignoring errors."""
        with contextlib_suppress():
            await self.app.updater.stop()
        with contextlib_suppress():
            await self.app.stop()
        with contextlib_suppress():
            await self.app.shutdown()

    def _allowed(self, update: Update) -> bool:
        if is_allowed(update, self.allowed_user):
            return True
        self.ignored += 1
        user = update.effective_user
        log.warning(
            "ignored message from a user who is not allowed: id=%s name=%s",
            user.id if user else None,
            user.username if user else None,
        )
        # It stays in the timeline for the weekly maintenance security summary.
        tasks = getattr(self.engine, "tasks", None)
        if tasks is None:
            return False
        event = tasks.event(
            None,
            "user_no_allowed",
            f"id {user.id if user else '?'}",
            {"id": user.id if user else None},
        )
        task = asyncio.ensure_future(event)
        self._logs.add(task)
        task.add_done_callback(self._logs.discard)
        return False

    async def _in_order(self, entry: Entry) -> None:
        async with self._order:
            await self.engine.receive(entry)

    async def _message(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        if not self._allowed(update):
            return
        entry = entry_from_message(update)
        if entry is not None:
            await self._in_order(entry)

    async def _button(self, update: Update, _context: ContextTypes.DEFAULT_TYPE) -> None:
        query = update.callback_query
        if query is None:
            return
        if not self._allowed(update):
            with contextlib_suppress():
                await query.answer()
            return
        with contextlib_suppress():
            await query.answer()
        await self.engine.receive(
            Entry("button", update.effective_user.id, button_datum=query.data)
        )

    async def _download(self, file_id: str, name: str, message_id: int) -> Path:
        folder = self.downloads / str(message_id)
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / Path(name).name
        file = await self.app.bot.get_file(file_id)
        await file.download_to_drive(custom_path=destination)
        return destination

    async def _with_file(self, update: Update, file_id: str, name: str, kind: str) -> None:
        if not self._allowed(update):
            return
        message = update.effective_message
        media_group = getattr(message, "media_group_id", None)
        if media_group:
            self._albums.starts(media_group)
        try:
            path = await self._download(file_id, name, message.message_id)
        except Exception as e:
            log.exception("could not download the file")
            if media_group:
                self._albums.ends(media_group, None)
            if self.channel:
                await self.channel.send(f"I couldn't download the file ({type(e).__name__}).")
            return
        replies_to = message.reply_to_message.message_id if message.reply_to_message else None
        entry = Entry(
            kind,
            update.effective_user.id,
            text=message.caption or "",
            message_id=message.message_id,
            replies_to=replies_to,
            replies_text=answered_text(message),
            replies_to_bot=answered_is_from_bot(message),
            files=[path],
            file_name=name,
        )
        if media_group:
            # Several at once: they are grouped with the rest of the album and its
            # caption.
            self._albums.ends(media_group, entry)
            return
        await self._in_order(entry)

    async def _document(self, update: Update, _context) -> None:
        doc = update.effective_message.document
        await self._with_file(
            update, doc.file_id, doc.file_name or f"file-{doc.file_unique_id}", "document"
        )

    async def _photo(self, update: Update, _context) -> None:
        photo = update.effective_message.photo[-1]
        await self._with_file(
            update, photo.file_id, f"photo-{update.effective_message.message_id}.jpg", "image"
        )

    async def _voice(self, update: Update, _context) -> None:
        message = update.effective_message
        audio = message.voice or message.audio
        name = getattr(audio, "file_name", None) or f"voice-{message.message_id}.ogg"
        await self._with_file(update, audio.file_id, name, "voice")

    async def _other(self, update: Update, _context) -> None:
        if not self._allowed(update):
            return
        if self.channel:
            await self.channel.send(
                "I can't handle this kind of message yet. Send me text, voice, documents or photos."
            )


class contextlib_suppress:
    """Ignores Telegram errors in side operations (answering buttons, closing)."""

    def __enter__(self) -> Any:
        return self

    def __exit__(self, kind, value, trace) -> None:
        if value is not None:
            log.debug("minor Telegram operation failed: %s", value)
        return True

    async def __aenter__(self) -> Any:
        return self

    async def __aexit__(self, kind, value, trace) -> None:
        return self.__exit__(kind, value, trace)
