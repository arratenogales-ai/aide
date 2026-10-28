"""Data for each panel view. Queries only: the actions are in app.py.

Decisions (approvals, plans, caps and memory proposals) are made only on Telegram; the
panel shows them for reference.
"""

from __future__ import annotations

import itertools
import json
import logging
import re
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aide.common.text import normalize
from aide.core.db import from_json
from aide.core.finished import inside_of
from aide.core.presentation import GROUPS, group_by_key, subtitle, task_group
from aide.core.progress import describe_tool, progress
from aide.core.projects import CONTEXT, existing_projects
from aide.core.states import TaskState
from aide.core.tasks import Task, task_from_row
from aide.core.usage import format_tokens

if TYPE_CHECKING:
    from aide.core.engine import Engine

log = logging.getLogger(__name__)

IGNORE_IN_PROJECTS = {
    "node_modules",
    ".venv",
    "venv",
    ".git",
    "__pycache__",
    ".cache",
    "dist",
    "build",
}
MAX_PROJECT_FILES = 300
MAX_TRANSCRIPTION = 4000
# Settings shown in the panel; permissions.yaml can only be read.
SETTINGS_FILES = ("models.yaml", "settings.yaml", "permissions.yaml")
ONLY_READ = frozenset({"permissions.yaml"})
TOKENS_TASK = (
    "(SELECT SUM(input_tokens + output_tokens + cache_read + cache_creation) "
    "FROM usage u WHERE u.task_id = t.id) AS tokens"
)
EVENT_TITLES = {
    "session": "Session",
    "agent_text": "Agent text",
    "triage": "Request classified",
    "plan": "Plan proposed",
    "delegation": "Handed to a subagent",
    "subagent_report": "Subagent report",
    "start_subagent": "Subagent starts",
    "end_subagent": "Subagent finishes",
    "subagent_plain_second": "Subagent in the background",
    "error_tool": "Tool error",
    "blocked": "Action blocked",
    "notice": "Notice",
    "possible_injection": "Possible instructions in external content",
    "token_cap": "Token budget exceeded",
    "compaction": "Conversation summarized",
    "delivery": "Delivery",
    "final_folder": "Copied to the final folder",
    "tool": "Tool",
    "error": "Error",
    "external_action": "External action",
    "approval_requested": "Approval requested",
    "batch_approval_requested": "Batch approval requested",
    "approval_reused": "Approval reused",
    "agent_notice": "Agent notice",
    "draft_reserved": "Draft set aside",
    "model_change": "Model changed",
    "context_no_updated": "Context not updated",
    "pending_context": "Context pending",
    "since_quick": "From a quick answer",
    "document": "Document",
    "waiting_plain_second": "Waiting for background work",
    "missing_delivery": "Delivery missing",
    "expired_google": "Google authorization expired",
    "chart": "Chart",
    "maintenance": "Maintenance",
    "proposal_memory": "Memory proposal",
    "user_message": "Your message",
    "model": "Model",
    "down_browser": "Browser down",
    "approved_plan": "Plan approved",
    "question": "Question",
    "answered_question": "Question answered",
    "scheduled_task": "Scheduled task",
    "scheduled_task_skipped": "Scheduled task skipped",
    "network": "Network",
    "retry": "Retry",
    "reply_to": "Reply to",
    "review": "Review",
    "start_review": "Review starts",
    "review_no_completed": "Review not completed",
    "script": "Script",
    "background_unfinished": "Background work unfinished",
    "no_connection": "Offline",
    "extended_cap": "Cap raised",
    "user_no_allowed": "User not allowed",
    "verification": "Check",
}
# Internal states (states.py) with their English name, for the full timeline.
STATE_NAMES = {
    "received": "received",
    "in_triage": "triage",
    "planning": "planning",
    "waiting_plan": "waiting for plan",
    "in_queue": "queued",
    "running": "running",
    "waiting_approval": "waiting for approval",
    "reviewing": "reviewing",
    "completed": "completed",
    "failed": "failed",
    "cancelled": "canceled",
    "paused_limit": "paused (plan limit)",
    "paused_cap": "paused (usage cap)",
    "paused_login": "paused (login)",
    "paused_browser": "paused (browser)",
    "paused_shutdown": "paused (stopped)",
}
# What triage decided (triage.py), as shown in the timeline.
TRIAGE_NAMES = {
    "conversation": "conversation",
    "quick_question": "quick question",
    "new_task": "new task",
    "continuation": "follow-up",
    "command": "command",
}
# The state of an approval, in the task history.
APPROVAL_NAMES = {
    "pending": "pending",
    "approved": "approved",
    "rejected": "rejected",
    "cancelled": "canceled",
}


def event_title(kind: str) -> str:
    return EVENT_TITLES.get(kind, kind.replace("_", " ").capitalize())


def state_name(state: str) -> str:
    return STATE_NAMES.get(state, state.replace("_", " "))


# English date formats: “%b” is the three-letter month and “%-d” the day without a leading zero
# (replaced here, independent of the locale and the platform).
SHORT = "%b %-d, %H:%M"  # Oct 28, 14:05
SHORT_SECONDS = "%b %-d, %H:%M:%S"
WITH_YEAR = "%b %-d, %Y"  # Oct 28, 2026
WITH_YEAR_HOUR = "%b %-d, %Y, %H:%M"


def local(value: str | None, zone: ZoneInfo, format_: str = SHORT) -> str:
    """Formats a stored date in a time zone, returning the original text if invalid."""
    if not value:
        return ""
    try:
        moment = datetime.fromisoformat(value).astimezone(zone)
    except ValueError:
        return value
    format_ = format_.replace("%b", MONTHS[moment.month - 1][:3]).replace("%-d", str(moment.day))
    return moment.strftime(format_)


def summarized_timeline(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Consecutive tool calls are merged into one line; everything else is left as is."""
    summary: list[dict[str, Any]] = []
    block: dict[str, Any] | None = None
    for e in events:
        if e["kind"] == "tool":
            data = e.get("data") or {}
            detail = (e.get("summary") or "").split(": ", 1)[-1]
            action = describe_tool(str(data.get("tool") or ""), detail)
            if block is None:
                block = {"kind": "actions", "local_hour": e["local_hour"], "n": 0, "examples": []}
                summary.append(block)
            block["n"] += 1
            if action not in block["examples"] and len(block["examples"]) < 4:
                block["examples"].append(action)
            continue
        block = None
        kind = e["kind"]
        summary.append(
            {
                "kind": kind,
                "title": event_title(kind),
                "local_hour": e["local_hour"],
                # Internal values (new_task, quick_question...) in English.
                "summary": TRIAGE_NAMES.get(
                    e.get("summary") or "", (e.get("summary") or "").replace("_", " ")
                )
                if kind == "triage"
                else e.get("summary") or "",
            }
        )
    return summary


MONTHS = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)


async def _card(do: Any, default: Any) -> Any:
    """A “Today” card: if it fails, it is logged and comes out empty (it does not break the
    page)."""
    try:
        return await do()
    except Exception:
        log.exception('couldn\'t prepare the "%s" card on Today', do.__name__)
        return default


def gmail_link(email: str | None) -> str | None:
    """The link to an email in Gmail (opens in the user's browser)."""
    return f"https://mail.google.com/mail/u/0/#all/{email}" if email else None


def join_with_and(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def month_day(day: date) -> str:
    """“Oct 31”: the three-letter month and the day."""
    return f"{MONTHS[day.month - 1][:3]} {day.day}"


def short_date(day: date) -> str:
    """“Oct 31, 2026”: the three-letter month, the day and the year."""
    return f"{month_day(day)}, {day.year}"


def time_left(missing: int) -> str:
    return "today" if missing == 0 else ("tomorrow" if missing == 1 else f"in {missing} days")


def important_date(dates: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The date shown without opening a document: the next one or, if all have passed,
    the last one."""
    upcoming = [f for f in dates if f["missing"] >= 0]
    if upcoming:
        return min(upcoming, key=lambda f: f["date"])
    return max(dates, key=lambda f: f["date"], default=None)


def order_in_folder(document: dict[str, Any]) -> tuple[int, str, int]:
    """Within a Documents folder, first what has a date ahead (the nearest first) and then the
    rest, most recently saved first."""
    f = document["important"]
    return (0, f["date"], 0) if f and f["missing"] >= 0 else (1, "", -document["id"])


# My profile: icon and color of each section of profile.md, by a word in its title; sections
# without one get the next color of the wheel that is not the previous section's.
PROFILE_SECTIONS = (
    # Words in Spanish and English (the profile may be written in either).
    ("quien", "profile", "turquoise"),
    ("who", "profile", "turquoise"),
    ("about me", "profile", "turquoise"),
    ("objetivo", "goal", "green"),
    ("goal", "goal", "green"),
    ("trabaj", "agent", "blue"),
    ("work", "agent", "blue"),
    ("redac", "write", "peach"),
    ("writ", "write", "peach"),
    ("cuenta", "key", "butter"),
    ("account", "key", "butter"),
    ("contacto", "contacts", "lilac"),
    ("contact", "contacts", "lilac"),
    ("nunca", "forbidden", "pink"),
    ("never", "forbidden", "pink"),
)
COLORS_BY_TURN = ("blue", "green", "lilac", "peach", "pink", "butter", "turquoise")
# “- Key: value”: a short key, without quotes or parentheses (“Constructions like "Label:
# explanation"” are not a key).
PROFILE_DATUM = re.compile(r"^(?P<key>[^:\"«»“”()]{1,30}):\s+(?P<value>.+)$")


def profile_sections(text: str) -> list[dict[str, Any]]:
    """profile.md as cards: one section for each “## Title”, with its data (“- Key: value”,
    with the key separate), its loose points and what hangs from each one (“  - …”)."""
    sections: list[dict[str, Any]] = []
    wheel = itertools.cycle(COLORS_BY_TURN)
    for line in text.splitlines():
        if not line.strip() or line.startswith("# "):
            continue
        if line.startswith("## "):
            title = line[3:].strip()
            key = normalize(title)
            icon, color = next(((i, c) for word, i, c in PROFILE_SECTIONS if word in key), ("", ""))
            if not icon:
                previous = sections[-1]["color"] if sections else None
                icon, color = "memory", next(c for c in wheel if c != previous)
            sections.append({"title": title, "icon": icon, "color": color, "data": []})
            continue
        if not sections:
            sections.append(
                {"title": "Profile", "icon": "profile", "color": "turquoise", "data": []}
            )
        data = sections[-1]["data"]
        clean = line.strip().removeprefix("- ").removeprefix("* ").strip()
        clean = clean.replace("**", "").replace("`", "")
        if line[:1] in (" ", "\t") and data:
            if line.strip()[:2] in ("- ", "* "):
                data[-1]["sub"].append(clean)
            elif data[-1]["sub"]:
                data[-1]["sub"][-1] += " " + clean
            else:
                data[-1]["value"] += " " + clean
            continue
        m = PROFILE_DATUM.match(clean)
        data.append(
            {
                "key": m["key"] if m else None,
                "value": m["value"] if m else clean,
                "sub": [],
            }
        )
    return [a for a in sections if a["data"]]


PROJECT_COLORS = ("blue", "green", "lilac", "peach", "turquoise", "butter")


def project_description(context: str, long: int = 190) -> str:
    """The first two sentences of the first paragraph of CONTEXT.md (or the first, if with the
    second it exceeds about 190 characters), without titles or Markdown marks."""
    paragraph: list[str] = []
    in_code = False
    for line in context.splitlines():
        clean = line.strip()
        if clean.startswith("```"):
            in_code = not in_code
            if paragraph:
                break
            continue
        if in_code or not clean or clean.startswith(("#", ">", "|", "<!--")):
            if paragraph:
                break
            continue
        paragraph.append(clean.removeprefix("- ").removeprefix("* "))
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", " ".join(paragraph))
    text = text.replace("**", "").replace("`", "")
    output = ""
    for phrase in re.split(r"(?<=[.!?])\s+", text)[:2]:
        if output and len(output) + 1 + len(phrase) > long:
            break
        output = f"{output} {phrase}".strip()
    if len(output) > long:
        output = output[:long].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return output


class Views:
    """Prepares the data shown in the panel from the database and the files."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self.db = engine.db
        self.paths = engine.config.paths
        self.name_zone = engine.config.time_zone
        self.zone = ZoneInfo(self.name_zone)

    def date(self, value: str | None, format_: str = SHORT) -> str:
        return local(value, self.zone, format_)

    # --- version for the live update -------------------------------------------------------

    async def version(self) -> str:
        """Returns a version string that changes when the panel data changes, for live updates."""
        row = await self.db.one(
            "SELECT (SELECT MAX(updated) FROM tasks) AS t, (SELECT MAX(id) FROM events) AS e, "
            "(SELECT COUNT(*) FROM approvals WHERE state='pending') AS a, "
            "(SELECT MAX(COALESCE(resolved, created)) FROM approvals) AS r, "
            "(SELECT COUNT(*) FROM memory_proposals WHERE state='pending') AS m, "
            "(SELECT MAX(COALESCE(last_run, created)) FROM scheduled_tasks) AS p, "
            "(SELECT MAX(id) FROM steps) AS s, (SELECT MAX(updated) FROM steps) AS sa"
        )
        from aide import life

        calendar = await self.db.kv_read(life.CALENDAR_KEY) or {}
        extra = await self.db.one(
            "SELECT (SELECT MAX(reviewed) FROM emails) AS c, "
            "(SELECT COUNT(*) || '-' || MAX(id) || '-' || MAX(COALESCE(saved, created)) FROM "
            "documents WHERE state != 'discarded') AS doc"
        )
        health = await self.db.one(
            "SELECT (SELECT COUNT(*) || '-' || MAX(day) FROM health_period) AS sr, "
            "(SELECT MAX(id) FROM health_gym) AS sg, (SELECT COUNT(*) || '-' || MAX(id) || "
            "'-' || SUM(COALESCE(kcal, 0)) FROM health_meals) AS sc, (SELECT COUNT(*) || '-' || "
            "MAX(id) FROM health_habits) AS sh, (SELECT MAX(updated) FROM health_profile) AS sp"
        )
        return json.dumps(
            {
                **(row or {}),
                **(extra or {}),
                **(health or {}),
                "cal": [calendar.get(k) for k in ("updated", "attempted", "error")],
                # When the day changes, “Today”, “Tomorrow” and what is due are
                # recalculated.
                "day": datetime.now(self.zone).date().isoformat(),
            },
            sort_keys=True,
            default=str,
        )

    # --- common pieces -------------------------------------------------------------------------------

    def state(self, task: Task) -> dict[str, str | None]:
        g = task_group(task.state)
        return {"key": g.key, "name": g.name, "detail": subtitle(task, self.name_zone)}

    def final_folder(self, task: Task) -> dict[str, Any] | None:
        """Describes a task's final folder and whether it exists and can be opened."""
        if not task.final_folder:
            return None
        path = Path(task.final_folder)
        return {
            "path": str(path),
            "name": path.name,
            "exists": path.is_dir(),
            "openable": path.is_dir() and inside_of(path, self.paths.finished),
        }

    async def card(self, task: Task) -> dict[str, Any]:
        """Card of a running task: status, progress, what it is doing and how long it has been
        running."""
        p = await progress(self.engine.tasks, task)
        return {
            "task": task,
            "state": self.state(task),
            "progress": p,
            "elapsed": p.elapsed(),
        }

    # --- home --------------------------------------------------------------------------------

    async def start(self) -> dict[str, Any]:
        """Collects the start page data: running and paused tasks, deliveries and approvals."""
        in_progress, waiting, in_pause = GROUPS[:3]
        running = await self.engine.tasks.list_items(
            states=[*in_progress.states, *waiting.states], limit=30
        )
        running = [t for t in running if t.kind != "message"]
        paused = await self.engine.tasks.list_items(states=list(in_pause.states), limit=30)
        deliveries = await self.engine.tasks.list_items(
            states=[TaskState.COMPLETED], kind="task", limit=8
        )
        approvals = await self.db.everything(
            "SELECT id, task_id, detail, created FROM approvals WHERE state='pending' ORDER BY id"
        )
        proposals = await self.db.value(
            "SELECT COUNT(*) FROM memory_proposals WHERE state='pending'"
        )
        # What awaits a reply from the user on Telegram: approvals, plans, caps and memory
        # proposals.
        decisions = await self.db.value(
            "SELECT COUNT(*) FROM tasks WHERE state IN ('waiting_plan', 'paused_cap')"
        )
        return {
            **await self.today(),
            "waiting": len(approvals) + (decisions or 0) + (proposals or 0),
            "cards": [await self.card(t) for t in reversed(running)],
            "paused": [{"task": t, "state": self.state(t)} for t in paused],
            "approvals": [
                {**a, "line": (a["detail"] or "").splitlines()[0][:160] if a["detail"] else ""}
                for a in approvals
            ],
            "proposals": proposals or 0,
            "deliveries": [{"task": t, "folder": self.final_folder(t)} for t in deliveries],
            "scheduled_tasks": await self.db.value(
                "SELECT COUNT(*) FROM scheduled_tasks WHERE active=1"
            )
            or 0,
        }

    async def today(self) -> dict[str, Any]:
        """The “Today” cards: calendar, email and documents. Everything from the database;
        the calendar, from the last thing read from Google. If a card fails, it comes out empty
        and the rest of the page carries on."""
        from aide import life

        moment = datetime.now(UTC)
        local = moment.astimezone(self.zone)
        today = local.date()
        core = self.engine.core

        async def calendar() -> dict[str, Any]:
            return await life.calendar(core, moment)

        async def emails() -> list[dict[str, Any]]:
            return [
                {
                    **c,
                    "sender": c.get("from_name") or c.get("from_address") or "",
                    "timing": self.date(c.get("reviewed")),
                    "link": gmail_link(c.get("thread") or c.get("id")),
                }
                for c in await life.important_emails(self.db)
            ]

        async def documents() -> list[dict[str, Any]]:
            output = []
            for v in await life.next_documents(self.db, 60, today):
                date_ = date.fromisoformat(v["date"])
                missing = (date_ - today).days
                output.append(
                    {
                        **v,
                        "title": v.get("title") or "A document",
                        # With the weekday only for this week; everything
                        # else, with its month.
                        "day": life.day_name(date_, today) if missing <= 6 else month_day(date_),
                        "missing": missing,
                    }
                )
            return output

        async def health() -> dict[str, Any]:
            # The small health card: by code, from the database.
            from aide.health import summary

            return await summary.card_today(self.db, today)

        data = {
            # “Tuesday, October 28”.
            "date_today": f"{life.WEEK_DAYS[local.weekday()]}, {MONTHS[local.month - 1]} "
            f"{local.day}",
            "greeting": (
                "Good morning"
                if local.hour < 12
                else ("Good afternoon" if local.hour < 18 else "Good evening")
            )
            + (f", {self.engine.config.user}" if self.engine.config.user else ""),
            "calendar": await _card(calendar, {"days": [], "today": [], "read": False}),
            "emails": await _card(emails, []),
            "documents": await _card(documents, []),
            "health": await _card(health, {"something": False}),
        }
        appointments = len(data["calendar"].get("today") or [])
        n_emails = len(data["emails"])
        expire = sum(1 for v in data["documents"] if v["missing"] <= 30)
        data["counters"] = [
            {
                "figure": appointments,
                "that": "events today" if appointments != 1 else "event today",
                "color": "lilac",
                "icon": "calendar",
                "link": "",
            },
            {
                "figure": n_emails,
                "that": "important emails" if n_emails != 1 else "important email",
                "color": "blue",
                "icon": "email",
                "link": "/email",
            },
            {
                "figure": expire,
                "that": "dates in 30 days" if expire != 1 else "date in 30 days",
                "color": "peach",
                "icon": "documents",
                "link": "/documents",
            },
        ]
        parts = []
        if appointments:
            parts.append(f"{appointments} {'event' if appointments == 1 else 'events'} today")
        if expire:
            parts.append(
                f"{expire} {'date' if expire == 1 else 'dates'} from your documents in 30 days"
            )
        data["summary"] = f"You have {join_with_and(parts)}." if parts else "Nothing pending today."
        return data

    async def documents(self) -> dict[str, Any]:
        """Your saved documents, in folders by topic: each one shows its important
        date and the rest is inside."""
        from aide.paperwork import categories, inbox, registry

        today = datetime.now(self.zone).date()
        by_folder: dict[str, list[dict[str, Any]]] = {c[0]: [] for c in categories.CATEGORIES}
        for d in await registry.saved(self.db):
            dates = []
            for f in await registry.dates_of(self.db, d["id"]):
                day = date.fromisoformat(f["date"])
                missing = (day - today).days
                dates.append(
                    {
                        **f,
                        "day": short_date(day),
                        "missing": missing,
                        "timing": time_left(missing) if 0 <= missing <= 30 else "",
                    }
                )
            by_folder[categories.category(d)].append(
                {
                    **d,
                    "title": d.get("title") or "Untitled document",
                    "dates": dates,
                    "important": important_date(dates),
                    "saved_text": self.date(d.get("saved"), WITH_YEAR),
                }
            )
        folders = []
        for key, name, icon, color in categories.CATEGORIES:
            documents = by_folder[key]
            if key == "other" and not documents:
                continue
            documents.sort(key=order_in_folder)
            folders.append(
                {
                    "key": key,
                    "name": name,
                    "icon": icon,
                    "color": color,
                    "documents": documents,
                }
            )
        proposed = await self.db.value("SELECT COUNT(*) FROM documents WHERE state='proposed'")
        return {
            "folders": folders,
            "total": sum(len(c["documents"]) for c in folders),
            "proposed": proposed or 0,
            "inbox": self.engine.config.paths.inbox.name,
            # What “Upload documents” lets you choose: the types the inbox can read.
            "kinds": ",".join(sorted(inbox.READABLE)),
        }

    # --- tasks -------------------------------------------------------------------------------

    async def tasks(self, key: str | None, query: str | None) -> list[dict[str, Any]]:
        """Lists tasks, filtered by group and text search, with their usage."""
        conditions, parameters = ["t.kind != 'message'"], []
        g = group_by_key(key) if key else None
        if g is not None:
            conditions.append(f"t.state IN ({', '.join('?' for _ in g.states)})")
            parameters += [e.value for e in g.states]
        if query:
            conditions.append("(t.request LIKE ? OR t.title LIKE ? OR t.summary LIKE ?)")
            parameters += [f"%{query}%"] * 3
        rows = await self.db.everything(
            f"SELECT t.*, {TOKENS_TASK} FROM tasks t WHERE {' AND '.join(conditions)} "
            "ORDER BY t.id DESC LIMIT 100",
            parameters,
        )
        result = []
        for row in rows:
            task = task_from_row(row)
            result.append(
                {
                    "task": task,
                    "state": self.state(task),
                    "tokens": format_tokens(row["tokens"]) if row["tokens"] else "",
                    "folder": self.final_folder(task),
                }
            )
        return result

    async def task(self, task_id: int) -> dict[str, Any] | None:
        """Returns a task with its events and files, or None if it does not exist."""
        task = await self.engine.tasks.get(task_id)
        if task is None:
            return None
        events = []
        for e in await self.engine.tasks.events(task_id):
            data = e.get("data")
            if isinstance(data, str):
                data = from_json(data)
            events.append({**e, "data": data, "local_hour": self.date(e["hour"], SHORT_SECONDS)})
        folder = self.paths.task(task_id)
        files = []
        if folder.is_dir():
            for sub in ("results", "input", "data"):
                base = folder / sub
                if base.is_dir():
                    for path in sorted(p for p in base.rglob("*") if p.is_file())[:200]:
                        files.append(
                            {
                                "folder": sub,
                                "name": str(path.relative_to(base)),
                                "path": str(path),
                                "size": path.stat().st_size,
                            }
                        )
        return {
            **await self.card(task),
            "transitions": await self.engine.tasks.transitions(task_id),
            "events": events,
            "timeline": summarized_timeline(events),
            "approvals": await self.db.everything(
                "SELECT * FROM approvals WHERE task_id=? ORDER BY id", (task_id,)
            ),
            "consumption": await self.engine.usage.task_consumption(task_id),
            "folder": self.final_folder(task),
            "files": files,
            "md_progress": (folder / "progress.md").read_text(encoding="utf-8")[-6000:]
            if (folder / "progress.md").is_file()
            else "",
        }

    def transcription(self, session_id: str | None) -> list[dict[str, str]]:
        """Full conversation of the SDK session (read on demand)."""
        if not session_id:
            return []
        candidates = list((Path.home() / ".claude" / "projects").glob(f"*/{session_id}.jsonl"))
        if not candidates:
            return []
        messages = []
        for line in candidates[0].read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if d.get("type") not in ("user", "assistant"):
                continue
            content = (d.get("message") or {}).get("content")
            blocks = [content] if isinstance(content, str) else content or []
            for b in blocks:
                if isinstance(b, str):
                    messages.append(
                        {"who": d["type"], "kind": "text", "text": b[:MAX_TRANSCRIPTION]}
                    )
                elif b.get("type") == "text":
                    messages.append(
                        {
                            "who": d["type"],
                            "kind": "text",
                            "text": b.get("text", "")[:MAX_TRANSCRIPTION],
                        }
                    )
                elif b.get("type") == "tool_use":
                    entry = json.dumps(b.get("input"), ensure_ascii=False)[:1500]
                    messages.append(
                        {
                            "who": "assistant",
                            "kind": f"tool {b.get('name')}",
                            "text": entry,
                        }
                    )
                elif b.get("type") == "tool_result":
                    result = b.get("content")
                    if isinstance(result, list):
                        result = "\n".join(
                            x.get("text", "[image]") for x in result if isinstance(x, dict)
                        )
                    messages.append(
                        {
                            "who": "user",
                            "kind": "result",
                            "text": str(result)[:MAX_TRANSCRIPTION],
                        }
                    )
        return messages

    # --- scheduled tasks -----------------------------------------------------------------------

    async def scheduled_tasks(self) -> list[dict[str, Any]]:
        """Lists scheduled tasks with their timing and next and last run dates."""
        rows = await self.db.everything(
            "SELECT * FROM scheduled_tasks ORDER BY active DESC, id DESC"
        )
        return [
            {
                **f,
                "when_data": from_json(f["timing"]) or {},
                "command_text": " ".join(from_json(f["command"]) or []) if f.get("command") else "",
                "next_local": self.date(f["next_run"], WITH_YEAR_HOUR) if f["active"] else "",
                "last_local": self.date(f["last_run"], WITH_YEAR_HOUR),
            }
            for f in rows
        ]

    async def scheduled_tasks_how_many(self) -> int:
        """Those shown in the Scheduled tab of Tasks, for its counter."""
        return await self.db.value("SELECT COUNT(*) FROM scheduled_tasks") or 0

    # --- memory and profile ------------------------------------------------------------------

    def instructions_lines(self) -> int:
        """Counts the lines of the agent's instructions and the profile."""
        total = 0
        for path in (self.paths.agent / "CLAUDE.md", self.paths.profile):
            if path.is_file():
                total += len(path.read_text(encoding="utf-8").splitlines())
        return total

    async def memory(self) -> dict[str, Any]:
        """My profile: profile.md as cards and the facts from memory.md, latest first; both
        files, as they are, to edit by hand."""
        profile = (
            self.paths.profile.read_text(encoding="utf-8") if self.paths.profile.is_file() else ""
        )
        facts = [
            {
                "text": h.text,
                "day": short_date(date.fromisoformat(h.date)) if h.date else "",
            }
            for h in reversed(self.engine.memory.facts())
        ]
        return {
            "profile": profile,
            "sections": profile_sections(profile),
            "memory": self.paths.memory.read_text(encoding="utf-8")
            if self.paths.memory.is_file()
            else "",
            "facts": facts,
            "proposals": await self.engine.memory.pending_proposals(),
            "lines": self.instructions_lines(),
        }

    # --- projects ----------------------------------------------------------------------------

    def projects(self) -> list[dict[str, Any]]:
        """Lists the projects with their modification date and description."""
        items = []
        for n, name in enumerate(existing_projects(self.paths.projects)):
            folder = self.paths.projects / name
            context = folder / CONTEXT
            items.append(
                {
                    "name": name,
                    "modified": short_date(
                        datetime.fromtimestamp(folder.stat().st_mtime, self.zone).date()
                    ),
                    "description": project_description(context.read_text(encoding="utf-8"))
                    if context.is_file()
                    else "",
                    "color": PROJECT_COLORS[n % len(PROJECT_COLORS)],
                }
            )
        return items

    def project(self, name: str) -> dict[str, Any] | None:
        """Returns a project's files and context, or None if it is outside the projects folder."""
        folder = (self.paths.projects / name).resolve()
        if not folder.is_dir() or not folder.is_relative_to(self.paths.projects.resolve()):
            return None
        files = []
        for path in sorted(folder.rglob("*")):
            relative = path.relative_to(folder)
            if any(p in IGNORE_IN_PROJECTS for p in relative.parts) or not path.is_file():
                continue
            files.append({"name": str(relative), "path": str(path), "size": path.stat().st_size})
            if len(files) >= MAX_PROJECT_FILES:
                break
        context = folder / CONTEXT
        return {
            "name": folder.name,
            "context": context.read_text(encoding="utf-8") if context.is_file() else "",
            "files": files,
        }

    # --- settings and logs (Advanced) ----------------------------------------------------------

    async def email(self) -> dict[str, Any]:
        """Email page: if reading is active, the latest alerts and the senders the user has
        marked with the buttons."""
        settings_view = self.engine.config.settings.email
        notices = await self.db.everything(
            "SELECT * FROM emails WHERE decision='notice' ORDER BY reviewed DESC LIMIT 30"
        )
        senders = await self.db.everything("SELECT * FROM senders ORDER BY since DESC")
        seen = await self.db.value("SELECT COUNT(*) FROM emails")
        return {
            "active": settings_view.active,
            "minutes": settings_view.minutes,
            "seen": seen or 0,
            "notices": [{**a, "reviewed_local": self.date(a.get("reviewed"))} for a in notices],
            "senders": [
                {**r, "since_local": self.date(r.get("since"), WITH_YEAR)} for r in senders
            ],
        }

    def settings_view(self) -> list[dict[str, Any]]:
        """Returns the settings files with their contents and whether they can be edited."""
        return [
            {
                "name": name,
                "content": (self.paths.config / name).read_text(encoding="utf-8"),
                "editable": name not in ONLY_READ,
            }
            for name in SETTINGS_FILES
            if (self.paths.config / name).is_file()
        ]

    async def logs(self) -> dict[str, Any]:
        """Returns the last log lines and the recent error events."""
        file = self.paths.logs / "aide.log"
        lines = (
            file.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
            if file.is_file()
            else []
        )
        errors = await self.db.everything(
            "SELECT * FROM events WHERE kind IN ('error_tool', 'error', 'possible_injection', "
            "'down_browser', 'expired_google', 'background_unfinished') ORDER BY id DESC LIMIT 60"
        )
        return {
            "lines": list(reversed(lines)),
            "errors": [{**e, "local_hour": self.date(e["hour"], SHORT_SECONDS)} for e in errors],
        }
