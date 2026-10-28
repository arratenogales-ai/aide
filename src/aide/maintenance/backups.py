"""e) Compressed backup in “Backups” (the latest ones are kept).

It includes the database (with SQLite's hot copy, consistent even while the agent is
running, with your health data), the profile, the memory, your documents, the meal
photos (data/health), config/ without credentials.json or the settings copies,
and the projects without dependencies or caches, also what is left out of git. It never
includes secrets: not .env, not credentials.json, not the projects' keys or tokens. The
file is readable only by the Mac's user (600).
"""

from __future__ import annotations

import asyncio
import fnmatch
import sqlite3
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from aide.common.paths import BACKUPS_FOLDER
from aide.maintenance.section import Section

if TYPE_CHECKING:
    from aide.common.paths import Paths
    from aide.core.engine import Engine

EXCLUDE_PROJECTS = {
    "node_modules",
    ".venv",
    "venv",
    "__pycache__",
    ".git",
    ".cache",
    "dist",
    "build",
}
EXCLUDE_CONFIG = {"credentials.json", "backups"}
# Files with secrets, which never go in the backup (the same names that permissions.yaml forbids the
# agent to read).
SECRETS = (
    ".env",
    ".env.*",
    "*.pem",
    "*.key",
    "*.p12",
    "id_rsa*",
    "id_ed25519*",
    "id_ecdsa*",
    "credentials.json",
    "token.json",
)


def _secret(path: Path) -> bool:
    """A file with secrets, by its name."""
    return any(fnmatch.fnmatch(path.name, pattern) for pattern in SECRETS)


def _copy_db(origin: Path, destination: Path) -> None:
    source = sqlite3.connect(f"file:{origin}?mode=ro", uri=True)
    try:
        backup = sqlite3.connect(destination)
        with backup:
            source.backup(backup)
        backup.close()
    finally:
        source.close()


def create_backup(paths: Paths, moment: datetime, keep: int) -> tuple[Path, int, list[Path]]:
    """Creates the backup and deletes the old ones. Returns (file, size, copies deleted)."""
    folder = paths.backups
    folder.mkdir(exist_ok=True)
    folder.chmod(0o700)
    destination = folder / f"backup-{moment:%Y-%m-%d-%H%M}.zip"
    with tempfile.TemporaryDirectory(prefix="aide-backup-") as tmp:
        db = Path(tmp) / "agent.db"
        _copy_db(paths.db, db)
        with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as z:
            z.write(db, "data/agent.db")
            for path, name in (
                (paths.profile, "agent/profile.md"),
                (paths.memory, "agent/memory.md"),
            ):
                if path.is_file():
                    z.write(path, name)
            # Your documents and the health data (meal photos): they are
            # in data/, which is not copied whole.
            for their, name in ((paths.documents, "documents"), (paths.health, "health")):
                if not their.is_dir():
                    continue
                for path in sorted(their.rglob("*")):
                    if path.is_file() and not path.is_symlink():
                        z.write(path, f"data/{name}/{path.relative_to(their)}")
            for path in sorted(paths.config.rglob("*")):
                relative = path.relative_to(paths.config)
                if (
                    path.is_file()
                    and not (set(relative.parts) & EXCLUDE_CONFIG)
                    and path.name != ".env"
                ):
                    z.write(path, f"config/{relative}")
            if paths.projects.is_dir():
                for path in sorted(paths.projects.rglob("*")):
                    relative = path.relative_to(paths.projects)
                    if (
                        path.is_file()
                        and not path.is_symlink()
                        and not (set(relative.parts) & EXCLUDE_PROJECTS)
                        and not _secret(path)
                    ):
                        z.write(path, f"agent/projects/{relative}")
    destination.chmod(0o600)
    backups = sorted(folder.glob("backup-*.zip"), key=lambda p: p.name.split("-", 1)[1])
    deleted = backups[:-keep] if len(backups) > keep else []
    for old in deleted:
        old.unlink()
    return destination, destination.stat().st_size, deleted


async def make_backup(engine: Engine) -> Section:
    """Makes a database backup, deletes old ones and returns a report section."""
    s = Section("backup", "Backup")
    keep = engine.config.settings.maintenance.backups_to_keep
    try:
        file, size, deleted = await asyncio.to_thread(
            create_backup, engine.config.paths, datetime.now(), keep
        )
    except Exception as e:
        s.failure(
            f"The backup could not be made ({type(e).__name__}: {str(e)[:150]}).",
            "Check that there is free disk space; if it keeps failing, ask Claude Code to look into it.",
        )
        return s
    s.lines.append(
        f"Backup done: {BACKUPS_FOLDER}/{file.name} ({size / 1_048_576:.1f} MB), without .env "
        f"or credentials.json. The last {keep} are kept"
        + (
            f"; I deleted {len(deleted)} old one{'s' if len(deleted) != 1 else ''}."
            if deleted
            else "."
        )
    )
    return s
