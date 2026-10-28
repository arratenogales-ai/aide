"""Running the weekly maintenance and the single report the user receives through Telegram.

The code does all the checks. The model (role “maintenance”) only receives the facts
already checked and the week's signals, without secrets, and returns the report text and
the improvement proposals. What fails is put at the start, with what to do, from the
code: it does not depend on the model including it.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import ClaudeAgentOptions

from aide.bot.channel import Button
from aide.bot.formatting import bold, esc
from aide.core import scheduler
from aide.core.db import now
from aide.core.scheduler import short_date
from aide.maintenance import (
    backups,
    cleanup,
    consumption,
    improvements,
    security,
    updates,
)
from aide.maintenance.section import Section

if TYPE_CHECKING:
    from aide.core.engine import Engine

log = logging.getLogger(__name__)

MESSAGE_LIMIT = 3900
# “Your week” does not exceed about 900 letters, and the model's text keeps at least 300 if
# they fit: what does not fit is trimmed before sending, so Telegram does not cut off the end.
MAX_WEEK = 900
# “Your health”, the same with about 500.
MAX_HEALTH = 500
MIN_MODEL = 300
GOOGLE_NOTICE = timedelta(hours=48)
# The title of each part, in case its check fails before giving its own.
TITLES = {
    "security": "Security",
    "consumption": "Usage",
    "cleanup": "Cleanup",
    "backup": "Backup",
    "updates": "Updates",
    "google": "Google",
}

SCHEMA = {
    "type": "object",
    "properties": {
        "report": {"type": "string"},
        "improvements": {
            "type": "array",
            "maxItems": 5,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["A", "B"]},
                    "title": {"type": "string"},
                    "reason": {"type": "string"},
                    "change": {
                        "type": "object",
                        "properties": {
                            "file": {"type": "string"},
                            "operation": {
                                "type": "string",
                                "enum": ["add", "replace", "create", "describe"],
                            },
                            "search": {"type": "string"},
                            "text": {"type": "string"},
                        },
                        "required": ["file", "operation", "text"],
                    },
                },
                "required": ["kind", "title", "reason", "change"],
            },
        },
    },
    "required": ["report", "improvements"],
}

SYSTEM = """You write the weekly maintenance report for Aide, the user's personal agent. You receive the facts already checked by code and the signals from the week: do not make up data or checks.

report: a short text (1500 characters at most), addressing the user as "you", warm and direct, in English, with no em dashes and no "Label: explanation" constructions. One or two sentences per section, in this order: security, usage, cleanup, backup, updates and Google. Failures are already highlighted separately at the top of the message, with what to do: here, mention them in one sentence only. Plain text, no Markdown.

improvements: at most 5 concrete improvements drawn from the week's signals, each with its reason (what happened and in which task). If no signal justifies one, return an empty list. Types:
- A, no code: only these files. agent/CLAUDE.md (operation "add" with the exact text of one or two lines, or "replace" with search = exact text that already exists), agent/.claude/skills/<name>/SKILL.md (operation "create", with a --- name and description --- header and the instructions), config/models.yaml (operation "replace" of one exact line) or memory (operation "add" with a one-line fact). 600 characters of text at most. Write the text in English.
- B, code or security: anything in src/, permissions.yaml, settings.yaml, hooks, Google scopes or limits. operation "describe" and, in text, the change to ask Claude Code for.
Never propose weakening the agent's security, approvals or limits."""


async def review_google(engine: Engine) -> Section:
    """Checks when the Google authorization expires and returns a report section."""
    s = Section("google", "Google")
    auth = engine.core.services.get("google")
    if auth is None:
        s.lines.append("Google is not set up.")
        return s
    expires = await asyncio.to_thread(auth.expires_in)
    if expires is None:
        s.lines.append("It is not known when the Google authorization expires.")
        return s
    local = expires.astimezone(ZoneInfo(engine.config.time_zone))
    if expires - datetime.now(UTC) < GOOGLE_NOTICE:
        s.failure(
            f"The Google authorization expires on {short_date(local)} at {local:%H:%M}.",
            "Renew it before then with the link in this message (open it on the Mac).",
        )
        s.data["renew"] = True
    else:
        s.lines.append(
            f"The Google authorization is valid until {short_date(local)} at {local:%H:%M}."
        )
    return s


async def draft(engine: Engine, data: dict[str, Any]) -> dict[str, Any]:
    """A single request to the model of the “maintenance” role with structured output."""
    from aide.core.base_session import sdk_environment, sdk_settings
    from aide.core.context import SessionContext
    from aide.core.orchestrator import AgentSession

    config = engine.config
    role = config.role("maintenance")
    options = ClaudeAgentOptions(
        model=role.id,
        effort=role.effort,
        system_prompt=SYSTEM,
        cwd=str(config.paths.agent),
        setting_sources=[],
        settings=sdk_settings(config),
        tools=[],
        permission_mode="default",
        env=sdk_environment(config),
        max_turns=3,
        max_budget_usd=1.0,
        output_format={"type": "json_schema", "schema": SCHEMA},
    )
    ses = SessionContext(
        core=engine.core, task_id=None, folder=None, role="maintenance", phase="maintenance"
    )
    async with AgentSession(ses, options, "maintenance") as session:
        result = await session.turn(json.dumps(data, ensure_ascii=False, indent=1))
    if result.kind != "ok" or not isinstance(result.structured, dict):
        raise RuntimeError(f"the model did not return the report ({result.kind})")
    return result.structured


async def _section(name: str, coroutine: Any) -> Section:
    try:
        return await coroutine
    except Exception as e:
        log.exception("error in the %s part of maintenance", name)
        title = TITLES.get(name, name.capitalize())
        s = Section(name, title)
        s.failure(
            f"The {title.lower()} check failed ({type(e).__name__}: {str(e)[:150]}).",
            "Check the logs (Advanced, Logs, in the panel) or ask Claude Code to look into it.",
        )
        return s


def truncate(text: str, maximum: int) -> str:
    """The text, cut at a word with “…” if, already escaped for HTML, it does not fit in
    `maximum`. It is cut before escaping so as not to split an entity (“&amp;”) in half."""
    if len(esc(text)) <= maximum:
        return text
    cutoff = maximum - 1
    while cutoff > 0:
        chunk = text[:cutoff].rsplit(" ", 1)[0].rstrip() + "…"
        if len(esc(chunk)) <= maximum:
            return chunk
        cutoff -= max(1, len(esc(chunk)) - maximum)
    return ""


def truncate_lines(text: str, maximum: int) -> str:
    """The first whole lines that fit in `maximum`, with “…” if others are missing."""
    output: list[str] = []
    used = 0
    for line in text.splitlines():
        if used + len(line) + 1 > maximum:
            output.append("…")
            break
        output.append(line)
        used += len(line) + 1
    return "\n".join(output)


def compose(
    sections: list[Section],
    model_text: str,
    proposals: list[dict[str, Any]],
    moment: datetime,
    test: bool,
    google_link: str | None,
    week: str = "",
    health: str = "",
) -> tuple[str, list[list[Button]]]:
    """Builds the weekly maintenance report text and its buttons from the sections."""
    header = bold("Weekly maintenance") + esc(
        f", {short_date(moment)}"
        + (" (test: the cleanup only lists what it would delete)" if test else "")
    )
    parts = [header]
    failures = [s for s in sections if not s.ok]
    if failures:
        lines = []
        for s in failures:
            facts = [line for line in s.lines if line]
            lines.append(f"- {s.title}. " + " ".join(facts[-2:]))
            lines += [f"  → {q}" for q in s.that_do]
        parts.append(bold("⚠️ Needs your attention") + "\n" + esc("\n".join(lines)))
    if google_link:
        # At the top, next to the alerts: it is what cannot be lost if the message is long.
        parts.append(esc(f"To renew Google, open this link on the Mac: {google_link}"))
    if week:
        # Your week, made by code: what went well and what to improve.
        parts.append(bold("Your week") + "\n" + esc(truncate_lines(week, MAX_WEEK)))
    if health:
        # Your health, by code and after the model, which does not see it.
        parts.append(bold("Your health") + "\n" + esc(truncate_lines(health, MAX_HEALTH)))
    buttons: list[list[Button]] = [row for s in sections for row in s.buttons]
    improvements_lines: list[str] = []
    for p in proposals:
        line = f"{p['id']}. {p['title']}. {p['reason']}"
        if p["kind"] == "A":
            change = p.get("change") or {}
            line += f' Change in {change.get("file")} → "{change.get("text", "")}"'
            buttons.append(
                [
                    Button(f"Apply {p['id']}", f"improvement:{p['id']}:apply"),
                    Button(f"Discard {p['id']}", f"improvement:{p['id']}:discard"),
                ]
            )
        else:
            line += (
                f" Needs code: I saved it in {p.get('b_file', 'improvements/')} for Claude Code."
            )
            if p.get("note"):
                line += f" {p['note']}"
        improvements_lines.append(line)
    improvements_title = bold("Suggested improvements") + "\n"
    improvements = "\n".join(improvements_lines)
    # What remains for the model's text and the proposals, which go at the end.
    rest = MESSAGE_LIMIT - sum(len(p) + 2 for p in parts)
    long_improvements = len(improvements_title) + len(esc(improvements)) + 2 if proposals else 0
    model = truncate(model_text.strip(), max(min(MIN_MODEL, rest), rest - long_improvements) - 2)
    rest -= len(esc(model)) + 2
    final = list(parts)
    if model:
        final.append(esc(model))
    if proposals:
        improvements = truncate(improvements, rest - len(improvements_title) - 2)
        if improvements:
            final.append(improvements_title + esc(improvements))
    return "\n\n".join(final), buttons


async def run(engine: Engine, test: bool = False) -> str:
    """Runs the full maintenance and sends the report. Returns the text sent."""
    moment = datetime.now(UTC)
    sections = [
        await _section("security", security.review(engine)),
        await _section("consumption", consumption.review(engine, moment)),
        await _section("cleanup", cleanup.clean(engine, test)),
        await _section("backup", backups.make_backup(engine)),
        await _section("updates", updates.review(engine)),
        await _section("google", review_google(engine)),
    ]
    signals = await improvements.signals(engine, moment)
    data = {
        "sections": [s.for_model() for s in sections],
        "week_signals": signals,
    }
    try:
        drafted = await draft(engine, data)
        model_text = str(drafted.get("report") or "")
        proposals = await improvements.save_proposals(
            engine, list(drafted.get("improvements") or []), moment
        )
    except Exception:
        log.exception("the model could not write the report; sending the facts as they are")
        model_text = "\n".join(f"{s.title}. " + " ".join(s.lines) for s in sections if s.lines)
        proposals = []
    for p in proposals:
        row = await engine.db.one("SELECT file, change FROM improvements WHERE id=?", (p["id"],))
        p["change"] = json.loads(row["change"] or "{}") if row else {}
    link = None
    if any(s.data.get("renew") for s in sections):
        create_link = engine.core.services.get("panel_link")
        link = create_link("/google/authorize") if callable(create_link) else None
    local = moment.astimezone(ZoneInfo(engine.config.time_zone))
    try:
        from aide import life

        week = life.week_text(await life.week(engine.core, moment))
    except Exception:
        log.exception("could not count up the week")
        week = ""
    try:
        # The week's health content: by code, and the model does not see it.
        from aide.health import summary

        photos = sum(int(s.data.get("health_photos") or 0) for s in sections)
        health = await summary.report_text(engine, moment, photos, test)
    except Exception:
        log.exception("could not count up the health part of the week")
        health = ""
    message, buttons = compose(sections, model_text, proposals, local, test, link, week, health)
    sent = await engine.notify(None, message, html=True, buttons=buttons or None)
    if not sent and (engine.channel is None or not engine.connection.in_line):
        # Offline (or without Telegram yet, at startup) it did not go out: it is repeated
        # the next time there is one, with the approval of the security changes if needed
        # .
        from aide.core.engine import PENDING_MAINTENANCE_KEY

        await engine.db.kv_put(PENDING_MAINTENANCE_KEY, now())
        log.info(
            "the maintenance report was not sent (no connection): it will be sent when it is back"
        )
        return message
    try:
        # The usage chart for the week, made by code.
        from aide.core.charts import consumption_chart

        chart = await consumption_chart(engine.core, 7, moment)
        if chart is not None and engine.channel is not None:
            await engine.channel.send_image(chart, "The agent's usage this week, by day and model.")
    except Exception:
        log.exception("could not make the chart of this week's usage")
    await engine.db.kv_put("last_maintenance", now())
    await engine.tasks.event(
        None,
        "maintenance",
        f"report sent ({sum(1 for s in sections if not s.ok)} warnings, {len(proposals)} improvements)",
        {"test": test, "failures": [s.key for s in sections if not s.ok]},
    )
    return message


async def ensure_scheduled_task(engine: Engine) -> None:
    """A single scheduled maintenance task, according to settings.yaml."""
    settings = engine.config.settings.maintenance
    zone = ZoneInfo(engine.config.time_zone)
    row = await engine.db.one("SELECT * FROM scheduled_tasks WHERE kind='maintenance'")
    if not settings.active:
        if row is not None and row["active"]:
            await engine.db.run("UPDATE scheduled_tasks SET active=0 WHERE id=?", (row["id"],))
        return
    timing = scheduler.interpret(
        {"kind": "weekly", "days": [settings.day], "hour": settings.hour}, zone
    )
    text = scheduler.describe(timing)
    description = (
        "Weekly maintenance (security, usage, improvements, cleanup, backup, updates and Google)"
    )
    if row is not None and row["timing"] == timing.to_json() and row["active"]:
        if row["timing_text"] != text or row["description"] != description:
            # Only the texts (e.g. the earlier ones, in Spanish): the next run is not
            # touched.
            await engine.db.run(
                "UPDATE scheduled_tasks SET timing_text=?, description=? WHERE id=?",
                (text, description, row["id"]),
            )
        return
    next_run = scheduler.next_step(timing, datetime.now(zone), zone)
    next_iso = next_run.astimezone(UTC).isoformat(timespec="seconds") if next_run else None
    if row is None:
        await engine.db.run(
            "INSERT INTO scheduled_tasks(description, timing, policy, active, created, next_run, "
            "timing_text, kind) VALUES (?, ?, 'run_always', 1, ?, ?, ?, 'maintenance')",
            (
                description,
                timing.to_json(),
                now(),
                next_iso,
                text,
            ),
        )
    else:
        await engine.db.run(
            "UPDATE scheduled_tasks SET timing=?, timing_text=?, next_run=?, active=1, "
            "policy='run_always' WHERE id=?",
            (timing.to_json(), text, next_iso, row["id"]),
        )
