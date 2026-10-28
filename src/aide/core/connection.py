"""Whether the Mac has internet.

The user spends days without wifi and shares the phone's data now and then. Without a connection,
the agent does not attempt what needs one: scheduled tasks are skipped (maintenance waits for the
next connection), email is not checked, warnings are neither sent nor accumulated, and a task that
loses the connection midway stays paused until the user resumes it, with no retries that spend
tokens.

The check opens and closes a TCP connection to Telegram (or, if it does not respond, to the
Anthropic API): it spends no tokens and barely any data. It repeats at most once a minute; network
failures seen while working mark it sooner. The state change is logged once, without looping errors.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import logging
import socket
import time
from datetime import UTC, datetime

log = logging.getLogger(__name__)

HOSTS = (("api.telegram.org", 443), ("api.anthropic.com", 443))
VALIDITY = 60.0  # seconds a check is valid
WAIT = 5.0  # seconds at most to open each connection
# Before giving a message up as lost, if the last offline check is older than this (seconds), it is
# tried again: an old state throws nothing away.
RECONFIRM = 15.0
# System errors that are network ones (no route, network down, connection refused or cut...).
NETWORK_ERRNOS = {
    errno.ENETUNREACH,
    errno.EHOSTUNREACH,
    errno.ENETDOWN,
    errno.ECONNREFUSED,
    errno.ECONNRESET,
    errno.ECONNABORTED,
    errno.ETIMEDOUT,
}
# Names of the network exceptions of the libraries in use (httpx, Telegram, Google, httplib2), to
# recognize them without importing them here.
NETWORK_ERRORS = {
    "ConnectError",
    "ConnectTimeout",
    "ReadTimeout",
    "WriteTimeout",
    "PoolTimeout",
    "RemoteProtocolError",
    "NetworkError",
    "TimedOut",
    "TransportError",
    "ServerNotFoundError",
    "SSLEOFError",
}
# In Telegram, BadRequest inherits from NetworkError, but it is a request error, not a network one.
NOT_NETWORK = {"BadRequest"}


def is_network_error(e: BaseException) -> bool:
    """Whether an exception (or the one that caused it) is a no-network one."""
    views = 0
    current: BaseException | None = e
    while current is not None and views < 6:
        names = {c.__name__ for c in type(current).__mro__}
        if names & NOT_NETWORK:
            return False
        if isinstance(current, (ConnectionError, TimeoutError, socket.gaierror)):
            return True
        if isinstance(current, OSError) and current.errno in NETWORK_ERRNOS:
            return True
        if names & NETWORK_ERRORS:
            return True
        current = current.__cause__ or current.__context__
        views += 1
    return False


class Connection:
    """Tracks whether the internet connection is available, checking known hosts."""

    def __init__(
        self, hosts: tuple[tuple[str, int], ...] = HOSTS, validity: float = VALIDITY
    ) -> None:
        self.hosts = hosts
        self.validity = validity
        self.in_line = True
        self.no_connection_since: datetime | None = None
        self._checked: float | None = None
        self._lock = asyncio.Lock()

    async def check(self, force: bool = False) -> bool:
        """Whether there is internet. Uses the last check if it is less than a minute old."""
        async with self._lock:
            recent = self._checked is not None and time.monotonic() - self._checked < self.validity
            if recent and not force:
                return self.in_line
            self._put(await self._test())
            return self.in_line

    async def _test(self) -> bool:
        for host, port in self.hosts:
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port), timeout=WAIT
                )
            except (OSError, TimeoutError):
                continue
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return True
        return False

    async def there_is(self) -> bool:
        """To send something. If the last check said there is no connection and it is more than a
        few seconds old, it is tried again before giving it up as lost."""
        if self.in_line:
            return True
        old = self._checked is None or time.monotonic() - self._checked > RECONFIRM
        return await self.check(force=old)

    def suspect(self) -> None:
        """A network failure while working. A single failure is not enough to consider it lost (it
        sometimes happens with wifi): the next check really tests it."""
        self._checked = None

    def mark_offline(self) -> None:
        """There is no connection, checked (for example, Telegram does not respond at startup)."""
        self._put(False)

    def mark_online(self) -> None:
        """Something has arrived from Telegram: there is a connection."""
        self._put(True)

    def _put(self, in_line: bool) -> None:
        self._checked = time.monotonic()
        if in_line == self.in_line:
            return
        if in_line:
            since = self.no_connection_since
            log.info(
                "internet connection is back%s",
                f" (offline since {since.astimezone():%H:%M on %b %-d})" if since else "",
            )
            self.no_connection_since = None
        else:
            log.info("no internet connection: not trying anything that needs it")
            self.no_connection_since = datetime.now(UTC)
        self.in_line = in_line
