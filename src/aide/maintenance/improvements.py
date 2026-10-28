"""c) Improvement proposals based on what happened during the week.

The code gathers the signals (failed or cancelled tasks, user corrections, rejected
approvals, caps, long tasks, repeated questions) and the model proposes at most 5
improvements. The code validates them:

- Type A, without code: agent/CLAUDE.md, a skill, config/models.yaml or the memory. They
  arrive with the Apply and Discard buttons; on applying, a copy is saved first and it can
  be undone with /undo_improvement N. The code checks the file, the YAML and the limit of
  150 lines.
- Type B, with code or security (src/, permissions.yaml, hooks, scopes, settings.yaml):
  never applied from the agent; they are written to data/improvements/YYYY-MM-DD.md to
  hand them to Claude Code.

A type A proposal that fails validation becomes type B.
"""

from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from aide.core.db import from_json, now, to_json

if TYPE_CHECKING:
    from aide.core.engine import Engine

MAX_PROPOSALS = 5
MAX_TEXT = 600  # it is shown in full in Telegram before being applied
LINES_LIMIT = 150
TAKES_MUCH = timedelta(minutes=30)
# How each state of an improvement is said (the database values remain in Spanish).
STATES = {
    "proposal": "proposed",
    "applied": "applied",
    "discarded": "discarded",
    "undone": "undone",
}


@dataclass
class Change:
    """A proposed change to a file: the operation, the text and what to replace."""

    file: str  # agent/CLAUDE.md, agent/.claude/skills/<name>/SKILL.md, config/models.yaml or memory
    operation: str  # add, replace or create
    text: str
    search: str = ""


def allowed_path(engine: Engine, file: str) -> Path | None:
    """Path of a file that can be changed with a type A improvement (None if it cannot)."""
    paths = engine.config.paths
    file = file.strip().lstrip("./")
    if file in ("memory", "agent/memory.md"):
        return paths.memory
    if file == "agent/CLAUDE.md":
        return paths.agent / "CLAUDE.md"
    if file == "config/models.yaml":
        return paths.config / "models.yaml"
    m = re.fullmatch(r"agent/\.claude/skills/([a-z0-9][a-z0-9-]{0,40})/SKILL\.md", file)
    if m:
        return paths.skills / m.group(1) / "SKILL.md"
    return None


def apply_in_text(current: str, change: Change) -> str:
    """Applies a change to a file's text and returns the new text, or raises ValueError."""
    if change.operation == "create":
        if current.strip():
            raise ValueError("the file already exists")
        return change.text.rstrip() + "\n"
    if change.operation == "add":
        return current.rstrip("\n") + "\n" + change.text.rstrip() + "\n"
    if change.operation == "replace":
        if not change.search or current.count(change.search) != 1:
            raise ValueError("the text to replace does not appear exactly once")
        return current.replace(change.search, change.text, 1)
    raise ValueError(f"invalid operation: {change.operation}")


def validate_change(engine: Engine, change: Change) -> tuple[Path, str]:
    """Checks the type A improvement and returns (path, new content)."""
    path = allowed_path(engine, change.file)
    if path is None:
        raise ValueError(f"{change.file} can't be changed without code")
    if not change.text.strip() or len(change.text) > MAX_TEXT:
        raise ValueError("the text of the change is empty or too long")
    if path == engine.config.paths.memory:
        if change.operation != "add" or "\n" in change.text.strip():
            raise ValueError("only a one-line fact can be added to the memory")
        return path, change.text.strip()
    current = path.read_text(encoding="utf-8") if path.is_file() else ""
    new = apply_in_text(current, change)
    if path.name == "models.yaml":
        from aide.common.config import Models

        try:
            Models.model_validate(yaml.safe_load(new) or {})
        except yaml.YAMLError as e:
            raise ValueError(f"models.yaml would not be valid YAML: {e}") from e
    if path.name == "CLAUDE.md":
        profile = engine.config.paths.profile
        lines = len(new.splitlines()) + (
            len(profile.read_text(encoding="utf-8").splitlines()) if profile.is_file() else 0
        )
        if lines > LINES_LIMIT:
            raise ValueError(
                f"CLAUDE.md and profile.md would add up to {lines} lines (maximum {LINES_LIMIT})"
            )
    return path, new


# --- signals of the week -----------------------------------------------------------------------


def _words(text: str) -> set[str]:
    return {p for p in re.findall(r"\w{4,}", (text or "").lower())}


async def signals(engine: Engine, moment: datetime | None = None) -> dict[str, Any]:
    """What happened during the week and may point to something to improve (data only)."""
    moment = moment or datetime.now(UTC)
    since = (moment - timedelta(days=7)).isoformat(timespec="seconds")
    db = engine.db
    tasks = await db.everything(
        "SELECT id, kind, state, title, request, error, created, size FROM tasks "
        "WHERE created >= ? ORDER BY id",
        (since,),
    )

    def short(t: dict[str, Any]) -> dict[str, Any]:
        return {
            "id": t["id"],
            "title": (t["title"] or t["request"] or "")[:80],
            "state": t["state"],
            "error": (t["error"] or "")[:160],
        }

    failed = [short(t) for t in tasks if t["state"] in ("failed", "cancelled")]
    corrections = await db.everything(
        "SELECT parent_task AS task, request FROM tasks WHERE kind='message' AND created >= ? "
        "AND parent_task IS NOT NULL",
        (since,),
    )
    plan_changes = await db.value(
        "SELECT COUNT(*) FROM transitions WHERE hour >= ? AND (reason LIKE '%changes en el plan%' "
        "OR reason LIKE '%changes to the plan%')",
        (since,),
    )
    rejected = await db.everything(
        "SELECT task_id, kind, substr(detail, 1, 120) AS detail FROM approvals "
        "WHERE state='rejected' AND resolved >= ?",
        (since,),
    )
    caps = await db.everything(
        "SELECT DISTINCT task_id FROM transitions WHERE until='paused_cap' AND hour >= ?",
        (since,),
    )
    review = await db.everything(
        "SELECT task_id, summary FROM events WHERE kind='review' AND hour >= ? "
        "AND summary != 'approved'",
        (since,),
    )
    long = []
    for t in tasks:
        if t["kind"] != "task" or t["state"] != "completed":
            continue
        hours = await db.everything(
            "SELECT hour FROM transitions WHERE task_id=? ORDER BY id", (t["id"],)
        )
        if len(hours) >= 2:
            duration = datetime.fromisoformat(hours[-1]["hour"]) - datetime.fromisoformat(
                hours[0]["hour"]
            )
            if duration > TAKES_MUCH:
                long.append({**short(t), "minutes": round(duration.total_seconds() / 60)})
    # Questions the user had to repeat: very similar quick replies during the week.
    quick = [t for t in tasks if t["kind"] == "quick"]
    duplicate = []
    for i, a in enumerate(quick):
        for b in quick[i + 1 :]:
            pa, pb = _words(a["request"]), _words(b["request"])
            if pa and pb and len(pa & pb) / len(pa | pb) >= 0.6:
                duplicate.append([a["request"][:80], b["request"][:80]])
    return {
        "week_tasks": len([t for t in tasks if t["kind"] in ("task", "quick")]),
        "failed_or_cancelled": failed[:10],
        "user_corrections": [
            {"task": c["task"], "message": (c["request"] or "")[:120]} for c in corrections[:10]
        ],
        "plans_with_change_requests": plan_changes or 0,
        "rejected_approvals": rejected[:10],
        "tasks_that_hit_the_cap": [t["task_id"] for t in caps],
        "reviews_with_failures": [
            {"task": r["task_id"], "summary": r["summary"]} for r in review[:10]
        ],
        "long_tasks": long[:10],
        "duplicate_questions": duplicate[:5],
    }


# --- proposals -------------------------------------------------------------------------------


def readable_change(change: dict[str, Any]) -> str:
    """The change of a type B improvement, to read it and hand it to Claude Code."""
    text = str(change.get("text") or "").strip()
    file = str(change.get("file") or "").strip()
    operation = str(change.get("operation") or "").strip()
    if text and operation in ("", "describe"):
        where = f" (in `{file}`)" if file else ""
        return f"What to ask Claude Code{where}:\n\n{text}\n"
    if text:
        parts = [f"Proposed change in `{file or '?'}` ({operation}):\n\n```\n{text}\n```\n"]
        if change.get("search"):
            parts.append(f"Instead of:\n\n```\n{change['search']}\n```\n")
        return "\n".join(parts)
    return (
        "Proposed change:\n\n```\n" + json.dumps(change, ensure_ascii=False, indent=2) + "\n```\n"
    )


async def save_proposals(
    engine: Engine, proposals: list[dict[str, Any]], moment: datetime | None = None
) -> list[dict[str, Any]]:
    """Validates the model's proposals, saves them and writes the type B ones to
    data/improvements/."""
    moment = moment or datetime.now(UTC)
    saved_items = []
    for_claude_code = []
    for p in proposals[:MAX_PROPOSALS]:
        title = str(p.get("title") or "").strip()[:150]
        reason = str(p.get("reason") or "").strip()[:500]
        if not title or not reason:
            continue
        kind = "A" if str(p.get("kind", "")).upper() == "A" else "B"
        data_change = p.get("change") or {}
        note = ""
        change = None
        if kind == "A":
            try:
                change = Change(
                    file=str(data_change.get("file") or ""),
                    operation=str(data_change.get("operation") or ""),
                    text=str(data_change.get("text") or ""),
                    search=str(data_change.get("search") or ""),
                )
                validate_change(engine, change)
            except Exception as e:
                kind, note = "B", f"Can't be applied without code ({e})."
        id_ = await engine.db.run(
            "INSERT INTO improvements(created, kind, title, reason, file, change, state) "
            "VALUES (?, ?, ?, ?, ?, ?, 'proposal')",
            (
                now(),
                kind,
                title,
                reason,
                change.file if change and kind == "A" else str(data_change.get("file") or ""),
                to_json(change.__dict__ if change and kind == "A" else data_change),
            ),
        )
        saved = {"id": id_, "kind": kind, "title": title, "reason": reason, "note": note}
        if kind == "B":
            for_claude_code.append({**saved, "change": data_change})
        saved_items.append(saved)
    if for_claude_code:
        folder = engine.config.paths.improvements
        folder.mkdir(exist_ok=True)
        file = folder / f"{moment.astimezone():%Y-%m-%d}.md"
        blocks = [
            f"## Improvement {m['id']}. {m['title']}\n\n{m['reason']}\n\n"
            + (f"{m['note']}\n\n" if m["note"] else "")
            + readable_change(m["change"])
            for m in for_claude_code
        ]
        header = (
            ""
            if file.exists()
            else "# Improvements for Claude Code\n\nSuggestions from the weekly maintenance that touch "
            "code or security. They are never applied on their own: give them to Claude Code if you "
            "want them.\n\n"
        )
        with file.open("a", encoding="utf-8") as f:
            f.write(header + "\n".join(blocks))
        # With separate data (AIDE_DATA), the folder can be outside the root.
        root = engine.config.paths.root
        relative = str(file.relative_to(root)) if file.is_relative_to(root) else str(file)
        for m in saved_items:
            if m["kind"] == "B":
                m["b_file"] = relative
    return saved_items


async def apply(engine: Engine, improvement_id: int) -> str:
    """Applies a proposed improvement after validating it and saving a backup."""
    row = await engine.db.one("SELECT * FROM improvements WHERE id=?", (improvement_id,))
    if row is None or row["kind"] != "A":
        return f"Improvement {improvement_id} can't be applied from here."
    if row["state"] != "proposal":
        return f"Improvement {improvement_id} is already {STATES.get(row['state'], row['state'])}."
    change = Change(**(from_json(row["change"]) or {}))
    try:
        path, new = validate_change(engine, change)
    except Exception as e:
        return f"I didn't apply improvement {improvement_id}: {e}."
    backups = engine.config.paths.improvements / "backups"
    backups.mkdir(parents=True, exist_ok=True)
    backup = backups / f"{improvement_id}-{path.name}.{datetime.now():%Y%m%d-%H%M%S}"
    if path.is_file():
        shutil.copy2(path, backup)
    else:
        backup.write_text("", encoding="utf-8")  # it did not exist: undoing deletes it
    if path == engine.config.paths.memory:
        await engine.memory.add(new)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(new, encoding="utf-8")
    await engine.db.run(
        "UPDATE improvements SET state='applied', backup=?, resolved=? WHERE id=?",
        (str(backup), now(), improvement_id),
    )
    return f"Improvement {improvement_id} applied in {change.file}. If you don't like it: /undo_improvement {improvement_id}"


async def discard(engine: Engine, improvement_id: int) -> str:
    """Discards a pending improvement proposal."""
    row = await engine.db.one("SELECT state FROM improvements WHERE id=?", (improvement_id,))
    if row is None or row["state"] != "proposal":
        return f"Improvement {improvement_id} is no longer pending."
    await engine.db.run(
        "UPDATE improvements SET state='discarded', resolved=? WHERE id=?", (now(), improvement_id)
    )
    return f"Improvement {improvement_id} discarded."


async def undo(engine: Engine, improvement_id: int) -> str:
    """Undoes an applied improvement by restoring its backup."""
    row = await engine.db.one("SELECT * FROM improvements WHERE id=?", (improvement_id,))
    if row is None or row["state"] != "applied" or not row["backup"]:
        return f"Improvement {improvement_id} is not applied."
    change = Change(**(from_json(row["change"]) or {}))
    path = allowed_path(engine, change.file)
    backup = Path(row["backup"])
    if path is None or not backup.is_file():
        return f"I can't find the backup copy of improvement {improvement_id}."
    if backup.stat().st_size == 0 and change.operation == "create":
        path.unlink(missing_ok=True)
    else:
        shutil.copy2(backup, path)
    await engine.db.run(
        "UPDATE improvements SET state='undone', resolved=? WHERE id=?", (now(), improvement_id)
    )
    return f"Improvement {improvement_id} undone: {change.file} is back to how it was."
