"""f) Updates: new versions of the main dependencies, tested in a separate environment.

It never updates the real environment: it copies the project (without data or secrets)
to a temporary folder, updates the packages there, runs the tests and reports whether it
is safe to update.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aide.maintenance.environment import search_uv
from aide.maintenance.section import Section

if TYPE_CHECKING:
    from aide.core.engine import Engine

# What the copy needs to pass the tests (no data, logs or secrets).
COPY = ("pyproject.toml", "uv.lock", ".python-version", "src", "tests")
COPY_CONFIG = ("models.yaml", "models.test.yaml", "settings.yaml", "permissions.yaml")
COPY_AGENT = ("CLAUDE.md", "profile.md", "memory.md", ".claude/skills")
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc", "report.md")


def _normal(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def main(root: Path) -> set[str]:
    """Returns the normalized names of the project's direct dependencies."""
    data = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    return {
        _normal(re.split(r"[<>=!~\[; ]", d, maxsplit=1)[0]) for d in data["project"]["dependencies"]
    }


def outdated(root: Path) -> list[dict[str, Any]]:
    """Returns the outdated direct dependencies, using uv."""
    r = subprocess.run(
        [search_uv(), "pip", "list", "--outdated", "--format", "json"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip()[-200:] or "uv pip list failed")
    names = main(root)
    return [p for p in json.loads(r.stdout or "[]") if _normal(p["name"]) in names]


def test(root: Path, packages: list[str]) -> tuple[bool, str]:
    """Updates the packages in a separate copy and runs the tests. Returns (ok, summary)."""
    with tempfile.TemporaryDirectory(prefix="aide-update-") as tmp:
        backup = Path(tmp) / "project"
        backup.mkdir()
        for name in COPY:
            origin = root / name
            if origin.is_dir():
                shutil.copytree(origin, backup / name, ignore=IGNORE)
            elif origin.is_file():
                shutil.copy2(origin, backup / name)
        (backup / "config").mkdir()
        for name in COPY_CONFIG:
            if (root / "config" / name).is_file():
                shutil.copy2(root / "config" / name, backup / "config" / name)
        for name in COPY_AGENT:
            origin = root / "agent" / name
            destination = backup / "agent" / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            if origin.is_dir():
                shutil.copytree(origin, destination, ignore=IGNORE)
            elif origin.is_file():
                shutil.copy2(origin, destination)
        environment = {
            k: v
            for k, v in os.environ.items()
            if not k.startswith(("ANTHROPIC_", "TELEGRAM_", "AIDE_"))
        }
        environment["UV_PROJECT_ENVIRONMENT"] = str(Path(tmp) / ".venv")
        uv = search_uv()
        environment["PATH"] = os.pathsep.join([str(Path(uv).parent), environment.get("PATH", "")])
        steps = [
            [uv, "lock", *[a for p in packages for a in ("--upgrade-package", p)]],
            [uv, "sync"],
            # Never the real tests (they spend tokens and one sends emails), even if
            # addopts changes; without an extra -q, so pytest writes its summary (“331
            # passed in…”).
            [uv, "run", "--no-sync", "pytest", "-m", "not real", "-p", "no:cacheprovider"],
        ]
        output = ""
        for step in steps:
            r = subprocess.run(
                step,
                cwd=backup,
                env=environment,
                capture_output=True,
                text=True,
                timeout=1800,
                check=False,
            )
            output = (r.stdout + r.stderr).strip()
            if r.returncode != 0:
                last = next((x for x in reversed(output.splitlines()) if x.strip()), "")
                return False, f"{' '.join(step[:2])} failed: {last[:200]}"
        summary = next(
            (x for x in reversed(output.splitlines()) if re.search(r"\d+ passed", x)), ""
        )
        return True, summary.strip("= ")


async def review(engine: Engine) -> Section:
    """Checks for new dependency versions and returns a report section."""
    s = Section("updates", "Updates")
    root = engine.config.paths.root
    if engine.config.settings.no_wifi.active:
        # Checking and testing new versions downloads packages: not on mobile data.
        s.lines.append(
            "I didn't check for updates this week: without wifi, it would use your phone's data."
        )
        return s
    try:
        new = await asyncio.to_thread(outdated, root)
    except Exception as e:
        s.failure(
            f"I couldn't check for new versions ({type(e).__name__}: {str(e)[:150]}).",
            "It's not urgent: it only means I don't know this week whether there are new "
            "versions. Ask Claude Code to look into it with this message.",
        )
        return s
    if not new:
        s.lines.append("claude-agent-sdk and the other main dependencies are up to date.")
        return s
    items = ", ".join(f"{p['name']} {p['version']} → {p['latest_version']}" for p in new)
    s.data["new"] = new
    if any(_normal(p["name"]) == "claude-agent-sdk" for p in new):
        s.lines.append("There is a new version of claude-agent-sdk.")
    s.lines.append(f"New versions: {items}.")
    if not engine.config.settings.maintenance.test_updates:
        s.lines.append("I didn't test them (test_updates is off in settings.yaml).")
        return s
    ok, summary = await asyncio.to_thread(test, root, [p["name"] for p in new])
    if ok:
        s.lines.append(
            "I tested them in a separate environment and the tests pass"
            + (f" ({summary})" if summary else "")
            + ": it's safe to update. I haven't changed anything; if you want to update, ask "
            "Claude Code."
        )
    else:
        s.lines.append(
            f"I tested them in a separate environment and it's not safe to update yet ({summary}). "
            "I haven't changed anything."
        )
    return s
