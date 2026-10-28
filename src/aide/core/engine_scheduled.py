"""Part of the engine: scheduled tasks.

Those that fall due, script ones, weekly maintenance and approval of those requested from
the terminal.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aide.bot.formatting import truncate
from aide.common.config import Cap
from aide.core import scheduler, scripts
from aide.core.db import now
from aide.core.engine_common import PENDING_MAINTENANCE_KEY, log, short_day

# These days without wifi, a scheduled task that arrives later than this (Mac asleep) is skipped.
NO_WIFI_DELAY = timedelta(minutes=10)


def _date_time(local: datetime) -> str:
    """“Oct 28, 2026 14:05”, as in the scheduler tools."""
    return f"{short_day(local)}, {local.year} {local:%H:%M}"


def _scheduled_task_caps(text: str | None) -> Cap | None:
    """Fixed caps of a scheduled task (`{"turns": 80, "usd": 4.0}`); without them or if invalid,
    those of its size."""
    if not text:
        return None
    try:
        return Cap.model_validate(json.loads(text))
    except (ValueError, TypeError):
        log.warning("invalid caps in a scheduled task: %s", text)
        return None


class ScheduledMixin:
    """Launch, skip and approve scheduled tasks."""

    async def check_scheduled_tasks(self) -> list[int]:
        """Launches due scheduled tasks or skips them according to their policy."""
        zone = ZoneInfo(self.config.time_zone)
        moment = datetime.now(UTC)
        rows = await self.db.everything(
            "SELECT * FROM scheduled_tasks WHERE active=1 AND next_run IS NOT NULL AND next_run <= ? "
            "ORDER BY next_run",
            (moment.isoformat(timespec="seconds"),),
        )
        launched = []
        window = timedelta(hours=self.config.settings.scheduler.window_hours)
        for row in rows:
            expected = datetime.fromisoformat(row["next_run"])
            timing = scheduler.interpret(row["timing"], zone)
            new = scheduler.next_step(timing, moment, zone)
            next_run = new.astimezone(UTC).isoformat(timespec="seconds") if new else None
            kind = row.get("kind") or "model"
            afternoon = self.config.settings.no_wifi.active and moment - expected > NO_WIFI_DELAY
            if kind != "maintenance" and (not self.connection.in_line or afternoon):
                # With no connection (or, these days without wifi, one that arrives
                # late because the Mac was asleep) it isn't run: it is skipped
                # without notice and not run when the connection returns.
                # Maintenance, however, waits for the next one.
                await self.db.run(
                    "UPDATE scheduled_tasks SET next_run=?, active=? WHERE id=?",
                    (next_run, int(next_run is not None), row["id"]),
                )
                if not self.connection.in_line:
                    reason = "no connection"
                else:
                    reason = "arrived late during the no-wifi days"
                await self.tasks.event(
                    None,
                    "scheduled_task_skipped",
                    f"scheduled task {row['id']}: {reason}",
                    {"scheduled_task_id": row["id"], "expected": row["next_run"], "reason": reason},
                )
                continue
            if scheduler.run_with_delay(row["policy"], moment - expected, window):
                if kind in ("script", "maintenance"):
                    # No model (script) or system maintenance: no task is
                    # created.
                    await self.db.run(
                        "UPDATE scheduled_tasks SET last_run=?, next_run=?, active=? WHERE id=?",
                        (now(), next_run, int(next_run is not None), row["id"]),
                    )
                    if kind == "script":
                        self._launch(self.run_script(row))
                    else:
                        self._launch(self.run_maintenance())
                    continue
                local = expected.astimezone(zone)
                request = (
                    f"Scheduled task {row['id']} ({row['timing_text'] or 'scheduled'}), "
                    f"run of {_date_time(local)}:\n{row['description']}"
                )
                # A scheduled task can set the size and caps of its task;
                # otherwise, triage decides.
                size = row.get("size")
                if size not in ("small", "medium", "large"):
                    size = await self._scheduled_task_size(request)
                task = await self.create_direct_task(
                    request,
                    origin="scheduled_task",
                    size=size,
                    caps=_scheduled_task_caps(row.get("caps")),
                    scheduled_task_id=row["id"],
                )
                await self.db.run(
                    "UPDATE scheduled_tasks SET last_run=?, last_task=?, next_run=?, active=? "
                    "WHERE id=?",
                    (now(), task.id, next_run, int(next_run is not None), row["id"]),
                )
                launched.append(task.id)
            else:
                await self.db.run(
                    "UPDATE scheduled_tasks SET next_run=?, active=? WHERE id=?",
                    (next_run, int(next_run is not None), row["id"]),
                )
                local = expected.astimezone(zone)
                await self.notify(
                    None,
                    f'Scheduled task {row["id"]} ("{truncate(row["description"], 80)}") was due '
                    f"on {short_day(local)} at {local:%H:%M} while the Mac was off, so it was "
                    "skipped, as its policy says.",
                )
        return launched

    async def run_script(self, row: dict[str, Any]) -> scripts.ScriptResult | None:
        """Runs a script-type scheduled task (no model) and notifies if there is anything to report."""
        try:
            command = scripts.validate(
                self.config.paths, row["project"], json.loads(row["command"] or "[]")
            )
            r = await scripts.run(self.config.paths, command, self.config.time_zone)
        except Exception as e:
            log.exception("couldn't run the script of scheduled task %s", row["id"])
            await self.notify(None, f"Scheduled task {row['id']} couldn't run: {e}")
            return None
        message = scripts.message_for_user(row["id"], command, row["silence"] or "", r)
        await self.tasks.event(
            None,
            "script",
            f"scheduled task {row['id']} ({command.project}): "
            + ("timed out" if r.exhausted else f"exit code {r.code}"),
            {
                "scheduled_task_id": row["id"],
                "code": r.code,
                "seconds": round(r.seconds, 2),
                "output": r.output[:500],
                "notice": message is not None,
            },
        )
        if message is not None:
            await self.notify(None, message)
        return r

    async def run_maintenance(self, test: bool = False) -> None:
        """Runs maintenance, deferring it until the connection is back if offline."""
        if self.maintenance is None:
            log.warning("maintenance isn't set up")
            return
        if self._maintenance_in_progress:
            return
        self._maintenance_in_progress = True
        try:
            if not await self.connection.check(force=True):
                # With no connection it runs the next time there is one, with its
                # report and, if needed, approval of the security changes.
                if not await self.db.kv_read(PENDING_MAINTENANCE_KEY):
                    await self.db.kv_put(PENDING_MAINTENANCE_KEY, now())
                log.info("maintenance offline: it will run when the connection is back")
                return
            await self.db.kv_delete(PENDING_MAINTENANCE_KEY)
            # A test run is requested from outside by leaving this marker (cleanup only
            # lists).
            if await self.db.kv_read("test_maintenance"):
                test = True
                await self.db.kv_put("test_maintenance", None)
            try:
                await self.maintenance(self, test)
            except Exception:
                log.exception("error in the weekly maintenance")
                await self.notify(
                    None,
                    "The weekly maintenance failed before finishing. Check the log "
                    "(Advanced, Logs, in the panel).",
                )
        finally:
            self._maintenance_in_progress = False

    async def check_pending_scripts(self) -> None:
        """Asks the user to approve scheduled tasks requested from the terminal
        (script or model-based)."""
        rows = await self.db.everything(
            "SELECT * FROM scheduled_tasks WHERE kind IN ('script', 'model') AND pending=1 ORDER BY id"
        )
        for row in rows:
            if row["id"] not in self._requesting_script:
                self._requesting_script.add(row["id"])
                if row["kind"] == "script":
                    self._launch(self._request_script_approval(row))
                else:
                    self._launch(self._request_model_approval(row))

    async def _request_script_approval(self, row: dict[str, Any]) -> None:
        from aide.core.approvals import Request

        try:
            command = scripts.validate(
                self.config.paths, row["project"], json.loads(row["command"] or "[]")
            )
            entry = {
                "scheduled_task": row["id"],
                "project": command.project,
                "command": list(command.argv),
                "timing": row["timing"],
                "silence": row["silence"] or "",
            }
            detail = (
                f"Schedule a script task (no model)\n{scripts.describe(command, row['silence'] or '')}"
                f"\nWhen: {row['timing_text'] or row['timing']}"
            )
            resolution = await self.approvals.request(
                Request(None, "mcp__aide__schedule_script", entry, "scheduled_task", detail)
            )
            await self._resolve_pending_scheduled_task(row, resolution.approved, "script")
        except Exception:
            log.exception("error asking for approval of script %s", row["id"])
        finally:
            self._requesting_script.discard(row["id"])

    async def _request_model_approval(self, row: dict[str, Any]) -> None:
        """Model-based scheduled task requested from the terminal: activated only if the user approves it."""
        from aide.core.approvals import Request

        try:
            zone = ZoneInfo(self.config.time_zone)
            timing = scheduler.interpret(row["timing"], zone)
            next_run = scheduler.next_step(timing, datetime.now(UTC), zone)
            entry = {
                "scheduled_task": row["id"],
                "description": row["description"],
                "timing": row["timing"],
                "policy": row["policy"],
            }
            lines = [
                "Schedule a task",
                f"Task: {row['description']}",
                f"When: {row['timing_text'] or scheduler.describe(timing)}",
                f"Next run: {_date_time(next_run.astimezone(zone))}"
                if next_run
                else "Next run: none",
                "If the Mac is off at that time: "
                + scheduler.describe_policy(
                    row["policy"], self.config.settings.scheduler.window_hours
                ),
            ]
            resolution = await self.approvals.request(
                Request(None, "mcp__aide__schedule_task", entry, "scheduled_task", "\n".join(lines))
            )
            await self._resolve_pending_scheduled_task(row, resolution.approved, "")
        except Exception:
            log.exception("error asking for approval of scheduled task %s", row["id"])
        finally:
            self._requesting_script.discard(row["id"])

    async def _resolve_pending_scheduled_task(
        self, row: dict[str, Any], approved: bool, kind: str
    ) -> None:
        """Activates the pending scheduled task if the user approved it; otherwise deletes it."""
        if approved:
            zone = ZoneInfo(self.config.time_zone)
            timing = scheduler.interpret(row["timing"], zone)
            new = scheduler.next_step(timing, datetime.now(UTC), zone)
            await self.db.run(
                "UPDATE scheduled_tasks SET pending=0, active=?, next_run=? WHERE id=?",
                (
                    int(new is not None),
                    new.astimezone(UTC).isoformat(timespec="seconds") if new else None,
                    row["id"],
                ),
            )
            await self.notify(None, f"Scheduled task {row['id']} activated: {row['timing_text']}.")
        else:
            await self.db.run("DELETE FROM scheduled_tasks WHERE id=?", (row["id"],))
            await self.notify(
                None,
                f"I didn't create the {kind} task."
                if kind
                else "I didn't create the scheduled task.",
            )

    async def _scheduled_task_size(self, request: str) -> str:
        """Size of a scheduled run according to triage (if it fails, medium)."""
        if self.triage is None:
            return "medium"
        try:
            triage = await self.triage(request, [], None)
        except Exception:
            log.warning("couldn't classify the scheduled task", exc_info=True)
            return "medium"
        return triage.size if triage.size in ("small", "medium", "large") else "medium"
