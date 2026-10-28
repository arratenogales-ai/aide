"""Final copy of each completed task into “Finished Tasks” (done by the core).

The agent always works in agent/tasks/<id>/ (resumption and the sandbox depend on it).
When a task is completed, the core copies its deliverables (results/) to a folder with the
date and a short name. Raw data, drafts and progress.md stay in the
internal folder (the summary is in the panel). If the task is reopened and completed again, the
same folder is updated.

Export already completed tasks (one time only):
    uv run python -m aide.core.finished 1 4 6
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
import shutil
import unicodedata
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

if TYPE_CHECKING:
    from aide.common.paths import Paths
    from aide.core.tasks import Task, TaskRepository

log = logging.getLogger(__name__)

# Words that add nothing to the folder name (without accents, as they end up after normalizing).
STOP_WORDS = frozenset(
    [
        "a",
        "al",
        "ante",
        "con",
        "contra",
        "de",
        "del",
        "desde",
        "e",
        "el",
        "en",
        "entre",
        "hacia",
        "hasta",
        "la",
        "las",
        "lo",
        "los",
        "mas",
        "muy",
        "o",
        "para",
        "por",
        "segun",
        "sin",
        "so",
        "sobre",
        "tras",
        "u",
        "un",
        "una",
        "unas",
        "uno",
        "unos",
        "y",
        "ya",
        "que",
        "se",
        "su",
        "sus",
        "mi",
        "mis",
        "tu",
        "tus",
        "me",
        "te",
        "le",
        "les",
        "nos",
        "os",
        "este",
        "esta",
        "estos",
        "estas",
        "ese",
        "esa",
        "eso",
        "esos",
        "esas",
        "como",
        "cual",
        "cuales",
        "donde",
        "crea",
        "creame",
        "haz",
        "hazme",
        "busca",
        "buscame",
        "prepara",
        "preparame",
        "investiga",
        "escribe",
        "escribeme",
        "redacta",
        "redactame",
        "dame",
        "dime",
        "quiero",
        "necesito",
        "puedes",
        "podrias",
        "favor",
        # Titles tend to start with an infinitive that doesn't tell one task from another.
        "crear",
        "hacer",
        "buscar",
        "preparar",
        "investigar",
        "escribir",
        "redactar",
        "generar",
        "elaborar",
        "the",
        "of",
        "and",
        "for",
        "to",
        "in",
        "on",
        "an",
        # The same ones, in English.
        "with",
        "from",
        "about",
        "by",
        "at",
        "into",
        "or",
        "my",
        "your",
        "our",
        "this",
        "that",
        "these",
        "those",
        "please",
        "can",
        "could",
        "would",
        "you",
        "i",
        "want",
        "need",
        "give",
        "tell",
        "make",
        "create",
        "find",
        "search",
        "prepare",
        "research",
        "write",
        "draft",
        "generate",
        "build",
        "put",
        "together",
        "it",
        "its",
        "is",
        "are",
        "was",
        "were",
        "be",
    ]
)


def brief_name(text: str, minimum: int = 2, maximum: int = 4) -> str:
    """2 to 4 lowercase words, without accents or symbols (for example “informe precios alquiler
    madrid”)."""
    plain = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    plain = re.sub(r"(?<=[a-z0-9])\.[a-z0-9]{1,5}\b", " ", plain)  # without extensions (tips.md)
    words = list(dict.fromkeys(re.findall(r"[a-z0-9]+", plain)))  # no repeats
    useful = [p for p in words if p not in STOP_WORDS]
    # With fewer than two useful words, all are used, in order.
    chosen = useful[:maximum] if len(useful) >= minimum else words[:maximum]
    while len(chosen) < minimum:
        chosen.append("task")
    return " ".join(chosen)


def free_folder(root: Path, base: str) -> Path:
    """`root/base`, or `root/base 2`, `root/base 3`... if it already exists."""
    destination, n = root / base, 2
    while destination.exists():
        destination = root / f"{base} {n}"
        n += 1
    return destination


def inside_of(path: Path, root: Path) -> bool:
    """Returns whether a path is inside a root folder, resolving both."""
    try:
        return path.resolve().is_relative_to(root.resolve())
    except (OSError, ValueError):
        return False


def deliverables(task_folder: Path) -> list[Path]:
    """Normal files in results/ (no hidden files or symlinks)."""
    results = task_folder / "results"
    if not results.is_dir():
        return []
    return sorted(
        p
        for p in results.rglob("*")
        if p.is_file()
        and not p.is_symlink()
        and not any(part.startswith(".") for part in p.relative_to(results).parts)
    )


def plan_of_backup(internal: Path) -> list[tuple[Path, Path]]:
    """(file in results/, relative path in the final folder) of each deliverable."""
    results = internal / "results"
    return [(file, file.relative_to(results)) for file in deliverables(internal)]


def _copy(destination: Path, pairs: list[tuple[Path, Path]]) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for origin, relative in pairs:
        (destination / relative).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(origin, destination / relative)


async def export_task(
    tasks: TaskRepository,
    paths: Paths,
    task: Task,
    zone: str,
    moment: datetime | None = None,
) -> Path | None:
    """Copies the deliverables of a completed task to its final folder and saves the path.

    Only tasks (not quick replies or messages) and only if they deliver some file:
    without deliverables there is no folder. Returns the folder, or None if not applicable. Errors
    are logged and not propagated: delivery doesn't depend on this.
    """
    if task.kind != "task":
        return None
    try:
        root = paths.finished
        date = (moment or datetime.now(UTC)).astimezone(ZoneInfo(zone))
        previous = Path(task.final_folder) if task.final_folder else None
        pairs = plan_of_backup(paths.task(task.id))
        reopened = previous is not None and inside_of(previous, root)
        if not pairs and not reopened:
            log.info("task %s has no deliverables: no final folder", task.id)
            return None
        if reopened:
            destination = previous  # the task was reopened: its folder is updated
        else:
            root.mkdir(parents=True, exist_ok=True)
            base = f"{date:%Y-%m-%d} {brief_name(task.title or task.request)}"
            destination = free_folder(root, base)
        copied = [str(relative) for _, relative in pairs]
        await asyncio.to_thread(_copy, destination, pairs)
        await tasks.update(task.id, final_folder=str(destination))
        await tasks.event(
            task.id, "final_folder", destination.name, {"path": str(destination), "files": copied}
        )
        log.info("task %s copied to %s", task.id, destination)
        return destination
    except Exception:
        log.exception("could not copy task %s to its final folder", task.id)
        return None


async def _export_completed(numbers: list[int]) -> int:
    from aide.common.config import Config
    from aide.common.paths import default_paths
    from aide.core.db import Database
    from aide.core.tasks import TaskRepository

    paths = default_paths()
    config = Config.load(paths)
    db = Database(paths.db)
    await db.connect()
    tasks = TaskRepository(db, paths)
    code = 0
    try:
        for number in numbers:
            task = await tasks.get(number)
            if task is None or task.state != "completed" or task.kind != "task":
                print(f"Task {number}: not a completed task; not exported.")
                code = 1
                continue
            # The folder date is the date it was completed.
            end = await db.value(
                "SELECT hour FROM transitions WHERE task_id=? AND until='completed' "
                "ORDER BY id DESC LIMIT 1",
                (number,),
            )
            moment = datetime.fromisoformat(end) if end else None
            destination = await export_task(tasks, paths, task, config.time_zone, moment=moment)
            print(f"Task {number}: {destination if destination else 'could not be exported'}")
            code = code or (0 if destination else 1)
    finally:
        await db.close()
    return code


def main(argv: list[str] | None = None) -> None:
    """Command line entry point that exports completed tasks by their numbers."""
    parser = argparse.ArgumentParser(
        prog="finished",
        description="Copies already completed tasks to “Finished Tasks”.",
    )
    parser.add_argument("tasks", nargs="+", type=int, help="task numbers")
    args = parser.parse_args(argv)
    raise SystemExit(asyncio.run(_export_completed(args.tasks)))


if __name__ == "__main__":
    main()
