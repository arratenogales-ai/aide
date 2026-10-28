"""Project context: `projects/<name>/CONTEXT.md`.

The agent reads CONTEXT.md before touching a project and updates it when done. The code
supports it in two ways: each task's prompt lists the existing projects and, if a
task wrote to a project without updating its CONTEXT.md, the engine asks for it before
delivering (only once).
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

CONTEXT = "CONTEXT.md"
LISTED_MAX = 30


def existing_projects(root: Path) -> list[str]:
    """Project names, from newest to oldest."""
    if not root.is_dir():
        return []
    folders = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    folders.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return [p.name for p in folders[:LISTED_MAX]]


def touched_projects(written: Iterable[str], root: Path) -> dict[str, bool]:
    """Projects written to and whether their CONTEXT.md was updated."""
    root = root.resolve()
    touched: dict[str, bool] = {}
    for path in written:
        try:
            relative = Path(path).resolve().relative_to(root)
        except (OSError, ValueError):
            continue
        if len(relative.parts) < 2:
            continue
        name = relative.parts[0]
        updated = relative.parts[1:] == (CONTEXT,)
        touched[name] = touched.get(name, False) or updated
    return touched


def no_context(written: Iterable[str], root: Path, exclude: Iterable[str] = ()) -> list[str]:
    """Returns the sorted touched projects that were not updated, minus excluded ones."""
    excluded = set(exclude)
    return sorted(
        n
        for n, updated in touched_projects(written, root).items()
        if not updated and n not in excluded
    )
