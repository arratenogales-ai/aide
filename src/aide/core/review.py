"""Independent final review, driven by the core.

The reviewer works in a separate session: it only receives the request, the plan and the list of
deliverables, never how the work was done, and returns a JSON verdict. The correction
rounds are counted by the engine. For each Word, Excel, PowerPoint or PDF it also receives
images of the first pages, which it must look at to check the format displays well.

Model: Opus for large tasks and those that sent something to third parties on the user's behalf
(emails, invitations, forms in their Chrome); Sonnet for the rest.

Small tasks: full review with Opus if they send or prepare
something for third parties; light (Haiku, few turns, on the delivery) if the delivery cites a file
that doesn't exist or is empty; none otherwise.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from pydantic import BaseModel, ValidationError

from aide.common.config import ModelRole
from aide.core.context import Core, SessionContext
from aide.core.delivery import result_files
from aide.core.documents import FORMATS, verify
from aide.core.orchestrator import AgentSession
from aide.core.session import review_options
from aide.core.subagents import DEFINITIONS
from aide.core.tasks import Task

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "approved": {"type": "boolean"},
        "failures": {"type": "array", "items": {"type": "string"}},
        "checks": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["approved", "failures", "checks"],
    "additionalProperties": False,
}


class Verdict(BaseModel):
    """A review verdict: whether approved, with its failures and checks."""

    approved: bool
    failures: list[str] = []
    checks: list[str] = []


async def deliverables(core: Core, task: Task) -> list[Path]:
    """Files in results/ and project files written during the task."""
    paths = result_files(core.paths.task(task.id))
    projects = core.paths.projects.resolve()
    for event in await core.tasks.events(task.id, ["tool"]):
        data = event.get("data") or {}
        if data.get("tool") not in ("Write", "Edit"):
            continue
        path = Path(event["summary"].split(": ", 1)[-1].strip())
        try:
            if path.exists() and path.resolve().is_relative_to(projects) and path not in paths:
                paths.append(path)
        except (OSError, ValueError):
            continue
    return paths


# Approvals of actions that send something to third parties on the user's behalf.
KINDS_TO_THIRD_PARTIES = ("email", "email_batch", "event", "chrome_send")


async def sends_to_third_parties(core: Core, task: Task) -> bool:
    """Tells whether the task has approved actions that send something to third parties."""
    n = await core.db.value(
        "SELECT COUNT(*) FROM approvals WHERE task_id=? AND state='approved' AND kind IN "
        f"({', '.join('?' for _ in KINDS_TO_THIRD_PARTIES)})",
        (task.id, *KINDS_TO_THIRD_PARTIES),
    )
    return bool(n)


async def prepares_for_third_parties(core: Core, task: Task) -> bool:
    """Email drafts created in the task: they go to third parties even if not yet sent."""
    n = await core.db.value(
        "SELECT COUNT(*) FROM external_actions WHERE task_id=? AND kind='draft' AND state='done'",
        (task.id,),
    )
    return bool(n)


# Paths cited in a delivery: in backticks or absolute, with an extension.
RE_QUOTED_PATH = re.compile(r"`([^`\n]*/[^`\n]*\.[A-Za-z0-9]{1,5})`")
RE_ABSOLUTE_PATH = re.compile(r"(/Users/[^\n`'\"<>]+?\.[A-Za-z0-9]{1,5})(?=[\s`'\".,;:)]|$)")


def missing_files(core: Core, task: Task, text: str) -> list[str]:
    """Files the delivery cites that don't exist or are empty (inside agent/)."""
    agent = core.paths.agent.resolve()
    bases = [core.paths.task(task.id), agent]
    missing = []
    appointments = RE_QUOTED_PATH.findall(text) + RE_ABSOLUTE_PATH.findall(text)
    for appointment in dict.fromkeys(c.strip() for c in appointments):
        if "://" in appointment:
            continue
        path = Path(appointment).expanduser()
        if path.is_absolute():
            if not path.resolve().is_relative_to(agent):
                continue
            candidates = [path]
        else:
            candidates = [b / appointment for b in bases]
        if not any(c.is_file() and c.stat().st_size > 0 for c in candidates):
            missing.append(appointment)
    return missing


async def review_level(core: Core, task: Task, final_text: str) -> tuple[str | None, bool]:
    """(“full”, “light” or None, and whether the full one is with Opus).

    Medium and large tasks are always reviewed: with Opus if they are large or if they sent something
    to third parties."""
    sends = await sends_to_third_parties(core, task)
    if task.size in ("medium", "large"):
        return "full", task.size == "large" or sends
    if sends or await prepares_for_third_parties(core, task):
        return "full", True
    if missing_files(core, task, final_text):
        return "light", False
    return None, False


def prompt_light_review(task: Task, final_text: str, missing: list[str]) -> str:
    """Builds the prompt for a quick review of a small task and its missing files."""
    notice = (
        "\nThe delivery mentions these files and they do not exist or are empty:\n"
        + "\n".join(f"- {f}" for f in missing)
        + "\n"
        if missing
        else ""
    )
    return f"""Light review of a small task. Do not explore: what is here is enough, and at most
open one or two of the files mentioned.

The user's request:
<request>
{task.request}
</request>

Delivery the user will receive:
<delivery>
{final_text[:4000]}
</delivery>

{notice}
Check only that the delivery matches what the user asked for, with no invented data, and that
the files the delivery says it saved exist. Approve if so; in the failures, list each specific
shortfall, in the language of the request."""


async def previews(core: Core, task: Task, files: list[Path]) -> list[Path]:
    """Images of the first pages of each deliverable document (generated anew)."""
    destination = core.paths.task(task.id) / "data" / "views"
    images: list[Path] = []
    for file in files:
        if file.suffix.lower().lstrip(".") in FORMATS:
            v = await asyncio.to_thread(verify, file, None, destination)
            images += v.views
    return images


def prompt_review(task: Task, files: list[Path], views: list[Path] | None = None) -> str:
    """Builds the prompt that asks the reviewer to check a task's deliverable files."""
    plan = f"\nApproved plan:\n<plan>\n{task.plan}\n</plan>\n" if task.plan else ""
    items = "\n".join(f"- {a}" for a in files) or "- (there are no deliverable files)"
    images = (
        "\nImages of the first pages of the documents (open all of them with Read and check "
        "that the formatting looks right: headings, tables, no text cut off, nothing empty). Do not approve "
        "without having looked at them:\n" + "\n".join(f"- {v}" for v in views) + "\n"
        if views
        else ""
    )
    return f"""Review the deliverables of this task.

The user's request:
<request>
{task.request}
</request>
{plan}
Deliverables:
{items}
{images}
Check each deliverable against the request and the plan: open it, run whatever is needed and look at
the screenshots if there are any. Approve only if it meets the request. In the failures, list each specific
shortfall with where it is and how to check it, in the language of the request; optional improvements are not failures."""


def review_role(
    core: Core,
    task: Task,
    opus_fallback: bool,
    to_third_parties: bool = False,
) -> ModelRole:
    """Picks the reviewer model, stricter for large tasks or ones sent to third parties."""
    strict = task.size == "large" or to_third_parties
    name = "reviewer" if strict else "quick_reviewer"
    role = core.config.role(name)
    if opus_fallback and role.alias == "opus":
        r = core.config.models.opus_limit_fallback
        role = ModelRole(alias=r.alias, id=r.id, effort=role.effort)
    return role


async def review(core: Core, task: Task, final_text: str) -> list[str]:
    """Returns the list of failures (empty if it passes or if the review couldn't be completed)."""
    from datetime import UTC, datetime

    from aide.core.db import to_date

    now = datetime.now(UTC)
    fallback = False
    for key in ("limit_opus_until", "temporary_sonnet_until"):
        until = to_date(await core.db.kv_read(key))
        fallback = fallback or bool(until and until > now)
    folder = core.paths.task(task.id)
    level, strict = await review_level(core, task, final_text)
    if level is None:
        return []
    if level == "light":
        role = core.config.role("light_reviewer")
        cap = core.config.settings.caps.light_review
        files, views = [], []
        prompt = prompt_light_review(task, final_text, missing_files(core, task, final_text))
    else:
        # Demanding: large, or a send or draft for third parties: Opus.
        role = review_role(core, task, fallback, strict)
        cap = core.config.settings.caps.review
        files = await deliverables(core, task)
        views = await previews(core, task, files)
        prompt = prompt_review(task, files, views)
    ses = SessionContext(core=core, task_id=task.id, folder=folder, role="review", phase="review")
    options = review_options(ses, role, schema=SCHEMA, max_turns=cap.turns, max_budget_usd=cap.usd)
    # The reviewer uses the same role prompt as the reviewer subagent.
    options.system_prompt = f"{options.system_prompt}\n\n{DEFINITIONS['reviewer']['prompt']}"
    await core.tasks.event(
        task.id,
        "start_review",
        f"{role.id}: {len(files)} deliverables" + (" (light)" if level == "light" else ""),
        {
            "model": role.id,
            "level": level,
            "deliverables": [str(a) for a in files],
            "views": [str(v) for v in views],
        },
    )
    try:
        async with AgentSession(ses, options, "review") as session:
            result = await session.turn(prompt)
    except Exception as e:
        log.exception("the review of task %s failed", task.id)
        await core.tasks.event(task.id, "review_no_completed", str(e)[:300])
        return []
    if result.kind != "ok" or not isinstance(result.structured, dict):
        await core.tasks.event(
            task.id, "review_no_completed", f"{result.kind}: {result.detail}"[:300]
        )
        return []
    try:
        verdict = Verdict.model_validate(result.structured)
    except ValidationError as e:
        await core.tasks.event(task.id, "review_no_completed", f"invalid verdict: {e}"[:300])
        return []
    failures = (
        []
        if verdict.approved
        else [f for f in verdict.failures if f.strip()]
        or ["The reviewer did not approve the delivery."]
    )
    await core.tasks.event(
        task.id,
        "review",
        # “approved” is compared by maintenance/improvements.py.
        "approved" if not failures else f"{len(failures)} failures",
        {
            "model": role.id,
            "approved": verdict.approved,
            "failures": failures,
            "checks": verdict.checks,
        },
    )
    return failures
