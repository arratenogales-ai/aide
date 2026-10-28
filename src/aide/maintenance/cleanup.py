"""d) Cleanup: old internal data, logs, database and test leftovers.

It never deletes Finished Tasks, the projects, the profile, the memory or the backups:
the code checks this before every deletion. In test mode it only lists what it would
delete. The meal photos older than 30 days are also deleted, without naming them
in the lines.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from aide.maintenance.section import Section

if TYPE_CHECKING:
    from aide.common.paths import Paths
    from aide.core.engine import Engine

CLAUDE_PROJECTS = Path.home() / ".claude" / "projects"
# Transcripts of the real pytest runs (Claude Code saves them under the name of the test's temporary
# folder).
TESTS_PATTERN = "-private-var-folders-*pytest-of-*"


def protected(paths: Paths) -> list[Path]:
    """Returns the paths that cleanup must never delete."""
    return [
        paths.finished,
        paths.projects,
        paths.profile,
        paths.memory,
        paths.backups,
        paths.config,
        paths.src,
        paths.env,
    ]


def can_delete(path: Path, paths: Paths, roots: list[Path]) -> bool:
    """Only inside the allowed roots and never inside (or above) what is protected."""
    try:
        real = path.resolve()
    except OSError:
        return False
    if not any(real.is_relative_to(r.resolve()) and real != r.resolve() for r in roots):
        return False
    for p in protected(paths):
        pr = p.resolve()
        if real == pr or real.is_relative_to(pr) or pr.is_relative_to(real):
            return False
    return True


def _size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    return sum(p.stat().st_size for p in path.rglob("*") if p.is_file())


def _delete(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


async def candidates(
    engine: Engine, moment: datetime | None = None
) -> list[tuple[Path, str, list[Path]]]:
    """(path, reason, allowed roots) of everything that can be deleted now."""
    moment = moment or datetime.now(UTC)
    paths = engine.config.paths
    settings = engine.config.settings.maintenance
    items: list[tuple[Path, str, list[Path]]] = []

    # Internal data of tasks finished long ago whose results are in the final folder.
    limit = (moment - timedelta(days=settings.internal_data_days)).isoformat(timespec="seconds")
    rows = await engine.db.everything(
        "SELECT t.id, t.final_folder FROM tasks t WHERE t.state='completed' AND "
        "t.final_folder IS NOT NULL AND (SELECT MAX(hour) FROM transitions WHERE task_id=t.id) < ?",
        (limit,),
    )
    for f in rows:
        if not Path(f["final_folder"]).is_dir():
            continue  # without the final copy nothing is touched
        internal = paths.task(f["id"])
        for name in ("data", "input", "progress.md"):
            path = internal / name
            if path.exists():
                items.append(
                    (path, f"task {f['id']} finished, with its final folder", [paths.tasks])
                )

    # Rotated logs older than N days.
    old = time.time() - settings.log_days * 86400
    for path in sorted(paths.logs.glob("aide.log.*")):
        if path.stat().st_mtime < old:
            items.append((path, "old log", [paths.logs]))

    # What was downloaded from Telegram: on receipt it is copied to the task or to your
    # documents, so after the same number of days as the logs it is no longer needed.
    downloads = paths.agent_cache / "telegram"
    if downloads.is_dir():
        for path in sorted(downloads.iterdir()):
            if path.stat().st_mtime < old:
                items.append((path, "file received on Telegram", [downloads]))

    # Leftovers from tests and the simulator.
    leftovers = time.time() - settings.test_leftover_days * 86400
    tests = paths.agent / "test_tasks"
    if tests.is_dir():
        for path in sorted(tests.iterdir()):
            if path.stat().st_mtime < leftovers:
                items.append((path, "test or simulator data", [tests]))
    previous = paths.tasks / "_previous"
    if previous.is_dir():
        for path in sorted(previous.iterdir()):
            if path.stat().st_mtime < old:
                items.append((path, "folder set aside from another database", [previous]))
    if CLAUDE_PROJECTS.is_dir():
        for path in sorted(CLAUDE_PROJECTS.glob(TESTS_PATTERN)):
            if path.stat().st_mtime < leftovers:
                items.append((path, "transcript of a pytest test", [CLAUDE_PROJECTS]))
    return items


async def clean(engine: Engine, test: bool = False) -> Section:
    """Deletes old files that are safe to remove, or only lists them in test mode."""
    s = Section("cleanup", "Cleanup")
    paths = engine.config.paths
    deleted, released = [], 0
    for path, reason, roots in await candidates(engine):
        if not can_delete(path, paths, roots):
            continue
        released += await asyncio.to_thread(_size, path)
        deleted.append(f"{path.name} ({reason})")
        if not test:
            await asyncio.to_thread(_delete, path)
    verb = "I would delete" if test else "I deleted"
    if deleted:
        s.lines.append(
            f"{verb} {len(deleted)} item{'s' if len(deleted) != 1 else ''} "
            f"({released / 1_048_576:.1f} MB): "
            + "; ".join(deleted[:8])
            + ("…" if len(deleted) > 8 else "")
            + "."
        )
    else:
        s.lines.append("There was nothing old to delete.")
    # The meal photos older than 30 days. They go separately and without names, because
    # the model that writes the report reads these lines: how many were deleted is stated by
    # “Your health”, which the code writes.
    from aide.health import meals

    s.data["health_photos"] = await meals.clean_photos(engine, test)
    # Database: the write-ahead log is emptied and the database is compacted.
    size_before = sum(p.stat().st_size for p in paths.data.glob("agent.db*") if p.is_file())
    if not test:
        await engine.db.run("PRAGMA wal_checkpoint(TRUNCATE)")
        await engine.db.run("VACUUM")
        size_after = sum(p.stat().st_size for p in paths.data.glob("agent.db*") if p.is_file())
        s.lines.append(
            f"Database compacted: from {size_before / 1_048_576:.1f} MB to "
            f"{size_after / 1_048_576:.1f} MB."
        )
    else:
        s.lines.append(f"I would compact the database ({size_before / 1_048_576:.1f} MB).")
    s.lines.append(
        "I never touch Finished Tasks, the projects, the profile, the memory or the backups."
    )
    return s
