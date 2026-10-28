"""Tailscale: the panel on the iPhone through the user's Tailscale network.

The panel still listens only on 127.0.0.1; `tailscale serve` publishes it inside the Tailscale
network with HTTPS. Funnel (opening it to the internet) is never used: if it is on, or cannot be
known, the panel rejects the Tailscale address (`panel/app.py`).
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# The Tailscale app's CLI, in case `/usr/local/bin/tailscale` is not in the PATH.
CLI_APP = Path("/Applications/Tailscale.app/Contents/MacOS/tailscale")


@dataclass
class ServeState:
    """The Tailscale serve state: funnel status, destinations and any error."""

    # True, on; False, off; None, unknown (no CLI, error or odd response).
    funnel: bool | None
    destinations: list[str] = field(default_factory=list)
    error: str | None = None

    @property
    def safe(self) -> bool:
        return self.funnel is False


def search_tailscale() -> str | None:
    """Returns the path of the tailscale command, or None if not found."""
    found = shutil.which("tailscale")
    if found:
        return found
    return str(CLI_APP) if CLI_APP.exists() else None


def _has_funnel(data: Any) -> bool:
    """Whether any part of the configuration (foreground ones too) allows Funnel."""
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "AllowFunnel" and isinstance(value, dict) and any(value.values()):
                return True
            if _has_funnel(value):
                return True
    elif isinstance(data, list):
        return any(_has_funnel(x) for x in data)
    return False


def _destinations(data: Any) -> list[str]:
    found: list[str] = []
    if isinstance(data, dict):
        for key, value in data.items():
            if key == "Proxy" and isinstance(value, str):
                found.append(value)
            else:
                found.extend(_destinations(value))
    elif isinstance(data, list):
        for x in data:
            found.extend(_destinations(x))
    return found


def analyze_serve(text: str) -> ServeState:
    """The output of `tailscale serve status --json` (`{}` if nothing is published)."""
    try:
        data = json.loads(text)
    except ValueError:
        return ServeState(None, error="unreadable reply from Tailscale")
    if not isinstance(data, dict):
        return ServeState(None, error="unreadable reply from Tailscale")
    return ServeState(_has_funnel(data), sorted(set(_destinations(data))))


def read_serve() -> ServeState:
    """Reads the Tailscale serve status as a ServeState, reporting errors in it."""
    cli = search_tailscale()
    if cli is None:
        return ServeState(None, error="can't find the tailscale command")
    try:
        r = subprocess.run(
            [cli, "serve", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return ServeState(None, error=f"tailscale didn't respond ({type(e).__name__})")
    if r.returncode != 0:
        return ServeState(None, error="tailscale didn't respond (is the app open?)")
    return analyze_serve(r.stdout)
