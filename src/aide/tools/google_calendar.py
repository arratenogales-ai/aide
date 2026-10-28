"""Google Calendar tools.

- Querying events and free slots: free. Slots are computed by code.
- Creating an event without guests: notice (it runs and the user is notified).
- Creating with guests, modifying or cancelling: approval with date, time, duration and
  guests.

Events are created with an identifier derived from the idempotency key: if the task
resumes after an interruption and creates the same event again, Google replies that it
already exists and it is not duplicated. Default time zone: the one in settings (UTC if
not changed).
"""

from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from claude_agent_sdk import ToolAnnotations, tool
from googleapiclient.errors import HttpError

from aide.core.approvals import approval_key, current_approval
from aide.core.context import SessionContext
from aide.core.hooks import note_progress
from aide.core.idempotency import action_key
from aide.core.permissions import Detail, formatters
from aide.core.policy import Decision
from aide.tools.google_auth import GoogleNotAuthorized, expired_authorization
from aide.tools.server import error, log_factory, text

READING = ToolAnnotations(readOnlyHint=True, openWorldHint=True)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=True)
DESTRUCTIVE = ToolAnnotations(readOnlyHint=False, destructiveHint=True, openWorldHint=True)
DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]
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


# --- pure helpers ---------------------------------------------------------------------------------


def to_datetime(value: str, zone: ZoneInfo) -> datetime:
    """ISO 8601 with or without a zone; without a zone it is interpreted in the user's zone."""
    value_text = str(value).strip().replace("Z", "+00:00")
    if len(value_text) == 10:
        date_ = datetime.combine(date.fromisoformat(value_text), time())
    else:
        date_ = datetime.fromisoformat(value_text)
    return date_.replace(tzinfo=zone) if date_.tzinfo is None else date_.astimezone(zone)


def start_end(event: dict[str, Any], zone: ZoneInfo) -> tuple[datetime, datetime, bool]:
    """Returns the start and end of a calendar event, and whether it lasts all day."""
    ini, end = event.get("start") or {}, event.get("end") or {}
    if "dateTime" in ini:
        return to_datetime(ini["dateTime"], zone), to_datetime(end["dateTime"], zone), False
    return to_datetime(ini.get("date"), zone), to_datetime(end.get("date"), zone), True


def calculate_slots(
    busy: list[tuple[datetime, datetime]],
    since: datetime,
    until: datetime,
    duration: timedelta,
    start_hour: time,
    end_hour: time,
    working_days: list[int],
) -> list[tuple[datetime, datetime]]:
    """Free intervals of at least `duration` within the working hours and working days."""
    zone = since.tzinfo
    busy = sorted((max(a, since), min(b, until)) for a, b in busy if b > since and a < until)
    slots = []
    day = since.date()
    while day <= until.date():
        if day.weekday() in working_days:
            ini_window = max(since, datetime.combine(day, start_hour, zone))
            end_window = min(until, datetime.combine(day, end_hour, zone))
            cursor = ini_window
            for a, b in busy:
                if b <= cursor or a >= end_window:
                    continue
                if a - cursor >= duration:
                    slots.append((cursor, a))
                cursor = max(cursor, b)
            if end_window - cursor >= duration:
                slots.append((cursor, end_window))
        day += timedelta(days=1)
    return slots


def event_id(key: str) -> str:
    """Valid id for Google Calendar (base32hex, 5-1024 characters) derived from the key."""
    return "aide" + key[:40]


def describe_event(
    title: str, ini: datetime, end: datetime, guests: list[str], extra: str = ""
) -> str:
    """Describes a calendar event as text, with its date, time and guests."""
    minutes = int((end - ini).total_seconds() // 60)
    lines = [
        f"Event: {title}",
        f"Date: {DAYS[ini.weekday()]}, {MONTHS[ini.month - 1]} {ini.day}, {ini.year}",
        f"Time: {ini:%H:%M} to {end:%H:%M} ({minutes} minutes)",
        f"Guests: {', '.join(guests) if guests else 'none'}",
    ]
    if guests:
        lines.append("Google will send the invitation to the guests.")
    if extra:
        lines.append(extra)
    return "\n".join(lines)


def _event_summary(event: dict[str, Any], zone: ZoneInfo) -> dict[str, Any]:
    ini, end, all_day = start_end(event, zone)
    return {
        "id": event.get("id"),
        "title": event.get("summary", "(untitled)"),
        "start": ini.isoformat(timespec="minutes"),
        "end": end.isoformat(timespec="minutes"),
        "all_day": all_day,
        "location": event.get("location"),
        "guests": [a.get("email") for a in event.get("attendees", []) if not a.get("self")],
        "busy": event.get("transparency", "opaque") != "transparent",
    }


# --- tools -------------------------------------------------------------------------------


@log_factory
def calendar_tools(ses: SessionContext) -> list[Any]:
    """Builds the Google Calendar tools, or none if Google is not set up."""
    core = ses.core
    auth = core.services.get("google")
    if auth is None:
        return []
    settings = core.config.settings.google
    zone = ZoneInfo(core.config.time_zone)
    calendar = settings.calendar

    async def call(function) -> Any:
        return await asyncio.to_thread(function)

    async def service() -> Any:
        return await asyncio.to_thread(auth.service, "calendar", "v3")

    async def expired(e: Exception) -> dict[str, Any]:
        return await expired_authorization(ses, e)

    async def events_between(svc, since: datetime, until: datetime) -> list[dict[str, Any]]:
        reply = await call(
            lambda: (
                svc.events()
                .list(
                    calendar_id=calendar,
                    time_min=since.isoformat(),
                    time_max=until.isoformat(),
                    single_events=True,
                    order_by="startTime",
                    max_results=250,
                )
                .execute()
            )
        )
        return [e for e in reply.get("items", []) if e.get("status") != "cancelled"]

    # --- approval formatters -----------

    async def format_create(_ses, _h, entry: dict[str, Any], _d: Decision) -> Detail:
        ini, end = to_datetime(entry["start"], zone), to_datetime(entry["end"], zone)
        return Detail(
            "event",
            describe_event(entry.get("title", ""), ini, end, entry.get("guests") or []),
        )

    async def format_modify(_ses, _h, entry: dict[str, Any], _d: Decision) -> Detail:
        svc = await service()
        current = await call(
            lambda: svc.events().get(calendar_id=calendar, event_id=entry["id"]).execute()
        )
        data = _event_summary(current, zone)
        ini = to_datetime(entry.get("start") or data["start"], zone)
        end = to_datetime(entry.get("end") or data["end"], zone)
        guests = entry.get("guests") if entry.get("guests") is not None else data["guests"]
        before = (
            f"Now: {data['title']}, {data['start'][:16].replace('T', ' ')} to {data['end'][11:16]}"
        )
        return Detail(
            "event",
            "Change event\n"
            + describe_event(entry.get("title") or data["title"], ini, end, guests, before),
        )

    async def format_cancel(_ses, _h, entry: dict[str, Any], _d: Decision) -> Detail:
        svc = await service()
        current = await call(
            lambda: svc.events().get(calendar_id=calendar, event_id=entry["id"]).execute()
        )
        data = _event_summary(current, zone)
        ini, end = to_datetime(data["start"], zone), to_datetime(data["end"], zone)
        extra = "The guests will be told about the cancellation." if data["guests"] else ""
        return Detail(
            "event",
            "Cancel event\n" + describe_event(data["title"], ini, end, data["guests"], extra),
        )

    formatters(ses).update(
        {
            "mcp__aide__calendar_create_event": format_create,
            "mcp__aide__calendar_modify_event": format_modify,
            "mcp__aide__calendar_cancel_event": format_cancel,
        }
    )

    async def with_approval(tool_: str, args: dict[str, Any]) -> bool:
        key = approval_key(ses.task_id, f"mcp__aide__{tool_}", args)
        return await current_approval(core.db, key) is not None

    # --- queries -----

    @tool(
        "calendar_events",
        "Events in the user's calendar between two dates (ISO 8601; without a time zone, the "
        "user's is used).",
        {
            "type": "object",
            "properties": {"since": {"type": "string"}, "until": {"type": "string"}},
            "required": ["since", "until"],
        },
        annotations=READING,
    )
    async def calendar_events(args: dict[str, Any]) -> dict[str, Any]:
        try:
            since, until = to_datetime(args["since"], zone), to_datetime(args["until"], zone)
            if until <= since:
                until = since + timedelta(days=1)
            events = await events_between(await service(), since, until)
            return text(
                [_event_summary(e, zone) for e in events] or "There are no events in that range."
            )
        except GoogleNotAuthorized as e:
            return await expired(e)
        except (ValueError, KeyError) as e:
            return error(f"Invalid dates: {e}")
        except HttpError as e:
            return error(f"Calendar returned an error: {e.status_code} {e.reason}")

    @tool(
        "calendar_slots",
        "Free slots of at least `duration` minutes between two dates, within the user's working "
        "hours (working days). The program calculates them from the busy events.",
        {
            "type": "object",
            "properties": {
                "since": {"type": "string"},
                "until": {"type": "string"},
                "duration": {"type": "integer", "description": "Minutes"},
            },
            "required": ["since", "until", "duration"],
        },
        annotations=READING,
    )
    async def calendar_slots(args: dict[str, Any]) -> dict[str, Any]:
        try:
            since, until = to_datetime(args["since"], zone), to_datetime(args["until"], zone)
            duration = timedelta(minutes=max(5, int(args["duration"])))
            events = await events_between(await service(), since, until)
            busy = []
            for e in events:
                r = _event_summary(e, zone)
                if r["busy"] and not (r["all_day"] and e.get("transparency") != "opaque"):
                    ini, end, _ = start_end(e, zone)
                    busy.append((ini, end))
            slots = calculate_slots(
                busy,
                since,
                until,
                duration,
                time.fromisoformat(settings.start_hour),
                time.fromisoformat(settings.end_hour),
                settings.working_days,
            )
            return text(
                [
                    {
                        "start": a.isoformat(timespec="minutes"),
                        "end": b.isoformat(timespec="minutes"),
                        "minutes": int((b - a).total_seconds() // 60),
                    }
                    for a, b in slots
                ]
                or "There are no free slots of that length within working hours."
            )
        except GoogleNotAuthorized as e:
            return await expired(e)
        except (ValueError, KeyError) as e:
            return error(f"Invalid data: {e}")
        except HttpError as e:
            return error(f"Calendar returned an error: {e.status_code} {e.reason}")

    # --- changes ---

    @tool(
        "calendar_create_event",
        "Creates an event in the user's calendar. Without guests it is created and the user is "
        "told; with guests it needs the user's approval (Google will send them the invitation).",
        {
            "type": "object",
            "properties": {
                "title": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "guests": {"type": "array", "items": {"type": "string"}},
                "minutes_reminder": {"type": "integer"},
            },
            "required": ["title", "start", "end"],
        },
        annotations=WRITE,
    )
    async def calendar_create_event(args: dict[str, Any]) -> dict[str, Any]:
        guests = [i.strip() for i in args.get("guests") or [] if i.strip()]
        if guests and not await with_approval("calendar_create_event", args):
            return error("An event with guests needs the user's approval, and it doesn't have it.")
        try:
            ini, end = to_datetime(args["start"], zone), to_datetime(args["end"], zone)
        except ValueError as e:
            return error(f"Invalid dates: {e}")
        if end <= ini:
            return error("The end time must be after the start time.")
        content = {"title": args["title"], "start": ini.isoformat(), "end": end.isoformat()}
        key = action_key(ses.task_id, "create_event", ",".join(sorted(guests)), content)
        actions = core.actions
        existing = await actions.start(
            key, ses.task_id, "create_event", ",".join(guests) or "the user", content
        )
        if existing is not None and existing.state == "done":
            return text(
                {
                    "id": (existing.result or {}).get("id"),
                    "note": "The event had already been created; it was not duplicated.",
                }
            )
        body = {
            "id": event_id(key),
            "summary": args["title"],
            "start": {"dateTime": ini.isoformat(), "timeZone": str(zone)},
            "end": {"dateTime": end.isoformat(), "timeZone": str(zone)},
        }
        if args.get("description"):
            body["description"] = args["description"]
        if args.get("location"):
            body["location"] = args["location"]
        if guests:
            body["attendees"] = [{"email": i} for i in guests]
        if args.get("minutes_reminder") is not None:
            body["reminders"] = {
                "useDefault": False,
                "overrides": [{"method": "popup", "minutes": int(args["minutes_reminder"])}],
            }
        try:
            svc = await service()
            try:
                event = await call(
                    lambda: (
                        svc.events()
                        .insert(
                            calendar_id=calendar,
                            body=body,
                            send_updates="all" if guests else "none",
                        )
                        .execute()
                    )
                )
            except HttpError as e:
                if e.status_code != 409:
                    raise
                # It already exists: the deterministic id avoids duplicating it
                # after an interruption.
                event = await call(
                    lambda: svc.events().get(calendar_id=calendar, event_id=body["id"]).execute()
                )
        except GoogleNotAuthorized as e:
            return await expired(e)
        except HttpError as e:
            await actions.fail(key, str(e))
            return error(
                f"Calendar returned an error while creating the event: {e.status_code} {e.reason}"
            )
        await actions.complete(key, {"id": event.get("id"), "link": event.get("htmlLink")})
        description = describe_event(args["title"], ini, end, guests)
        note_progress(
            ses.folder,
            f"Event created: {args['title']} ({MONTHS[ini.month - 1][:3]} {ini.day} {ini:%H:%M}).",
        )
        await core.tasks.event(
            ses.task_id,
            "external_action",
            f"Event created: {args['title']}",
            {"kind": "event", "id": event.get("id")},
        )
        if not guests and core.notify is not None and ses.role == "orchestrator":
            await core.notify(ses.task_id, f"I created an event in your calendar.\n{description}")
        return text({"id": event.get("id"), "link": event.get("htmlLink"), "created": description})

    @tool(
        "calendar_modify_event",
        "Changes an event (title, times, description, location or guests). It needs the user's "
        "approval.",
        {
            "type": "object",
            "properties": {
                "id": {"type": "string"},
                "title": {"type": "string"},
                "start": {"type": "string"},
                "end": {"type": "string"},
                "description": {"type": "string"},
                "location": {"type": "string"},
                "guests": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["id"],
        },
        annotations=WRITE,
    )
    async def calendar_modify_event(args: dict[str, Any]) -> dict[str, Any]:
        if not await with_approval("calendar_modify_event", args):
            return error("Changing an event needs the user's approval, and it doesn't have it.")
        patch: dict[str, Any] = {}
        if args.get("title"):
            patch["summary"] = args["title"]
        if args.get("start"):
            patch["start"] = {
                "dateTime": to_datetime(args["start"], zone).isoformat(),
                "timeZone": str(zone),
            }
        if args.get("end"):
            patch["end"] = {
                "dateTime": to_datetime(args["end"], zone).isoformat(),
                "timeZone": str(zone),
            }
        if args.get("description") is not None:
            patch["description"] = args["description"]
        if args.get("location") is not None:
            patch["location"] = args["location"]
        if args.get("guests") is not None:
            patch["attendees"] = [{"email": i} for i in args["guests"]]
        try:
            svc = await service()
            event = await call(
                lambda: (
                    svc.events()
                    .patch(
                        calendar_id=calendar, event_id=args["id"], body=patch, send_updates="all"
                    )
                    .execute()
                )
            )
        except GoogleNotAuthorized as e:
            return await expired(e)
        except HttpError as e:
            return error(
                f"Calendar returned an error while changing it: {e.status_code} {e.reason}"
            )
        note_progress(ses.folder, f"Event changed: {event.get('summary')}.")
        await core.tasks.event(
            ses.task_id,
            "external_action",
            f"Event changed: {event.get('summary')}",
            {"kind": "event", "id": event.get("id")},
        )
        return text(_event_summary(event, zone))

    @tool(
        "calendar_cancel_event",
        "Cancels an event in the user's calendar. It needs the user's approval; if there are "
        "guests, they are told.",
        {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
        annotations=DESTRUCTIVE,
    )
    async def calendar_cancel_event(args: dict[str, Any]) -> dict[str, Any]:
        if not await with_approval("calendar_cancel_event", args):
            return error("Canceling an event needs the user's approval, and it doesn't have it.")
        key = action_key(ses.task_id, "cancel_event", args["id"], {"id": args["id"]})
        actions = core.actions
        existing = await actions.start(
            key, ses.task_id, "cancel_event", args["id"], {"id": args["id"]}
        )
        if existing is not None and existing.state == "done":
            return text("The event was already canceled; not repeated.")
        try:
            svc = await service()
            try:
                await call(
                    lambda: (
                        svc.events()
                        .delete(calendar_id=calendar, event_id=args["id"], send_updates="all")
                        .execute()
                    )
                )
            except HttpError as e:
                if e.status_code not in (404, 410):
                    raise
        except GoogleNotAuthorized as e:
            return await expired(e)
        except HttpError as e:
            await actions.fail(key, str(e))
            return error(f"Calendar returned an error while canceling: {e.status_code} {e.reason}")
        await actions.complete(key, {"id": args["id"]})
        note_progress(ses.folder, f"Event canceled: {args['id']}.")
        await core.tasks.event(
            ses.task_id, "external_action", "Event canceled", {"kind": "event", "id": args["id"]}
        )
        return text("Event canceled.")

    return [
        calendar_events,
        calendar_slots,
        calendar_create_event,
        calendar_modify_event,
        calendar_cancel_event,
    ]
