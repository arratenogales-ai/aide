"""System checks that don't need the model: Chrome, extension and caffeinate."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import logging
import os
import socket
import subprocess
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)


@dataclass
class ChromeState:
    """Whether Chrome is open and its extension is connected."""

    open_chrome: bool
    connected_extension: bool
    detail: str

    @property
    def ready(self) -> bool:
        return self.open_chrome and self.connected_extension


def _open_chrome() -> bool:
    try:
        r = subprocess.run(
            ["pgrep", "-x", "Google Chrome"], capture_output=True, text=True, timeout=5
        )
        return r.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _command_of(pid: int) -> str:
    try:
        r = subprocess.run(
            ["ps", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, timeout=5
        )
        return r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _socket_replies(path: Path) -> bool:
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1.0)
    try:
        s.connect(str(path))
        return True
    except OSError:
        return False
    finally:
        s.close()


def bridge_folder(user: str | None = None) -> Path:
    return Path("/tmp") / f"claude-mcp-browser-bridge-{user or getpass.getuser()}"


def chrome_state(folder: Path | None = None) -> ChromeState:
    """Chrome open and extension connected, checked by code.

    Chrome launches the bridge process (`--chrome-native-host`) when the Claude in
    Chrome extension connects; that process listens on /tmp/claude-mcp-browser-bridge-<user>/<pid>.sock.
    """
    is_open = _open_chrome()
    folder = folder or bridge_folder()
    connected = False
    detail = ""
    if folder.is_dir():
        for sock in sorted(folder.glob("*.sock")):
            try:
                pid = int(sock.stem)
            except ValueError:
                continue
            try:
                os.kill(pid, 0)
            except OSError:
                continue
            if "chrome-native-host" not in _command_of(pid):
                continue
            if _socket_replies(sock):
                connected = True
                detail = f"bridge active (pid {pid})"
                break
    if not is_open:
        detail = "Chrome is not open"
    elif not connected:
        detail = "the Claude in Chrome extension is not connected"
    return ChromeState(open_chrome=is_open, connected_extension=connected, detail=detail)


class KeepAwake:
    """Keeps the Mac awake with `caffeinate -i` while there are tasks running."""

    def __init__(self) -> None:
        self._process: asyncio.subprocess.Process | None = None
        self._uses = 0
        self._lock = asyncio.Lock()

    @property
    def active(self) -> bool:
        return self._process is not None and self._process.returncode is None

    async def acquire(self) -> None:
        """Starts keeping the Mac awake, running caffeinate on the first use."""
        async with self._lock:
            self._uses += 1
            if self._uses == 1 and not self.active:
                try:
                    self._process = await asyncio.create_subprocess_exec(
                        "caffeinate",
                        "-i",
                        stdout=asyncio.subprocess.DEVNULL,
                        stderr=asyncio.subprocess.DEVNULL,
                    )
                    log.info("caffeinate on")
                except OSError as e:
                    log.warning("could not start caffeinate: %s", e)

    async def release(self) -> None:
        """Releases one use of the wake lock and stops it when nobody needs it."""
        async with self._lock:
            self._uses = max(0, self._uses - 1)
            if self._uses == 0:
                await self._stop()

    async def _stop(self) -> None:
        if self._process is not None and self._process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                self._process.terminate()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._process.wait(), timeout=5)
            log.info("caffeinate off")
        self._process = None

    async def close(self) -> None:
        """Drops all uses of the wake lock and stops it right away."""
        async with self._lock:
            self._uses = 0
            await self._stop()
