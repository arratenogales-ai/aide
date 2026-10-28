"""b) Usage: weekly limit, trend, projection to the reset and the tasks that spend the
most.

In addition, outside the report, immediate alerts when passing 70% and 90% of the weekly
limit (once per week each). The 90% one offers to move the orchestrator and the reviewer
to Sonnet until the reset; this is only done if the user approves it with the button.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aide.bot.channel import Button
from aide.core.db import from_json
from aide.core.scheduler import short_date
from aide.core.usage import format_tokens
from aide.maintenance.section import Section

if TYPE_CHECKING:
    from aide.core.engine import Engine

WEEK = timedelta(days=7)
THRESHOLDS = (0.70, 0.90)


def _weekly(data: dict[str, Any] | None) -> tuple[float, int] | None:
    window = ((data or {}).get("unifiedWindows") or {}).get("seven_day") or {}
    if window.get("utilization") is None or not window.get("resetsAt"):
        return None
    return float(window["utilization"]), int(window["resetsAt"])


def projection(used: float, reset: datetime, moment: datetime) -> float | None:
    """Expected usage at the reset if the pace of this week's average continues (None if it is
    early)."""
    start = reset - WEEK
    elapsed = (moment - start).total_seconds()
    if elapsed < 24 * 3600:
        return None
    return used * WEEK.total_seconds() / elapsed


async def review(engine: Engine, moment: datetime | None = None) -> Section:
    """Reviews usage against the weekly plan limit and returns a report section."""
    s = Section("consumption", "Usage")
    moment = moment or datetime.now(UTC)
    zone = ZoneInfo(engine.config.time_zone)
    row = await engine.db.one(
        "SELECT hour, data FROM limits WHERE data LIKE '%seven_day%' ORDER BY id DESC LIMIT 1"
    )
    weekly = _weekly(from_json(row["data"]) if row else None)
    if weekly is None:
        s.lines.append("There is no data on the weekly limit yet.")
    else:
        used, ts_reset = weekly
        reset = datetime.fromtimestamp(ts_reset, UTC)
        if reset <= moment:
            s.lines.append("The weekly limit has reset and there is no new data yet.")
        else:
            local = reset.astimezone(zone)
            s.lines.append(
                f"Weekly limit at {round(used * 100)}% (resets on {short_date(local)} at {local:%H:%M})."
            )
            forecast = projection(used, reset, moment)
            if forecast is not None:
                if forecast >= 0.95:
                    s.failure(
                        f"At this pace you'd reach the reset with {round(forecast * 100)}% used: "
                        "cutting it close.",
                        "Cut back on big tasks until the reset, or use /sonnet for the next ones.",
                    )
                else:
                    s.lines.append(
                        f"At this pace you'd reach the reset with {round(forecast * 100)}% used "
                        f"({100 - round(forecast * 100)}% to spare)."
                    )
            s.data["weekly"] = {"used": used, "reset": reset.isoformat()}

    # Token trend: this week against the previous one.
    this = await engine.usage.consumption_since(moment - WEEK)
    previous_total = await engine.db.value(
        "SELECT SUM(input_tokens + output_tokens + cache_read + cache_creation) FROM usage "
        "WHERE hour >= ? AND hour < ?",
        (
            (moment - 2 * WEEK).isoformat(timespec="seconds"),
            (moment - WEEK).isoformat(timespec="seconds"),
        ),
    )
    previous_total = int(previous_total or 0)
    line = f"This week the agent used {this.text()}"
    if previous_total:
        change = round(100 * (this.total - previous_total) / previous_total)
        line += f" ({'+' if change >= 0 else ''}{change}% compared with the week before)."
    else:
        line += " (there is no data from the week before to compare)."
    s.lines.append(line)

    # The 5 tasks that consumed the most tokens.
    rows = await engine.db.everything(
        "SELECT u.task_id, t.title, t.request, "
        "SUM(u.input_tokens + u.output_tokens + u.cache_read + u.cache_creation) AS tokens "
        "FROM usage u JOIN tasks t ON t.id = u.task_id WHERE u.hour >= ? "
        "GROUP BY u.task_id ORDER BY tokens DESC LIMIT 5",
        ((moment - WEEK).isoformat(timespec="seconds"),),
    )
    if rows:
        s.lines.append(
            "The ones that used the most: "
            + "; ".join(
                f'task {f["task_id"]} "{(f["title"] or f["request"] or "")[:50]}" with '
                f"{format_tokens(f['tokens'])}"
                for f in rows
            )
            + "."
        )
    return s


# --- immediate weekly limit alerts -------------------------------------------------------------


async def review_thresholds(engine: Engine, raw: dict[str, Any] | None) -> None:
    """After each limits reading: alert when passing 70% and 90% (once per week each)."""
    weekly = _weekly(raw)
    if weekly is None:
        return
    used, ts_reset = weekly
    key = f"weekly_notice:{ts_reset}"
    notified = float(await engine.db.kv_read(key, 0) or 0)
    threshold = max((u for u in THRESHOLDS if used >= u), default=None)
    if threshold is None or threshold <= notified:
        return
    await engine.db.kv_put(key, threshold)
    reset = datetime.fromtimestamp(ts_reset, UTC).astimezone(ZoneInfo(engine.config.time_zone))
    text = (
        f"You've used {round(used * 100)}% of your plan's weekly limit. It resets on "
        f"{short_date(reset)} at {reset:%H:%M}."
    )
    if threshold >= 0.90:
        text += (
            " If you like, I can switch the orchestrator and the reviewer to Sonnet until the reset "
            "to use less. I won't change it without your permission."
        )
        buttons = [
            [
                Button("Switch to Sonnet until the reset", f"sonnet:yes:{ts_reset}"),
                Button("Keep as is", "sonnet:no"),
            ]
        ]
        await engine.notify(None, text, buttons=buttons)
    else:
        await engine.notify(None, text)


async def use_sonnet_until(engine: Engine, ts_reset: int) -> str:
    """The user accepted moving to Sonnet: it is saved until the reset and undone on its own."""
    until = datetime.fromtimestamp(ts_reset, UTC)
    if until <= datetime.now(UTC):
        return "The weekly limit has already reset: nothing needs to change."
    await engine.db.kv_put("temporary_sonnet_until", until.isoformat(timespec="seconds"))
    local = until.astimezone(ZoneInfo(engine.config.time_zone))
    return (
        f"Done: until {short_date(local)} at {local:%H:%M}, the orchestrator and the reviewer use "
        "Sonnet. After that they go back to Opus on their own."
    )
