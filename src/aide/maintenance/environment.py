"""External programs for maintenance, also when the agent runs under launchd.

The LaunchAgent starts with a minimal PATH and uv may be outside it (here, next to the
system Python). Without this lookup, the dependency audit and the update check failed
under launchd even though they worked from the Terminal.
"""

from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path


def uv_candidates() -> list[Path]:
    """Returns the places where the uv tool may be installed, in search order."""
    home = Path.home()
    return [
        Path(sys.base_prefix) / "bin" / "uv",  # next to the Python the environment was created with
        home / ".local" / "bin" / "uv",
        home / ".cargo" / "bin" / "uv",
        Path("/opt/homebrew/bin/uv"),
        Path("/usr/local/bin/uv"),
        *sorted(
            Path("/Library/Frameworks/Python.framework/Versions").glob("*/bin/uv"), reverse=True
        ),
    ]


def search_uv() -> str:
    """Path of the uv program even if it is not in the PATH. If it does not appear,
    FileNotFoundError."""
    in_path = shutil.which("uv")
    if in_path:
        return in_path
    for path in uv_candidates():
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise FileNotFoundError("can't find the uv program, neither in PATH nor in its usual folders")
