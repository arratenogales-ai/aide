"""Result of each part of the maintenance, checked by code."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from aide.bot.channel import Button


@dataclass
class Section:
    """A section of the maintenance report, with its status, lines and actions."""

    key: str
    title: str
    ok: bool = True  # False: it goes at the start of the report, as a priority alert
    lines: list[str] = field(default_factory=list)  # verified facts
    that_do: list[str] = field(default_factory=list)  # instructions if something fails
    data: dict[str, Any] = field(default_factory=dict)
    buttons: list[list[Button]] = field(default_factory=list)

    def failure(self, line: str, that_do: str | None = None) -> None:
        """Marks the section as failed and adds a line and an optional instruction."""
        self.ok = False
        self.lines.append(line)
        if that_do:
            self.that_do.append(that_do)

    def for_model(self) -> dict[str, Any]:
        """What the model sees to write the report (without secrets)."""
        return {
            "section": self.title,
            "correct": self.ok,
            "facts": self.lines,
            "that_do": self.that_do,
        }
