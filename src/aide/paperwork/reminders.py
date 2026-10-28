"""Reminders for the dates of your documents.

By code, without a model, once a day from 9:00 while there is a connection: 60, 30 and 7
days before each date of a saved document, and the same day, all in a single message.
Each reminder goes out once: if there was no connection one day, when it returns the one
that is due is sent (not the overdue ones one by one). Without a connection nothing is
recorded, so it is sent at the next check with a connection. When a document is saved,
the reminder due that day counts as given (the user just saw it), and the morning
summary carries that day's and marks them as notified.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from aide.paperwork import NOTICES, registry, short_date

log = logging.getLogger(__name__)

DAY_KEY = "documents:notices"
# No reminders before this hour (no middle-of-the-night messages).
NOTICES_HOUR = 9
# Dates per message: Telegram cuts a message of more than 4,096 characters.
MAX_LINES = 20

Statements = list[tuple[str, tuple[Any, ...]]]


def stage(days: int) -> int | None:
    """The reminder due `days` before the date: 60, 30, 7 or 0; None if it is not yet due or
    has already passed."""
    if days < 0 or days > max(NOTICES):
        return None
    return min(a for a in NOTICES if a >= days)


def notice_text(f: dict[str, Any], today: date) -> str:
    """Builds the reminder text for a document date, saying how soon it is."""
    day = date.fromisoformat(f["date"])
    days = (day - today).days
    source = f.get("title") or "a document"
    if days == 0:
        return f"Today {f['description']} ({source})."
    timing = "tomorrow" if days == 1 else f"in {days} days"
    return f"On {short_date(day)} ({timing}) {f['description']} ({source})."


def _sent(f: dict[str, Any]) -> set[int]:
    return set(registry.given_notices(f.get("notices")))


def _mark(f: dict[str, Any], sent: set[int], touches: int) -> tuple[str, tuple[Any, ...]]:
    # This reminder and the earlier ones, which no longer make sense to send late.
    new = sorted(sent | {a for a in NOTICES if a >= touches}, reverse=True)
    return ("UPDATE document_dates SET notices=? WHERE id=?", (json.dumps(new), f["id"]))


async def pending_detail(
    db: Any, today: date
) -> list[tuple[dict[str, Any], str, tuple[str, tuple[Any, ...]]]]:
    """The reminders due `today` that have not been given: each date, its line and how to record
    it as given (the morning summary needs the date so as not to repeat those of the next 7
    days)."""
    rows = await db.everything(
        "SELECT f.*, d.title FROM document_dates f JOIN documents d ON d.id = f.document_id "
        "WHERE d.state='saved' AND f.date >= ? AND f.date <= ? ORDER BY f.date, f.id",
        (today.isoformat(), (today + timedelta(days=max(NOTICES))).isoformat()),
    )
    output = []
    for f in rows:
        try:
            touches = stage((date.fromisoformat(f["date"]) - today).days)
            sent = _sent(f)
            if touches is None or touches in sent:
                continue
            output.append((f, notice_text(f, today), _mark(f, sent, touches)))
        except Exception:  # an odd date cannot leave the others without reminders
            log.exception("cannot send the reminder for date %s of a document", f.get("id"))
    return output


async def pending(db: Any, today: date) -> tuple[list[str], Statements]:
    """The reminders due `today` that have not been given: their lines and how to record them as
    given."""
    detail = await pending_detail(db, today)
    return [line for _f, line, _m in detail], [mark for _f, _l, mark in detail]


async def mark(db: Any, marks: Statements) -> None:
    if marks:
        await db.run_several(marks)


async def mark_as_seen(db: Any, document_id: int, today: date) -> None:
    """When a document is saved, the reminder due today for each date is already given: the
    user just saw its dates. The next one is that of the coming stage."""
    marks: Statements = []
    for f in await db.everything(
        "SELECT * FROM document_dates WHERE document_id=?", (document_id,)
    ):
        try:
            touches = stage((date.fromisoformat(f["date"]) - today).days)
            if touches is not None:
                marks.append(_mark(f, _sent(f), touches))
        except Exception:
            log.exception("cannot mark date %s of a document as seen", f.get("id"))
    await mark(db, marks)


def message(lines: list[str]) -> str:
    """Joins reminder lines into one message."""
    if len(lines) == 1:
        return lines[0]
    return "A reminder of these dates in your documents.\n" + "\n".join(f"- {x}" for x in lines)


async def review(engine: Any, moment: datetime | None = None) -> int:
    """Sends the reminders due today, once a day and from 9:00. Returns how many dates it has
    notified. If there are many, they go in several messages (Telegram cuts very long ones)
    and each counts as given only if it went out."""
    db = engine.db
    local = (moment or datetime.now(UTC)).astimezone(ZoneInfo(engine.config.time_zone))
    today = local.date()
    if local.hour < NOTICES_HOUR or await db.kv_read(DAY_KEY) == today.isoformat():
        return 0
    summary = engine.config.settings.morning_summary
    if summary.active:
        from aide import morning

        if local.time() < morning.hour_of(summary.until) and (
            await db.kv_read(morning.DAY_KEY) != today.isoformat()
        ):
            # The morning summary can still go out and carries them: if it does not go
            # out, they go after their cutoff hour.
            return 0
    detail = await pending_detail(db, today)
    notified = 0
    for i in range(0, len(detail), MAX_LINES):
        chunk = detail[i : i + MAX_LINES]
        if not await engine.notify(None, message([line for _f, line, _m in chunk])):
            return notified  # no connection: what is missing goes out at the next check
        await mark(db, [marker for _f, _l, marker in chunk])
        notified += len(chunk)
    if notified:
        log.info("document reminders sent: %d", notified)
    await db.kv_put(DAY_KEY, today.isoformat())
    return notified
