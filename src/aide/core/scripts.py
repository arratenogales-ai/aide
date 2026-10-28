"""Scheduled tasks of type “script”: the core runs a fixed command, without a model.

The command is an allowed interpreter and a script of the project itself, inside
agent/projects/<project>/. It runs with a maximum time of 2 minutes and inside the macOS
sandbox (sandbox-exec): it can only write to its project and to temp files and cannot read
secrets (the same ones as the agent's sandbox). If the output isn't the silence word, it is
sent as is to the user through the bot; if it fails or times out, they are notified.

Registering them always needs the user's approval in Telegram with the exact command: the
agent's schedule_script tool (level 2) or, from the terminal,

    uv run python -m aide.core.scripts request --project rain-alert \\
        --command "python3 rain_alert.py" --daily 07:30 --silence NO_RAIN

which leaves the request pending; the running agent asks the user for it and only activates it if they
approve.
"""

from __future__ import annotations

import argparse
import asyncio
import re
import shlex
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from aide.common.paths import Paths

MAX_TIME = 120
INTERPRETERS = ("python3", "python", "bash", "sh")
# No chained shell commands, redirections or substitutions: the command is fixed.
FORBIDDEN = re.compile(r"[;&|<>`$(){}\\\n\r*?!\"']")
SANDBOX_EXEC = "/usr/bin/sandbox-exec"
MAX_MESSAGE = 3500


@dataclass(frozen=True)
class Command:
    """A project script to run, as its project name and its argument list."""

    project: str
    argv: tuple[str, ...]  # interpreter (logical name), project script and arguments

    def text(self) -> str:
        return shlex.join(self.argv)


@dataclass
class ScriptResult:
    """The outcome of running a script: exit code, output, error, duration and timeout."""

    code: int | None
    output: str
    error: str
    seconds: float
    exhausted: bool = False

    @property
    def ok(self) -> bool:
        return not self.exhausted and self.code == 0


def project_folder(paths: Paths, project: str) -> Path:
    """Returns the folder of an existing project, rejecting invalid names or outside paths."""
    if not re.fullmatch(r"[\w][\w.-]*", project or "") or project in (".", ".."):
        raise ValueError(f"invalid project name: {project!r}")
    root = paths.projects.resolve()
    try:
        folder = (paths.projects / project).resolve(strict=True)
    except OSError as e:
        raise ValueError(f"project {project} does not exist") from e
    if not folder.is_dir() or folder == root or not folder.is_relative_to(root):
        raise ValueError(f"project {project} is not inside agent/projects")
    return folder


def validate(paths: Paths, project: str, command: str | list[str] | tuple[str, ...]) -> Command:
    """Checks that the command is an allowed interpreter and a script of the project itself."""
    folder = project_folder(paths, project)
    argv = shlex.split(command) if isinstance(command, str) else [str(a) for a in command]
    if not argv or argv[0] not in INTERPRETERS:
        raise ValueError(f"the command must start with {', '.join(INTERPRETERS)}")
    if len(argv) < 2:
        raise ValueError("the script to run is missing")
    for token in argv:
        if FORBIDDEN.search(token):
            raise ValueError(f"character not allowed in {token!r}")
    script = (folder / argv[1]).resolve()
    if not script.is_file() or not script.is_relative_to(folder):
        raise ValueError(f"the script {argv[1]} is not a file of project {project}")
    for token in argv[2:]:
        if "/" in token or token.startswith(("~", ".")):
            path = (folder / token).expanduser().resolve()
            if not path.is_relative_to(folder):
                raise ValueError(f"the path {token} goes outside project {project}")
    return Command(project, tuple(argv))


def interpreter(name: str) -> str:
    """Returns the executable path for an interpreter name such as python3 or bash."""
    if name in ("python3", "python"):
        return sys.executable  # the Aide environment's Python
    return {"bash": "/bin/bash", "sh": "/bin/sh"}[name]


def _string(path: Path | str) -> str:
    return str(path).replace("\\", "\\\\").replace('"', '\\"')


def sandbox_profile(folder: Path, paths: Paths) -> str:
    """sandbox-exec profile: writes only to the project and temp files; reads no secrets."""
    from aide.core.session import SECRET_PATHS

    writable = {
        folder.resolve(),
        Path("/private/tmp"),
        Path(tempfile.gettempdir()).resolve(),  # this user's temporary folder
    }
    secret_folders = [Path(p).expanduser() for p in SECRET_PATHS] + [
        paths.config,
        paths.data,
        paths.logs,
        paths.src,
        paths.finished,
        # The weekly backups (the database and your documents) and the inbox: not a script
        # either.
        paths.backups,
        paths.inbox,
    ]
    denied_read = (
        " ".join(f'(subpath "{_string(p.resolve())}")' for p in secret_folders)
        + f' (literal "{_string(paths.env.resolve())}")'
    )
    write = " ".join(f'(subpath "{_string(p)}")' for p in sorted(writable))
    return "\n".join(
        [
            "(version 1)",
            "(allow default)",
            "(deny file-write*)",
            f'(allow file-write* {write} (literal "/dev/null") (literal "/dev/dtracehelper")'
            ' (regex #"^/dev/tty") (regex #"^/dev/fd/"))',
            # Neither the project's git repository nor what changes how git works
            # .
            r'(deny file-write* (regex #"/\.git(/|$)") (regex #"/\.gitattributes$")'
            r' (regex #"/\.gitmodules$"))',
            f"(deny file-read* {denied_read})",
        ]
    )


async def run(paths: Paths, command: Command, zone: str) -> ScriptResult:
    """Runs a project script in a restricted environment and returns its result."""
    folder = project_folder(paths, command.project)
    argv = [interpreter(command.argv[0]), *command.argv[1:]]
    environment = {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
        "HOME": str(Path.home()),
        "LANG": "en_US.UTF-8",
        "LC_ALL": "en_US.UTF-8",
        "TZ": zone,
        "TMPDIR": tempfile.gettempdir(),
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    start = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        shutil.which("sandbox-exec") or SANDBOX_EXEC,
        "-p",
        sandbox_profile(folder, paths),
        *argv,
        cwd=str(folder),
        env=environment,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        output, error = await asyncio.wait_for(process.communicate(), timeout=MAX_TIME)
    except TimeoutError:
        process.kill()
        await process.wait()
        return ScriptResult(None, "", "", time.monotonic() - start, exhausted=True)
    return ScriptResult(
        process.returncode,
        output.decode("utf-8", errors="replace"),
        error.decode("utf-8", errors="replace"),
        time.monotonic() - start,
    )


def message_for_user(
    scheduled_task_id: int, command: Command, silence: str, r: ScriptResult
) -> str | None:
    """What is sent to the user after running (None if there is nothing to say)."""
    name = f"Scheduled task {scheduled_task_id} ({command.project})"
    if r.exhausted:
        return f"{name} took more than {MAX_TIME // 60} minutes, so I stopped it."
    if r.code != 0:
        last = next((line for line in reversed(r.error.strip().splitlines()) if line.strip()), "")
        detail = f": {last[:300]}" if last else ""
        return f"{name} failed (exit code {r.code}){detail}."
    text = r.output.strip()
    if not text or (silence and text == silence.strip()):
        return None
    return text[:MAX_MESSAGE]


def describe(command: Command, silence: str) -> str:
    """Describes a script command, its time limit and when it sends its output."""
    lines = [
        f"Project: agent/projects/{command.project}",
        f"Exact command: {command.text()}",
        f"Time limit: {MAX_TIME // 60} minutes, inside the sandbox, no model",
    ]
    if silence:
        lines.append(
            f"If the output is “{silence}”, it does not message you; otherwise it sends you the output"
        )
    else:
        lines.append("It sends you the output whenever it is not empty")
    return "\n".join(lines)


# --- registration request from the terminal -----------------------------------------------------


async def _request(args: argparse.Namespace) -> int:
    from aide.common.config import Config
    from aide.common.paths import default_paths
    from aide.core import scheduler
    from aide.core.db import Database, now

    paths = default_paths()
    config = Config.load(paths)
    command = validate(paths, args.project, args.command)
    from zoneinfo import ZoneInfo

    zone = ZoneInfo(config.time_zone)
    timing = scheduler.interpret({"kind": "daily", "hour": args.daily}, zone)
    db = Database(paths.db)
    await db.connect()
    try:
        scheduled_task_id = await db.run(
            "INSERT INTO scheduled_tasks(description, timing, policy, active, created, timing_text, "
            "kind, project, command, silence, pending) VALUES (?, ?, ?, 0, ?, ?, 'script', ?, ?, ?, 1)",
            (
                args.description or f"Script for {command.project}",
                timing.to_json(),
                args.policy,
                now(),
                scheduler.describe(timing),
                command.project,
                json_dumps(list(command.argv)),
                args.silence or "",
            ),
        )
    finally:
        await db.close()
    print(
        f"Request {scheduled_task_id} is pending: the running agent will ask for your approval in Telegram."
    )
    return 0


def json_dumps(value: object) -> str:
    """Serializes a value to JSON, keeping non-ASCII characters as they are."""
    import json

    return json.dumps(value, ensure_ascii=False)


def main(argv: list[str] | None = None) -> None:
    """Command line entry point to request a new scheduled script task."""
    parser = argparse.ArgumentParser(
        prog="scripts", description="Scheduled tasks without the model"
    )
    sub = parser.add_subparsers(dest="action", required=True)
    p = sub.add_parser("request", help="request a new script task (approved in Telegram)")
    p.add_argument("--project", required=True)
    p.add_argument("--command", required=True, help='for example "python3 rain_alert.py"')
    p.add_argument("--daily", required=True, help="time HH:MM")
    p.add_argument("--silence", default="")
    p.add_argument("--description", default="")
    p.add_argument("--policy", default="run_if_recent")
    args = parser.parse_args(argv)
    raise SystemExit(asyncio.run(_request(args)))


if __name__ == "__main__":
    main()
