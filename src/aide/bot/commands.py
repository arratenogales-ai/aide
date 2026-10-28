"""Telegram commands and buttons that are not part of the core."""

from __future__ import annotations

import asyncio
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

from aide.bot.channel import Button, Entry
from aide.bot.formatting import bold, esc, truncate
from aide.common.paths import FINISHED_FOLDER
from aide.core.presentation import GROUPS, readable_state, subtitle, task_group
from aide.core.progress import progress as read_progress
from aide.core.states import IN_PROGRESS, PAUSED, TaskState
from aide.core.tasks import Task
from aide.core.usage import bar
from aide.health.telegram import HANDLERS as HEALTH_HANDLERS

if TYPE_CHECKING:
    from aide.core.engine import Engine

log = logging.getLogger(__name__)

HELP = """<b>Commands</b>
/status · tasks in progress, waiting for you or paused, and plan limits
/tasks · last ten tasks
/task N · progress and details of a task
/stop N · cancel a task
/pause N · pause a task
/resume N · resume a task
/plan text · task with a plan first
/opus · /sonnet · /max · model for the next task
/usage · plan limits and usage per task
/memory · see the memory
/forget text · delete a fact from the memory
/schedule text · schedule a task
/scheduled · scheduled tasks
/profile · see your profile
/undo_improvement N · undo an improvement applied from the weekly report
/documents · read the new documents in the inbox
/period · your cycle; /period today, /period 24-10 or /period end to log it
/gym · your week at the gym; or send me “squat 4x8 60”
/meals · what you ate today (send the photo with “dinner”, “breakfast”...); /meals week
/water · /sleep 7.5 · /steps 8000 · /weight 61.5 · your habits
/health · your day against your targets; /health profile for your profile
/panel · address of the local panel
/google · renew the Google authorization now (Gmail and Calendar)

To continue a task, reply to any of its messages."""

PRESENTATION = """<b>{greeting} I'm Aide.</b>
Send me whatever you need as text, a voice note or a file and I'll see it through to the end: research, code, writing, your email and calendar, documents and scheduled tasks. Before I send, publish or invite anyone, I'll ask for your approval with buttons."""


def presentation(name: str) -> str:
    """The /start message, with the user's name (USER_NAME) if they set it."""
    greeting = f"Hi, {esc(name)}." if name else "Hi."
    return PRESENTATION.format(greeting=greeting)


def _line(task: Task, detail: str | None = None) -> str:
    return esc(f"{task.id}. {task.short_name}" + (f" ({detail})" if detail else ""))


def _lowercase(text: str) -> str:
    """“Paused (Chrome closed)” -> “paused (Chrome closed)”: only the first letter."""
    return text[:1].lower() + text[1:]


def _number(arguments: str) -> int | None:
    text = arguments.strip().lstrip("#")
    return int(text) if text.isdigit() else None


async def run_command(engine: Engine, entry: Entry) -> None:
    """Runs the handler for an entry's command, or handles a button press."""
    if entry.kind == "button":
        await _button(engine, entry.button_datum or "")
        return
    name = (entry.command or "").lower()
    handler = HANDLERS.get(name)
    if handler is None:
        await engine.notify(None, f"I don't know the command /{name}. Type /help to see the list.")
        return
    await handler(engine, entry)


async def _start(engine: Engine, entry: Entry) -> None:
    await engine.notify(None, f"{presentation(engine.config.user)}\n\n{HELP}", html=True)


async def _help(engine: Engine, entry: Entry) -> None:
    await engine.notify(None, HELP, html=True)


async def _state_cmd(engine: Engine, entry: Entry) -> None:
    zone = engine.config.time_zone
    approvals = await engine.approvals.pending()
    parts = [bold("Status")]
    # The three groups of what is not finished: in progress, waiting for your reply and paused.
    for g in GROUPS[:3]:
        items = await engine.tasks.list_items(states=list(g.states), limit=20)
        if not items:
            continue
        lines = [
            _line(t, "queued" if t.state == TaskState.IN_QUEUE else subtitle(t, zone))
            for t in reversed(items)
        ]
        parts.append(bold(g.name) + "\n" + "\n".join(lines))
    if approvals:
        lines = [
            esc(f"#{a['id']} (task {a['task_id']}): {truncate(a['detail'].splitlines()[0], 80)}")
            for a in approvals
        ]
        parts.append(bold("Pending approvals") + "\n" + "\n".join(lines))
    if len(parts) == 1:
        parts.append(esc("Nothing running or pending."))
    await engine.refresh_limits()  # if the saved data is old, new data is requested
    parts.append(esc(await engine.usage.limits_text(engine.config.time_zone)))
    await engine.notify(None, "\n\n".join(parts), html=True)


async def _tasks(engine: Engine, entry: Entry) -> None:
    tasks = await engine.tasks.list_items(kind="task", limit=10)
    if not tasks:
        await engine.notify(None, "No tasks yet.")
        return
    zone = engine.config.time_zone
    lines = [_line(t, _lowercase(readable_state(t, zone))) for t in tasks]
    await engine.notify(None, bold("Latest tasks") + "\n" + "\n".join(lines), html=True)


async def progress_text(engine: Engine, task: Task) -> str:
    """Progress summary of a task: the same as the card and the panel detail."""
    zone = engine.config.time_zone
    p = await read_progress(engine.tasks, task)
    parts = [
        bold(f"Task {task.id} · {readable_state(task, zone)}")
        + "\n"
        + esc(task.title or task.short_name)
    ]
    lines = []
    if p.total:
        lines.append(f"{p.facts} of {p.total} steps done {bar(p.percentage)}")
    if p.now:
        lines.append(p.now + ".")
    if len(p.subagents) == 1:
        lines.append(f"Subagent at work: {p.subagents[0]}.")
    elif p.subagents:
        lines.append(f"Subagents at work: {', '.join(p.subagents)}.")
    elapsed = p.elapsed()
    key = task_group(task.state).key
    if elapsed and key in ("in_progress", "waiting"):
        lines.append(f"Running for {elapsed}.")
    elif elapsed and key == "completed":
        lines.append(f"Took {elapsed}.")
    if lines:
        parts.append(esc("\n".join(lines)))
    if p.steps:
        parts.append(esc("\n".join(f"{step.mark} {step.subject}" for step in p.steps)))
    return "\n\n".join(parts)


async def _task(engine: Engine, entry: Entry) -> None:
    number = _number(entry.arguments)
    if number is None:
        await engine.notify(None, "Give me the task number: /task 12")
        return
    task = await engine.tasks.get(number)
    if task is None:
        await engine.notify(None, f"Task {number} doesn't exist.")
        return
    files = await engine.tasks.files(number, "result")
    parts = [
        await progress_text(engine, task),
        bold("Request") + "\n" + esc(truncate(task.request, 800)),
    ]
    if task.summary and task.state == TaskState.COMPLETED:
        parts.append(bold("Summary") + "\n" + esc(truncate(task.summary, 800)))
    if task.error:
        parts.append(bold("Error") + "\n" + esc(truncate(task.error, 400)))
    if files:
        parts.append(esc("Files: " + ", ".join(a["path"].rsplit("/", 1)[-1] for a in files)))
    if task.final_folder:
        parts.append(esc(f"Saved in {FINISHED_FOLDER}/{Path(task.final_folder).name}."))
    consumption = await engine.usage.task_consumption(number)
    consumption_text = f"Usage: {consumption.text()}"
    if consumption.total:
        consumption_text += f" ({consumption.breakdown()})"
    parts.append(esc(consumption_text + "."))
    message_id = await engine.notify(number, "\n\n".join(parts), html=True)
    if message_id:
        await engine.tasks.link_message(message_id, number, "output")


async def _choose(engine: Engine, entry: Entry, states: set[TaskState], verb: str) -> int | None:
    number = _number(entry.arguments)
    if number is not None:
        return number
    candidates = await engine.tasks.list_items(states=list(states), kind="task", limit=5)
    if len(candidates) == 1:
        return candidates[0].id
    await engine.notify(None, f"Tell me which task to {verb}: /{entry.command} N")
    return None


async def _stop(engine: Engine, entry: Entry) -> None:
    active = {*IN_PROGRESS, TaskState.IN_QUEUE, TaskState.WAITING_PLAN, *PAUSED}
    number = await _choose(engine, entry, active, "stop")
    if number is not None and not await engine.cancel_task(
        number, "canceled by the user with /stop"
    ):
        await engine.notify(None, f"Task {number} isn't running.")


async def _pause(engine: Engine, entry: Entry) -> None:
    number = await _choose(engine, entry, {*IN_PROGRESS, TaskState.IN_QUEUE}, "pause")
    if number is not None and not await engine.pause_task(number):
        await engine.notify(None, f"Task {number} can't be paused right now.")


async def _resume(engine: Engine, entry: Entry) -> None:
    number = await _choose(engine, entry, set(PAUSED), "resume")
    if number is None:
        return
    if await engine.resume_task(number):
        await engine.notify(number, f"Resuming task {number}.")
    else:
        await engine.notify(None, f"Task {number} isn't paused.")


async def _plan(engine: Engine, entry: Entry) -> None:
    text = entry.arguments.strip()
    if not text:
        await engine.notify(None, "Write the request after the command: /plan text")
        return
    size = "medium"
    actions = False
    browser = False
    if engine.triage is not None:
        triage = await engine.triage(
            text, await engine.tasks.list_items(limit=8, kind="task"), None
        )
        size, actions, browser = (
            triage.size,
            triage.external_actions,
            triage.needs_real_browser,
        )
    task = await engine.create_direct_task(
        text,
        origin="telegram",
        size=size,
        needs_plan=True,
        external_actions=actions,
        needs_browser=browser,
    )
    if entry.message_id:
        await engine.tasks.link_message(entry.message_id, task.id, "entry")
    await engine.notify(task.id, f"Preparing a plan for task {task.id}.")


async def _model(engine: Engine, name: str) -> None:
    engine.next_model = name
    await engine.db.kv_put("next_model", name)
    role = engine.config.models.forceable[name]
    await engine.notify(None, f"The next task will use {role.id}.")


async def _opus(engine: Engine, entry: Entry) -> None:
    await _model(engine, "opus")


async def _sonnet(engine: Engine, entry: Entry) -> None:
    await _model(engine, "sonnet")


async def _maximum(engine: Engine, entry: Entry) -> None:
    role = engine.config.models.forceable["maximum"]
    text = (
        f"Max mode uses {role.id} for the next task. It uses up the plan limits faster "
        "(at most half of the weekly limit) and, depending on your plan, may need extra usage "
        "credits; if so, the request isn't sent and I'll let you know. Turn it on for the next task?"
    )
    buttons = [[Button("Use max mode", "max:yes"), Button("Cancel", "max:no")]]
    await engine.notify(None, text, buttons=buttons)


async def _usage(engine: Engine, entry: Entry) -> None:
    zone = engine.config.time_zone
    await engine.refresh_limits()  # if the saved data is old, new data is requested
    parts = [bold("Plan limits") + "\n" + esc(await engine.usage.limits_text(zone))]
    today = await engine.usage.consumption_today(zone)
    rows = await engine.usage.consumption_by_tasks(datetime.now(UTC) - timedelta(days=7), limit=15)
    consumption = [f"Agent usage today: {today.text()}."]
    if rows:
        consumption.append("")
        consumption += [f"{f['task_id']}. {f['name']} · {f['tokens_text']}" for f in rows]
    parts.append(bold("Usage per task, last 7 days") + "\n" + esc("\n".join(consumption)))
    parts.append(
        esc(
            "The percentages cover your whole Claude account, so they also include what "
            "you use outside the agent (Claude Code or claude.ai)."
        )
    )
    await engine.notify(None, "\n\n".join(parts), html=True)
    await _consumption_chart(engine, 14, "Agent usage by day and model, last 14 days.")


async def _consumption_chart(engine: Engine, days: int, caption: str) -> None:
    """The usage chart, made by code. If it fails, the text has already arrived."""
    from aide.core.charts import consumption_chart

    try:
        path = await consumption_chart(engine.core, days)
        if path is not None and engine.channel is not None:
            await engine.channel.send_image(path, caption)
    except Exception:
        log.exception("could not make the usage chart")


async def _memory(engine: Engine, entry: Entry) -> None:
    facts = engine.memory.facts()
    proposals = await engine.memory.pending_proposals()
    if not facts:
        text = esc("The memory is empty.")
    else:
        lines = [esc(f"{n}. {h.text}") for n, h in enumerate(facts, 1)]
        text = bold("Memory") + "\n" + "\n".join(lines)
    if proposals:
        text += "\n\n" + esc(f"Proposals waiting for your decision: {len(proposals)}.")
    if len(text) > engine.config.settings.telegram.text_limit and engine.channel is not None:
        await engine.channel.send_file(engine.config.paths.memory, "Full memory")
        return
    await engine.notify(None, text, html=True)


async def _forgets(engine: Engine, entry: Entry) -> None:
    text = entry.arguments.strip()
    if not text:
        await engine.notify(None, "Write which fact to delete: /forget text")
        return
    matches = engine.memory.matches(text)
    if not matches:
        await engine.notify(None, "I can't find any fact with that text in the memory.")
        return
    buttons = [
        [Button(truncate(f"Forget: {h.text}", 60), f"forget:{h.id}:yes")] for h in matches[:8]
    ]
    buttons.append([Button("Cancel", "olv:-:no")])
    question = (
        "Which fact should I delete?"
        if len(matches) > 1
        else f"Delete this fact?\n“{matches[0].text}”"
    )
    await engine.notify(None, question, buttons=buttons)


async def _schedule(engine: Engine, entry: Entry) -> None:
    text = entry.arguments.strip()
    if not text:
        await engine.notify(
            None,
            "Write what and when: /schedule every Monday at 9 check whether any company has replied",
        )
        return
    request = (
        "Schedule this task with the schedule_task tool (work out when and how often; "
        f"if anything is missing, ask me): {text}"
    )
    task = await engine.create_direct_task(request, origin="telegram", size="small")
    if entry.message_id:
        await engine.tasks.link_message(entry.message_id, task.id, "entry")


async def _scheduled_tasks(engine: Engine, entry: Entry) -> None:
    rows = await engine.db.everything(
        "SELECT * FROM scheduled_tasks ORDER BY active DESC, id DESC LIMIT 30"
    )
    if not rows:
        await engine.notify(None, "No scheduled tasks.")
        return
    zone = ZoneInfo(engine.config.time_zone)
    lines, buttons = [], []
    for f in rows:
        timing = f["timing_text"] or f["timing"]
        kind = f.get("kind") or "model"
        if f.get("pending"):
            state = "waiting for your approval"
        elif f["active"] and f["next_run"]:
            next_run = datetime.fromisoformat(f["next_run"]).astimezone(zone)
            state = f"next: {next_run:%b} {next_run.day} {next_run:%H:%M}"
        else:
            state = "finished" if not f["active"] else "no date"
        if f["active"] and f["next_run"] and kind != "maintenance":
            buttons.append(
                [
                    Button(
                        truncate(f"Cancel {f['id']}: {f['description']}", 60),
                        f"schedask:{f['id']}:cancel",
                    )
                ]
            )
        last = f" · last: {f['last_run'][:16].replace('T', ' ')}" if f["last_run"] else ""
        mark = {"script": " (script, no model)", "maintenance": " (system)"}.get(kind, "")
        lines.append(
            esc(f"{f['id']}. {truncate(f['description'], 80)}{mark} · {timing} · {state}{last}")
        )
    await engine.notify(
        None,
        bold("Scheduled tasks") + "\n" + "\n".join(lines),
        buttons=buttons[:8] or None,
        html=True,
    )


async def _profile(engine: Engine, entry: Entry) -> None:
    path = engine.config.paths.profile
    if not path.exists():
        await engine.notify(None, "There's no profile. Create agent/profile.md from the template.")
        return
    content = path.read_text(encoding="utf-8")
    if len(content) > 3000 and engine.channel is not None:
        await engine.channel.send_file(path, "Your profile")
        return
    await engine.notify(None, f"<pre>{esc(content)}</pre>", html=True)


async def _panel(engine: Engine, entry: Entry) -> None:
    link = engine.core.services.get("panel_link")
    url = (
        link()
        if callable(link)
        else f"http://{engine.config.panel_host}:{engine.config.panel_port}"
    )
    text = f"Panel on this Mac: {url}"
    mobile = engine.core.services.get("link_panel_mobile")
    url_mobile = mobile() if callable(mobile) else None
    if url_mobile:
        # Through Tailscale, only inside the user's network and with Funnel off.
        text += f"\n\nOn the iPhone, with Tailscale connected: {url_mobile}"
    await engine.notify(None, text)


async def _google(engine: Engine, entry: Entry) -> None:
    """The link to renew Google authorization now, for example before a few days without
    wifi, so it does not expire in the middle. Once renewed, it lasts another 7 days."""
    auth = engine.core.services.get("google")
    link = engine.core.services.get("google_link")
    if auth is None or not callable(link):
        await engine.notify(None, "Google isn't set up in this agent.")
        return
    expires = await asyncio.to_thread(auth.expires_in)
    text = (
        "To renew it now (it lasts another 7 days), open this link on the Mac and sign in with your "
        f"Google account:\n{link()}"
    )
    if expires is not None:
        local = expires.astimezone(ZoneInfo(engine.config.time_zone))
        text = (
            f"The Google authorization expires on {local:%b} {local.day} at {local:%H:%M}. " + text
        )
    else:
        text = "Google isn't authorized. " + text.replace(
            "To renew it now (it lasts another 7 days)", "To authorize it"
        )
    await engine.notify(None, text)


async def _undo_improvement(engine: Engine, entry: Entry) -> None:
    number = _number(entry.arguments)
    if number is None:
        await engine.notify(None, "Give me the improvement number: /undo_improvement 3")
        return
    from aide.maintenance import improvements

    await engine.notify(None, await improvements.undo(engine, number))


async def _documents(engine: Engine, entry: Entry) -> None:
    """The document inbox: how many new ones there are and how much reading them will
    cost."""
    from aide.paperwork import inbox

    await inbox.ask(engine)


async def _button(engine: Engine, datum: str) -> None:
    parts = datum.split(":")
    kind = parts[0]
    if (
        kind == "doc"
        and len(parts) == 3
        and parts[1] in ("sav", "cor", "no")
        and parts[2].isdigit()
    ):
        # “Save”, “Correct” or “Discard” on a document.
        from aide.paperwork import flow as paperwork_flow

        await paperwork_flow.button(engine, parts[1], int(parts[2]))
        return
    if (
        kind == "inbox"
        and len(parts) == 3
        and parts[1] in ("read", "nothing", "sav", "rev", "des")
        and parts[2].isdigit()
    ):
        # The document inbox.
        from aide.paperwork import inbox

        await inbox.button(engine, parts[1], int(parts[2]))
        return
    if kind == "pending" and len(parts) == 3 and parts[1] in ("doc", "other", "com"):
        # A PDF or a photo that arrived without a caption, or a meal.
        await engine.files_button(parts[1], parts[2])
        return
    if kind in ("health", "gym", "meal", "mealtype"):
        # Health: quick habits, profile, doubtful exercises and meals.
        from aide.health import telegram as health

        if await health.button(engine, datum):
            return
    if kind == "email" and len(parts) == 3 and parts[1] in ("imp", "sil"):
        # “Important” or “Don't notify me about this” on an email alert.
        from aide.email import review

        await review.notice_button(engine, parts[1], parts[2])
        return
    if kind == "max":
        if len(parts) > 1 and parts[1] == "yes":
            await _model(engine, "maximum")
        else:
            await engine.notify(None, "Max mode canceled.")
    elif kind == "forget" and len(parts) == 3:
        if parts[2] != "yes":
            await engine.notify(None, "I didn't delete anything.")
            return
        done = await engine.memory.forget(parts[1])
        await engine.notify(
            None,
            f"Forgotten: “{done.text}”" if done else "That fact was no longer in the memory.",
        )
    elif kind == "schedask" and len(parts) == 3 and parts[2] == "cancel":
        row = await engine.db.one("SELECT * FROM scheduled_tasks WHERE id=?", (int(parts[1]),))
        if row is None or not row["active"]:
            await engine.notify(None, "That scheduled task is no longer active.")
            return
        await engine.db.run("UPDATE scheduled_tasks SET active=0 WHERE id=?", (row["id"],))
        await engine.notify(
            None, f"Scheduled task {row['id']} canceled: “{truncate(row['description'], 80)}”."
        )
    elif kind == "improvement" and len(parts) == 3 and parts[1].isdigit():
        from aide.maintenance import improvements

        action = improvements.apply if parts[2] == "apply" else improvements.discard
        await engine.notify(None, await action(engine, int(parts[1])))
    elif kind == "security" and len(parts) == 3 and parts[1] == "approve":
        from aide.maintenance import security

        if await security.approve_fingerprints(engine, parts[2]):
            await engine.notify(
                None, "Security changes approved: I'll use them as the reference from now on."
            )
        else:
            await engine.notify(
                None,
                "Those files have changed again since the report, so I'm not approving anything. "
                "You'll see it in the next report.",
            )
    elif kind == "sonnet" and len(parts) >= 2:
        from aide.maintenance import consumption

        if parts[1] == "yes" and len(parts) == 3 and parts[2].isdigit():
            await engine.notify(None, await consumption.use_sonnet_until(engine, int(parts[2])))
        else:
            await engine.notify(None, "Keeping things as they are: no model changes.")
    elif kind == "mem" and len(parts) == 3:
        done = await engine.memory.resolve_proposal(int(parts[1]), parts[2] == "yes")
        if done is None:
            await engine.notify(None, "That proposal was already settled.")
        else:
            await engine.notify(
                None, f"Saved to memory: “{done}”" if parts[2] == "yes" else "Discarded."
            )
    else:
        log.warning("unknown button: %s", datum)


HANDLERS = {
    "start": _start,
    "help": _help,
    "status": _state_cmd,
    "tasks": _tasks,
    "task": _task,
    "stop": _stop,
    "pause": _pause,
    "resume": _resume,
    "plan": _plan,
    "opus": _opus,
    "sonnet": _sonnet,
    "max": _maximum,
    "usage": _usage,
    "memory": _memory,
    "forget": _forgets,
    "schedule": _schedule,
    "scheduled": _scheduled_tasks,
    "profile": _profile,
    "panel": _panel,
    "google": _google,
    "undo_improvement": _undo_improvement,
    "documents": _documents,
    # Health: by code, no tasks or model.
    **HEALTH_HANDLERS,
}
