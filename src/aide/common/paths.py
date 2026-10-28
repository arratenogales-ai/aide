"""Canonical project paths.

The root is deduced from the package location. It can be changed with AIDE_ROOT (useful in tests)
and the data folder with AIDE_DATA (useful for the simulator). With a separate data folder, task
folders also go separately (agent/test_tasks/<name>/) and memory lives with that data, so that a
test database never uses or modifies the real tasks or memory.

The user's folders (Finished Tasks, Document Inbox, Backups and the Projects link) go in
`folders`: the folder that contains the project (MI AGENTE), so that they do
not have to go into the program's one. `default_paths` chooses it by itself (or with
AIDE_FOLDERS); if not given, it is the root, as in tests.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

_PACKAGE_ROOT = Path(__file__).resolve().parents[3]
FINISHED_FOLDER = "Finished Tasks"
# Where the user leaves documents for the agent to read. In NFC, as it is written.
INBOX_FOLDER = "Document Inbox"
PROJECTS_LINK = "Projects"
BACKUPS_FOLDER = "Backups"


@dataclass(frozen=True)
class Paths:
    """The root and data folders of the project, with derived paths."""

    root: Path
    data_dir: Path | None = None
    folders_dir: Path | None = None

    @property
    def folders(self) -> Path:
        """Where the user's folders are."""
        return self.folders_dir or self.root

    @property
    def backups(self) -> Path:
        """The weekly backups (they carry the database and its documents: nobody else reads them)."""
        return self.folders / BACKUPS_FOLDER

    @property
    def agent(self) -> Path:
        return self.root / "agent"

    @property
    def config(self) -> Path:
        return self.root / "config"

    @property
    def data(self) -> Path:
        return self.data_dir or (self.root / "data")

    @property
    def logs(self) -> Path:
        """Inside data/ since the cleanup of 2026-10-27 (before, in the root)."""
        return self.data / "logs"

    @property
    def improvements(self) -> Path:
        """The weekly report's improvements and the copies of what they change."""
        return self.data / "improvements"

    @property
    def src(self) -> Path:
        return self.root / "src"

    @property
    def deploy(self) -> Path:
        return self.root / "deploy"

    @property
    def env(self) -> Path:
        return self.root / ".env"

    @property
    def db(self) -> Path:
        return self.data / "agent.db"

    @property
    def tasks(self) -> Path:
        if self.data_dir is not None:
            return self.agent / "test_tasks" / self.data_dir.name
        return self.agent / "tasks"

    @property
    def projects(self) -> Path:
        return self.agent / "projects"

    @property
    def skills(self) -> Path:
        return self.agent / ".claude" / "skills"

    @property
    def profile(self) -> Path:
        return self.agent / "profile.md"

    @property
    def memory(self) -> Path:
        # The agent only accesses memory through its tools, so in tests with separate data
        # it can live with that data and not touch the real memory.
        if self.data_dir is not None:
            return self.data_dir / "memory.md"
        return self.agent / "memory.md"

    @property
    def finished(self) -> Path:
        # Copy of each completed task's deliverables, made by the core (not the agent). With
        # separate data it goes with that data, so as not to mix tests with the real
        # results.
        if self.data_dir is not None:
            return self.data_dir / FINISHED_FOLDER
        return self.folders / FINISHED_FOLDER

    @property
    def inbox(self) -> Path:
        """The document inbox, next to Finished Tasks: what the user drags from the Finder
        for the agent to read with /documents. With separate data, in that data."""
        if self.data_dir is not None:
            return self.data_dir / INBOX_FOLDER
        return self.folders / INBOX_FOLDER

    @property
    def documents(self) -> Path:
        """Your documents: in data/, outside git and outside what the agent can read."""
        return self.data / "documents"

    @property
    def health(self) -> Path:
        """Your health data: outside git and outside what the agent can read, like your
        documents. The tables are in the database; here, the meal photos."""
        return self.data / "health"

    @property
    def photos_health(self) -> Path:
        """The meal photos: 30 days, and the weekly cleanup deletes them."""
        return self.health / "photos"

    @property
    def agent_cache(self) -> Path:
        return self.agent / ".cache"

    def task(self, task_id: int) -> Path:
        return self.tasks / str(task_id)

    def create_task_folder(self, task_id: int) -> Path:
        """Creates a task folder with input, data and results, and a progress file."""
        base = self.task(task_id)
        for sub in ("input", "data", "results"):
            (base / sub).mkdir(parents=True, exist_ok=True)
        progress = base / "progress.md"
        if not progress.exists():
            progress.write_text(f"# Task {task_id} progress\n\n", encoding="utf-8")
        return base

    def reserve_old_folder(self, task_id: int) -> Path | None:
        """A new task does not inherit another's folder with the same number.

        This happens if the database was created anew: the previous folder is moved (not deleted)
        to tasks/_previous/.
        """
        base = self.task(task_id)
        if not base.exists():
            return None
        destination = self.tasks / "_previous" / f"{task_id}-{datetime.now():%Y%m%d-%H%M%S}"
        destination.parent.mkdir(parents=True, exist_ok=True)
        base.rename(destination)
        return destination

    @property
    def projects_link(self) -> Path:
        """Symbolic link to agent/projects next to the user's other folders, to see the projects from
        Finder."""
        return self.folders / PROJECTS_LINK

    def ensure(self) -> None:
        """Creates the data, log, task, project, cache and inbox folders if missing."""
        for folder in (
            self.data,
            self.logs,
            self.tasks,
            self.projects,
            self.agent_cache,
            self.inbox,
        ):
            folder.mkdir(parents=True, exist_ok=True)
        link = self.projects_link
        if not link.exists() and not link.is_symlink():
            # Relative, so that it stays valid if the folder containing everything is
            # moved.
            destination = os.path.relpath(self.projects, self.folders)
            link.symlink_to(destination, target_is_directory=True)

    def available_skills(self) -> list[str]:
        """Returns the sorted names of the skills that have a SKILL.md file."""
        if not self.skills.is_dir():
            return []
        return sorted(p.parent.name for p in self.skills.glob("*/SKILL.md"))


def default_paths() -> Paths:
    """Builds Paths from AIDE_ROOT and AIDE_DATA, or the package root by default."""
    root = Path(os.environ.get("AIDE_ROOT", _PACKAGE_ROOT)).resolve()
    data = os.environ.get("AIDE_DATA")
    return Paths(
        root=root,
        data_dir=Path(data).resolve() if data else None,
        folders_dir=user_folders(root),
    )


def user_folders(root: Path) -> Path:
    """The folder of the user's folders: AIDE_FOLDERS, or the one that contains the
    project if they are already there (“Finished Tasks” is checked), or the root if not."""
    chosen = os.environ.get("AIDE_FOLDERS")
    if chosen:
        return Path(chosen).resolve()
    if (root.parent / FINISHED_FOLDER).is_dir():
        return root.parent
    return root
