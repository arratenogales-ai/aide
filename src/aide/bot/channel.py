"""Channel interface with the user and input types.

The core only talks to a `Channel`. The Telegram bot, the simulator and the tests implement this
same interface, so the simulator follows the same path as Telegram. The texts the core sends are in
Telegram HTML (already escaped with `format_.esc`).
"""

from __future__ import annotations

import asyncio
import itertools
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

EntryKind = Literal["text", "voice", "document", "image", "button", "command"]


@dataclass(frozen=True)
class Button:
    """A Telegram inline button with its text and callback data, limited to 64 bytes."""

    text: str
    datum: str  # Telegram callback_data (maximum 64 bytes)

    def __post_init__(self) -> None:
        if len(self.datum.encode()) > 64:
            raise ValueError(f"button data too long: {self.datum}")


@dataclass
class Entry:
    """An incoming item from the user: kind, text, reply info, files and button data."""

    kind: EntryKind
    user_id: int
    text: str = ""
    message_id: int | None = None
    replies_to: int | None = None
    # The text of the message it replies to, if it is a reply, and whether that message was the
    # bot's (None if unknown).
    replies_text: str | None = None
    replies_to_bot: bool | None = None
    files: list[Path] = field(default_factory=list)
    button_datum: str | None = None
    command: str | None = None
    arguments: str = ""
    file_name: str | None = None


class Channel(Protocol):
    """The interface the bot uses to send and edit messages in a chat."""

    async def send(
        self,
        text: str,
        *,
        buttons: list[list[Button]] | None = None,
        replies_to: int | None = None,
    ) -> int: ...

    async def edit(
        self, message_id: int, text: str, *, buttons: list[list[Button]] | None = None
    ) -> None: ...

    async def send_file(self, path: Path, text: str | None = None) -> int: ...

    async def send_image(self, path: Path, text: str | None = None) -> int: ...

    async def writing(self) -> None: ...


def remove_html(text: str) -> str:
    """Strips HTML tags from text and decodes the basic HTML entities."""
    text = re.sub(r"<[^>]+>", "", text)
    return (
        text.replace("&lt;", "<").replace("&gt;", ">").replace("&quot;", '"').replace("&amp;", "&")
    )


@dataclass
class OutgoingMessage:
    """A message recorded as sent, with its kind, text, buttons and file path."""

    id: int
    kind: Literal["text", "file", "image", "edit"]
    text: str
    buttons: list[list[Button]] | None = None
    path: Path | None = None
    replies_to: int | None = None

    @property
    def buttons_data(self) -> list[str]:
        return [b.datum for row in (self.buttons or []) for b in row]

    @property
    def plain_text(self) -> str:
        return remove_html(self.text)


class FakeChannel:
    """In-memory channel for the simulator and the tests.

    `on_send` is called with each outgoing message (for example, to press buttons by script).
    """

    def __init__(
        self,
        print_out: bool = False,
        on_send: Callable[[OutgoingMessage], Awaitable[None]] | None = None,
    ) -> None:
        self.messages: list[OutgoingMessage] = []
        self.print_out = print_out
        self.on_send = on_send
        self._ids = itertools.count(1000)
        self.writing_times = 0
        self.new = asyncio.Event()

    def _record(self, message: OutgoingMessage) -> int:
        self.messages.append(message)
        self.new.set()
        if self.print_out:
            header = {
                "text": "BOT",
                "file": "FILE",
                "image": "IMAGE",
                "edit": "EDIT",
            }[message.kind]
            extra = f" [{message.path}]" if message.path else ""
            print(f"\n[{header} #{message.id}]{extra}\n{message.plain_text}")
            for row in message.buttons or []:
                print("   " + "  ".join(f"[{b.text}]" for b in row))
        return message.id

    async def _notify(self, message: OutgoingMessage) -> None:
        if self.on_send is not None:
            await self.on_send(message)

    async def send(self, text, *, buttons=None, replies_to=None) -> int:
        """Records a text message in memory and returns its id."""
        m = OutgoingMessage(next(self._ids), "text", text, buttons, replies_to=replies_to)
        self._record(m)
        await self._notify(m)
        return m.id

    async def edit(self, message_id, text, *, buttons=None) -> None:
        m = OutgoingMessage(message_id, "edit", text, buttons)
        self._record(m)

    async def send_file(self, path, text=None) -> int:
        """Records a file message with an optional caption and returns its id."""
        m = OutgoingMessage(next(self._ids), "file", text or "", None, path=Path(path))
        self._record(m)
        await self._notify(m)
        return m.id

    async def send_image(self, path, text=None) -> int:
        """Records an image message with an optional caption and returns its id."""
        m = OutgoingMessage(next(self._ids), "image", text or "", None, path=Path(path))
        self._record(m)
        await self._notify(m)
        return m.id

    async def writing(self) -> None:
        self.writing_times += 1

    # utilities for tests
    def texts(self) -> list[str]:
        return [m.plain_text for m in self.messages]

    def last_with_buttons(self, prefix: str = "") -> OutgoingMessage | None:
        """Returns the latest message with a button whose data starts with the prefix."""
        for m in reversed(self.messages):
            if m.buttons and any(d.startswith(prefix) for d in m.buttons_data):
                return m
        return None


class GuardedChannel:
    """Wraps a channel so that a Telegram failure never stops the agent.

       Errors are logged with a hint for the user and the methods return None. With no connection
    nothing is attempted: warnings are not stored to send them all on return, and a
       network failure marks the connection as down with a single log line.
    """

    def __init__(self, channel: Channel, connection=None) -> None:
        self.channel = channel
        self.connection = connection
        self.last_error: str | None = None

    async def _no_network(self, operation: str) -> bool:
        """If there is no connection (confirmed, not an old state): then nothing is attempted."""
        if self.connection is None or await self.connection.there_is():
            return False
        import logging

        logging.getLogger(__name__).info("offline: not trying to %s", operation)
        return True

    def _failure(self, operation: str, error: Exception) -> None:
        import logging

        from aide.core.connection import is_network_error

        if is_network_error(error):
            if self.connection is not None:
                self.connection.suspect()
            logging.getLogger(__name__).warning("Telegram could not %s: offline.", operation)
            return
        text = str(error)
        hint = ""
        if "chat not found" in text.lower():
            hint = (
                " Open the bot in Telegram and tap Start (or send it /start); "
                "a bot can only write to someone who has written to it first."
            )
        self.last_error = f"{type(error).__name__}: {text}"
        logging.getLogger(__name__).error("Telegram could not %s: %s.%s", operation, text, hint)

    async def send(self, text, *, buttons=None, replies_to=None) -> Any:
        """Sends a message unless offline, logging failures and returning None instead."""
        if await self._no_network("send a message"):
            return None
        try:
            return await self.channel.send(text, buttons=buttons, replies_to=replies_to)
        except Exception as e:
            self._failure("send a message", e)
            return None

    async def edit(self, message_id, text, *, buttons=None) -> Any:
        """Edits a message unless offline, logging any failure instead of raising."""
        if await self._no_network("edit a message"):
            return
        try:
            await self.channel.edit(message_id, text, buttons=buttons)
        except Exception as e:
            self._failure("edit a message", e)

    async def send_file(self, path, text=None) -> Any:
        """Sends a file unless offline, logging failures and returning None instead."""
        if await self._no_network(f"send the file {Path(path).name}"):
            return None
        try:
            return await self.channel.send_file(path, text)
        except Exception as e:
            self._failure(f"send the file {Path(path).name}", e)
            return None

    async def send_image(self, path, text=None) -> Any:
        """Sends an image unless offline, logging failures and returning None instead."""
        if await self._no_network(f"send the image {Path(path).name}"):
            return None
        try:
            return await self.channel.send_image(path, text)
        except Exception as e:
            self._failure(f"send the image {Path(path).name}", e)
            return None

    async def writing(self) -> None:
        """Shows the typing indicator unless offline, logging any failure."""
        if self.connection is not None and not self.connection.in_line:
            return
        try:
            await self.channel.writing()
        except Exception as e:
            self._failure("show the typing indicator", e)
