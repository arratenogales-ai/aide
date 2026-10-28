"""Sample data to show the panel: an invented person and their week.

Creates, in a separate folder (by default `.tmp/demo`, which does not go into the repository), a
sample project with its configuration, its database and its files: tasks in different states with
their progress and deliverables, scheduled tasks, projects, documents with dates, profile and
memory, calendar events, important emails, usage and limits, and health (periods, gym, meals with
photos drawn by code, habits and health profile). Everything is invented and says so: there are no
real names, companies or places, and the emails are from example domains.

It never touches the real data: it uses AIDE_ROOT, AIDE_DATA and AIDE_FOLDERS pointing to the sample
folder, and refuses to write to a folder that is not its own.

    uv run python scripts/demo_data.py              # creates (or redoes) the sample data
    uv run python scripts/demo_data.py --serve      # and opens the panel on them
    uv run python scripts/demo_data.py --name Sam   # with this name (Alex if none is given)

In a terminal, without --name, it asks for the name.
With --serve, the panel listens on 127.0.0.1, on a free port and with a random token, and the entry
link is printed in the terminal. It does not connect to Telegram, Google or the model.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import shutil
import socket
import sys
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

PUB_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FOLDER = PUB_ROOT / ".tmp" / "demo"
MARK = "DEMO-DATA.txt"
MARK_TEXT = (
    "Example data for Aide, made by scripts/demo_data.py.\n"
    "Everything here is invented (the person, the tasks, the documents and the emails).\n"
)
DEFAULT_NAME = "Alex"
EXAMPLE_NOTICE = "Example data: invented for the demo."


# --- folders -------------------------------------------------------------------------------


def safe_folder(folder: Path) -> Path:
    """The sample folder: new, or one this script already made. Never the real one."""
    folder = folder.expanduser().resolve()
    forbidden = [PUB_ROOT, PUB_ROOT / "data", PUB_ROOT / "agent", Path.home()]
    if folder in forbidden or any(folder == p.parent for p in forbidden if p != Path.home()):
        raise SystemExit(f"Refusing to use {folder}: choose an empty folder for the demo.")
    if (PUB_ROOT / "data") in folder.parents or (PUB_ROOT / "agent") in folder.parents:
        raise SystemExit(f"Refusing to use {folder}: it is inside the real data.")
    if folder.exists() and any(folder.iterdir()) and not (folder / MARK).is_file():
        raise SystemExit(
            f"Refusing to use {folder}: it isn't empty and wasn't made by this script."
        )
    return folder


def prepare_folders(folder: Path, name: str) -> None:
    """Redoes the sample folder and leaves the environment pointing to it."""
    if folder.exists():
        shutil.rmtree(folder)
    project = folder / "project"
    (project / "config").mkdir(parents=True)
    for file in ("settings.yaml", "models.yaml", "permissions.yaml"):
        shutil.copy(PUB_ROOT / "config" / file, project / "config" / file)
    (project / "agent").mkdir()
    shutil.copy(PUB_ROOT / "agent" / "CLAUDE.md", project / "agent" / "CLAUDE.md")
    (project / ".env").write_text(f"# {EXAMPLE_NOTICE}\nUSER_NAME={name}\n", encoding="utf-8")
    (folder / MARK).write_text(MARK_TEXT, encoding="utf-8")
    os.environ["AIDE_ROOT"] = str(project)
    os.environ["AIDE_DATA"] = str(folder / "data")
    os.environ["AIDE_FOLDERS"] = str(folder)


# --- sample texts -----------------------------------------------------------------------------


def profile_text(name: str) -> str:
    """The sample profile, with that name."""
    return f"""# User profile ({EXAMPLE_NOTICE.lower().removesuffix(".")})

## Who I am
- Name: {name}
- Where I live: a mid-sized city, in a rented two-bedroom flat
- Work: product designer at a small software studio, three days in the office
- Languages: English, and learning Spanish

## Goals
- Move to a flat with a balcony before next summer
- Keep the monthly budget under control
- Run a 10 km race in the spring

## How I want you to work
- Short answers first, details after
- Ask before sending anything in my name
- Spreadsheets for comparisons, one page summaries for everything else

## Writing
- Friendly and direct, no exclamation marks
- British spelling

## Accounts
- Email and calendar: Gmail (example account)
"""


def memory_text(name: str) -> str:
    """The sample memory, with that name."""
    return f"""# Aide memory

Lasting facts approved by the user. {EXAMPLE_NOTICE}

- (2026-09-14) {name} prefers trains to flights for trips under five hours
- (2026-09-30) The household budget sheet is updated on the first Sunday of each month
- (2026-10-06) {name} is vegetarian
- (2026-10-15) Running days are Tuesday, Thursday and Saturday mornings
- (2026-10-22) The current lease ends on 31 March, with three months' notice
"""


TASKS: list[dict[str, Any]] = [
    {
        "request": "Compare three laptops under 1,200 for photo editing: screen, weight, battery "
        "and repairability. A table and a recommendation, please.",
        "title": "Laptop comparison for photo editing",
        "summary": "Three models compared on 11 criteria. The 14-inch model wins on screen and "
        "repairability; the lightest one is the backup choice.",
        "final": "completed",
        "days": 6,
        "duration": 23,
        "size": "medium",
        "model": "claude-sonnet-5-5",
        "tokens": (48_000, 9_500, 820_000, 41_000),
        "steps": [
            "Collect the specifications of the three models",
            "Read independent reviews of screen and battery",
            "Build the comparison table",
            "Write the recommendation",
        ],
        "tools": [
            ("WebSearch", "laptop colour accuracy review 14-inch"),
            ("WebFetch", "https://reviews.example.com/laptops/screen-test"),
            ("Write", "results/laptop-comparison.md"),
            ("Bash", "python make_table.py"),
        ],
        "files": {
            "results/laptop-comparison.md": "# Laptop comparison\n\n"
            '| | Model A 14" | Model B 13" | Model C 15" |\n|---|---|---|---|\n'
            "| Screen (sRGB) | 100% | 96% | 92% |\n| Weight | 1.4 kg | 1.1 kg | 1.8 kg |\n"
            "| Battery (video) | 14 h | 16 h | 10 h |\n| Repairability | 8/10 | 5/10 | 7/10 |\n\n"
            "Recommendation: Model A.\n",
            "results/scores.csv": "criterion,model_a,model_b,model_c\nscreen,9,8,7\n"
            "weight,7,9,5\nbattery,8,9,6\nrepair,8,5,7\n",
        },
        "final_folder": "Laptop comparison",
    },
    {
        "request": "Plan a three-day weekend by train, leaving Friday after work. Budget 400 "
        "for travel and two nights. Vegetarian-friendly places to eat.",
        "title": "Weekend trip by train",
        "summary": "Itinerary with two train options each way, two guesthouses within budget and "
        "a short list of places to eat.",
        "final": "completed",
        "days": 3,
        "duration": 17,
        "size": "medium",
        "model": "claude-sonnet-5-5",
        "tokens": (36_000, 7_200, 610_000, 30_000),
        "steps": [
            "Find train times for Friday evening and Sunday",
            "Shortlist two guesthouses",
            "Pick places to eat",
            "Put the itinerary together",
        ],
        "tools": [
            ("WebSearch", "weekend train timetable friday evening"),
            ("WebFetch", "https://travel.example.org/guesthouses"),
            ("Write", "results/itinerary.md"),
        ],
        "files": {
            "results/itinerary.md": "# Weekend itinerary\n\n- Friday 18:42 train, arrive "
            "21:05\n- Saturday: old town walk, market, river path\n- Sunday 16:10 train back\n\n"
            "Total estimate: 362.\n",
        },
        "final_folder": "Weekend trip",
    },
    {
        "request": "Look for two-bedroom flats with a balcony to rent, up to 1,500 a month, "
        "within 30 minutes of the office by public transport. Shortlist the best five.",
        "title": "Flat search: two bedrooms with a balcony",
        "final": "running",
        "days": 0,
        "minutes_back": 14,
        "size": "large",
        "model": "claude-opus-5-5",
        "tokens": (61_000, 8_800, 1_300_000, 52_000),
        "steps": [
            "Search the listing sites",
            "Check commute times",
            "Score each flat",
            "Write the shortlist",
        ],
        "facts": 2,
        "tools": [
            ("WebSearch", "two bedroom flat balcony rent"),
            ("WebFetch", "https://listings.example.net/search?rooms=2&balcony=1"),
            ("WebFetch", "https://listings.example.net/flat/4821"),
            ("Bash", "python commute.py --max 30"),
        ],
        "subagent": "researcher",
        "files": {
            "data/listings.csv": "id,rooms,balcony,rent,commute_min\n4821,2,yes,1420,24\n"
            "4907,2,yes,1380,31\n5012,2,yes,1495,18\n",
        },
    },
    {
        "request": "Draft a reply to the letting agent about the boiler repair: ask for a date "
        "this week and mention that the hot water has been off since Monday.",
        "title": "Reply to the letting agent about the boiler",
        "final": "waiting_approval",
        "days": 0,
        "minutes_back": 45,
        "size": "small",
        "model": "claude-sonnet-5-5",
        "tokens": (9_000, 2_100, 120_000, 8_000),
        "steps": ["Read the last emails in the thread", "Write the draft", "Send it"],
        "facts": 2,
        "tools": [("gmail_thread", "Boiler repair"), ("gmail_create_draft", "Re: Boiler")],
        "approval": "Send email to lettings@agency.example.com\nSubject: Re: Boiler repair\n\n"
        "Hello, the hot water has been off since Monday. Could someone come this week? "
        "Any morning works for me. Thanks, {name}",
    },
    {
        "request": "Summarise this year's electricity bills: monthly cost, usage and how it "
        "compares with last year.",
        "title": "Electricity bills summary",
        "final": "paused_limit",
        "days": 1,
        "duration": 9,
        "size": "medium",
        "model": "claude-sonnet-5-5",
        "tokens": (22_000, 3_100, 340_000, 15_000),
        "steps": ["Read the bills", "Build the monthly table", "Write the summary"],
        "facts": 1,
        "tools": [("Read", "input/bills.csv")],
        "files": {
            "input/bills.csv": "month,kwh,cost\n2026-01,310,96\n2026-02,288,89\n2026-03,251,78\n",
        },
    },
    {
        "request": "Make a reading list for the next month: three novels and two books on "
        "design, with one line on each.",
        "title": "Reading list for November",
        "final": "in_queue",
        "days": 0,
        "minutes_back": 3,
        "size": "small",
    },
    {
        "request": "Find a 10 km training plan for beginners, three runs a week.",
        "title": "10 km training plan",
        "summary": "Eight-week plan, three runs a week, with one long run on Saturdays.",
        "final": "completed",
        "days": 9,
        "duration": 8,
        "size": "small",
        "model": "claude-haiku-5-5",
        "tokens": (12_000, 2_900, 150_000, 9_000),
        "steps": ["Find two plans", "Adapt them to three days a week"],
        "tools": [("WebSearch", "10k beginner plan three runs a week")],
        "files": {
            "results/training-plan.md": "# 10 km plan\n\nWeek 1: 3 x 25 min easy\nWeek 8: race\n",
        },
        "final_folder": "Training plan",
    },
    {
        "request": "Book a table for four on Saturday night somewhere quiet.",
        "title": "Saturday dinner booking",
        "final": "cancelled",
        "days": 4,
        "duration": 2,
        "size": "small",
        "model": "claude-haiku-5-5",
        "tokens": (4_000, 900, 40_000, 3_000),
    },
]

PATHS = {
    "completed": ["in_triage", "running", "reviewing", "completed"],
    "running": ["in_triage", "running"],
    "waiting_approval": ["in_triage", "running", "waiting_approval"],
    "paused_limit": ["in_triage", "running", "paused_limit"],
    "in_queue": ["in_triage", "in_queue"],
    "cancelled": ["in_triage", "running", "cancelled"],
}

DOCUMENTS: list[dict[str, Any]] = [
    {
        "title": "Home insurance policy",
        "kind": "insurance",
        "entity": "Example Insurance Co.",
        "summary": "Contents and liability cover for the flat. Renews every year.",
        "dates": [("renewal", 24, "the home insurance renews")],
        "saved": 40,
    },
    {
        "title": "Flat lease",
        "kind": "housing",
        "entity": "Example Lettings",
        "summary": "Lease of the current flat. Three months' notice to leave.",
        "dates": [
            ("advance_notice", 33, "last day to give notice on the lease"),
            ("end", 154, "the lease ends"),
        ],
        "saved": 120,
    },
    {
        "title": "Car inspection certificate",
        "kind": "car",
        "entity": "Vehicle testing centre (example)",
        "summary": "Roadworthiness certificate for the car.",
        "dates": [("expiry", 9, "the car inspection expires")],
        "saved": 300,
    },
    {
        "title": "Dental check-up",
        "kind": "health",
        "entity": "Example Dental Clinic",
        "summary": "Yearly check-up and cleaning.",
        "dates": [("appointment", 2, "dental check-up at 09:30")],
        "saved": 20,
    },
    {
        "title": "Employment contract",
        "kind": "work",
        "entity": "Example Studio Ltd.",
        "summary": "Permanent contract, 32 hours a week, 25 days of holiday.",
        "dates": [("deadline", 63, "the annual review is due")],
        "saved": 400,
    },
    {
        "title": "Spanish course enrolment",
        "kind": "education",
        "entity": "Example Language School",
        "summary": "Evening course, level B1, two evenings a week.",
        "dates": [("payment", 16, "the second course payment is due")],
        "saved": 35,
    },
]

EMAILS = [
    ("billing@bank.example.com", "Example Bank", "Your card statement is ready", "bank", 3),
    (
        "appointments@clinic.example.org",
        "Example Dental Clinic",
        "Reminder: your check-up this week",
        "appointment",
        20,
    ),
    (
        "noreply@insurer.example.net",
        "Example Insurance Co.",
        "Your policy renews soon",
        "insurance",
        30,
    ),
]

EVENTS = [
    # (days from today, time or None if all-day, duration in minutes, title, place)
    (0, "10:00", 45, "Design review with the team", "Video call"),
    (0, "18:30", 60, "Running club", "City park"),
    (1, "08:15", 30, "Call the letting agent", ""),
    (2, "09:30", 45, "Dental check-up", "Example Dental Clinic"),
    (3, None, 0, "Office day", ""),
    (5, "11:00", 120, "Flat viewing", "Riverside area"),
]

SCHEDULED_TASKS = [
    (
        "Plan my week: calendar, deadlines from my documents and a short to-do list",
        {"kind": "weekly", "days": ["monday"], "hour": "07:30"},
        "run_if_recent",
    ),
    (
        "Morning digest: design news and anything new on the flat listings",
        {"kind": "daily", "hour": "07:45"},
        "skip",
    ),
    (
        "Check which subscriptions renew this month and what they cost",
        {"kind": "monthly", "day": 1, "hour": "09:00"},
        "run_always",
    ),
]


# More projects, for the Projects tab: name, what it is, a file and its contents.
EXTRA_PROJECTS = [
    (
        "flat-search",
        "Flats with a balcony: the listings found, scored and compared.",
        "flats.csv",
        "area,rooms,balcony,rent,score\nNorth,2,yes,1450,8\nRiverside,2,yes,1520,7\n",
    ),
    (
        "running-plan",
        "A twelve-week plan for the spring 10 km race, three runs a week.",
        "plan.md",
        "# Running plan\n\nWeek 1: 3 x 5 km easy.\n",
    ),
    (
        "portfolio-site",
        "A small static portfolio website with three case studies.",
        "index.html",
        "<h1>Portfolio</h1>\n",
    ),
]
# Health: profile, periods (days ago from today and duration), gym and meals.
HEALTH_PROFILE = {
    "sex": "female",
    "birth": "1994-05-12",
    "height": "168",
    "goal": "maintain",
    "activity": "moderate",
    "water": "2000",
}
PERIODS = [(128, 5), (99, 4), (70, 5), (42, 5), (13, 4)]
# Each session: days ago and its lines, with the squat and bench press going up.
SESSIONS = [
    (27, "squat 4x8 50\nbench press 3x10 30\nbarbell row 3x10 30"),
    (24, "romanian deadlift 3x10 40\noverhead press 3x8 20\npull-ups 3x5"),
    (20, "squat 4x8 52.5\nbench press 3x10 32.5\nbarbell row 3x10 32.5"),
    (17, "romanian deadlift 3x10 42.5\noverhead press 3x8 22.5\npull-ups 3x6"),
    (13, "squat 4x8 55\nbench press 3x8 35\nlat pulldown 3x12 35"),
    (10, "hip thrust 4x10 60\nlateral raises 3x12 5\npull-ups 3x6"),
    (6, "squat 4x8 57.5\nbench press 3x8 35\nbarbell row 3x10 35"),
    (3, "romanian deadlift 3x8 47.5\noverhead press 3x8 25\npull-ups 3x7"),
    (1, "squat 4x6 60\nbench press 3x8 37.5\ncrunches 3x15"),
]
# Today's meals, with their photo drawn by code: (type, drawing, foods).
MEALS_TODAY = [
    (
        "breakfast",
        "avena",
        [
            ("Porridge oats with milk", 1, "bowl", 250, 290, 11, 45, 7),
            ("Blueberries", 80, "g", 80, 46, 0.6, 11, 0.3),
            ("Walnuts", 15, "g", 15, 98, 2.3, 2, 9.8),
        ],
    ),
    (
        "lunch",
        "salad",
        [
            ("Lentil salad with feta", 1, "bowl", 320, 430, 22, 42, 18),
            ("Wholemeal bread", 1, "slice", 35, 85, 3.5, 15, 1),
        ],
    ),
    (
        "snack",
        "fruit",
        [
            ("Greek yogurt", 150, "g", 150, 145, 14, 6, 7.5),
            ("Apple", 1, "unit", 180, 94, 0.5, 25, 0.3),
        ],
    ),
]
# What was eaten the previous 6 days, in kcal per meal (breakfast, lunch, dinner), no photo.
WEEK_MEALS = [
    (6, (520, 780, 900)),
    (5, (480, 840, 860)),
    (4, (560, 720, 1010)),
    (3, (470, 810, 880)),
    (2, (540, 900, 820)),
    (1, (500, 760, 950)),
]
WEEK_NAMES = {
    "breakfast": "Toast with peanut butter and banana",
    "lunch": "Vegetable curry with rice",
    "dinner": "Pasta with tomato and chickpeas",
}


# --- the database ----------------------------------------------------------------------------


def iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat(timespec="seconds")


async def create_task(engine: Any, data: dict[str, Any], now: datetime, name: str) -> int:
    from aide.core.states import TaskState

    paths = engine.config.paths
    tasks = engine.tasks
    t = await tasks.create(data["request"], title=data["title"])
    paths.create_task_folder(t.id)
    start = now - timedelta(days=data.get("days", 0), minutes=data.get("minutes_back", 0))
    if data.get("days"):
        start = start.replace(hour=9 + t.id % 8, minute=(t.id * 7) % 60)
    duration = timedelta(minutes=data.get("duration", data.get("minutes_back", 5)))
    route = PATHS[data["final"]]
    for state in route:
        await tasks.change_state(t.id, TaskState(state), "demo")
    await tasks.update(
        t.id,
        size=data.get("size"),
        model=data.get("model"),
        summary=data.get("summary"),
        turns_used=len(data.get("tools", [])) * 2 + 2,
        pause_reason="plan limit" if data["final"] == "paused_limit" else None,
        pause_until=iso(now + timedelta(hours=2)) if data["final"] == "paused_limit" else None,
    )
    session = f"demo-{t.id}"
    facts = data.get("facts", len(data.get("steps", [])))
    for n, subject in enumerate(data.get("steps", []), 1):
        await tasks.create_step(t.id, session, str(n), subject, None)
        state = "completed" if n <= facts else "pending"
        if n == facts + 1 and data["final"] == "running":
            state = "in_progress"
        await tasks.update_step(t.id, session, str(n), state=state)
    await tasks.event(t.id, "triage", "new_task", {"kind": "new_task", "size": data.get("size")})
    if data.get("subagent"):
        await tasks.event(t.id, "start_subagent", data["subagent"], {"agent_id": f"demo-{t.id}-1"})
    for tool, detail in data.get("tools", []):
        await tasks.event(t.id, "tool", f"{tool}: {detail}", {"tool": tool})
    if data.get("approval"):
        await engine.db.run(
            "INSERT INTO approvals(task_id, kind, tool, entry, detail, level, "
            "state, created) VALUES (?, 'email', 'mcp__aide__gmail_send_draft', '{}', ?, 2, "
            "'pending', ?)",
            (t.id, data["approval"].replace("{name}", name), iso(now - timedelta(minutes=2))),
        )
    if data["final"] == "completed":
        await tasks.event(t.id, "review", "Reviewed: the result answers the request")
    folder = paths.task(t.id)
    for relative, text in data.get("files", {}).items():
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        if relative.startswith("results/"):
            await tasks.log_file(t.id, path, "result")
    (folder / "progress.md").write_text(
        f"# Task {t.id} progress\n\n"
        + "".join(
            f"- [{'x' if n <= facts else ' '}] {p}\n"
            for n, p in enumerate(data.get("steps", []), 1)
        ),
        encoding="utf-8",
    )
    if data.get("final_folder"):
        final = paths.finished / f"{start:%Y-%m-%d} {data['final_folder']}"
        final.mkdir(parents=True, exist_ok=True)
        for path in (folder / "results").glob("*"):
            shutil.copy(path, final / path.name)
        await tasks.update(t.id, final_folder=str(final))
    if data.get("tokens"):
        entry, output, reading, creation = data["tokens"]
        await engine.db.run(
            "INSERT INTO usage(task_id, hour, origin, model, input_tokens, output_tokens, "
            "cache_read, cache_creation, cost_usd, turns, web_searches) "
            "VALUES (?, ?, 'orchestrator', ?, ?, ?, ?, ?, 0, ?, 0)",
            (
                t.id,
                iso(start + duration),
                data.get("model") or "claude-sonnet-5-5",
                entry,
                output,
                reading,
                creation,
                len(data.get("tools", [])) * 2 + 2,
            ),
        )
    finished = data["final"] in ("completed", "cancelled", "paused_limit")
    await stamp_date(engine.db, t.id, start, start + duration, final=finished)
    return t.id


async def stamp_date(
    db: Any, task_id: int, start: datetime, end: datetime, final: bool = True
) -> None:
    """Spreads the task's hours between its start and its end, in the order everything happened."""

    async def distribute(table: str, columns: tuple[str, ...]) -> None:
        rows = await db.everything(
            f"SELECT id FROM {table} WHERE task_id=? ORDER BY id", (task_id,)
        )
        for n, row in enumerate(rows):
            moment = start + (end - start) * ((n + 1) / (len(rows) + 1))
            assignments = ", ".join(f"{c}=?" for c in columns)
            await db.run(
                f"UPDATE {table} SET {assignments} WHERE id=?",
                (*[iso(moment)] * len(columns), row["id"]),
            )

    await distribute("events", ("hour",))
    await distribute("steps", ("created", "updated"))
    await distribute("files", ("hour",))
    transitions = await db.everything(
        "SELECT id FROM transitions WHERE task_id=? ORDER BY id", (task_id,)
    )
    # In an unfinished task, the status changes are at the start: it has been running a while.
    until = end if final else start + min(end - start, timedelta(minutes=2))
    for n, row in enumerate(transitions):
        moment = start if n == 0 else start + (until - start) * (n / (len(transitions) - 1))
        await db.run("UPDATE transitions SET hour=? WHERE id=?", (iso(moment), row["id"]))
    await db.run(
        "UPDATE tasks SET created=?, updated=? WHERE id=?", (iso(start), iso(end), task_id)
    )


async def create_documents(engine: Any, today: date, now: datetime) -> None:
    from aide.paperwork import registry

    base = engine.config.paths.documents / f"{today:%Y}"
    base.mkdir(parents=True, exist_ok=True)
    for n, d in enumerate(DOCUMENTS, 1):
        name = f"{today:%Y%m%d}-{d['title'].replace(' ', '-')}.txt"
        (base / name).write_text(
            f"{d['title']}\n{d['entity']}\n\n{d['summary']}\n\n{EXAMPLE_NOTICE}\n",
            encoding="utf-8",
        )
        dates = [
            {
                "kind": kind,
                "date": (today + timedelta(days=days)).isoformat(),
                "description": description,
                "checked": True,
            }
            for kind, days, description in d["dates"]
        ]
        document_id = await registry.create(
            engine.db,
            {
                "title": d["title"],
                "kind": d["kind"],
                "entity": d["entity"],
                "number": f"EX-{1000 + n * 37}",
                "summary": d["summary"],
                "file": f"{today:%Y}/{name}",
                "original_name": f"{d['title']}.txt",
                "text": f"{d['summary']} {EXAMPLE_NOTICE}",
                "origin_text": "pdf",
            },
            dates,
        )
        await registry.save(engine.db, document_id)
        saved = iso(now - timedelta(days=d["saved"]))
        await engine.db.run(
            "UPDATE documents SET created=?, saved=? WHERE id=?",
            (saved, saved, document_id),
        )


async def create_scheduled_tasks(engine: Any, zone: ZoneInfo, now: datetime, last: int) -> None:
    from aide.core import scheduler
    from aide.maintenance.report import ensure_scheduled_task

    for n, (description, when_data, policy) in enumerate(SCHEDULED_TASKS):
        timing = scheduler.interpret(when_data, zone)
        next_run = scheduler.next_step(timing, now.astimezone(zone), zone)
        await engine.db.run(
            "INSERT INTO scheduled_tasks(description, timing, policy, active, created, next_run, "
            "timing_text, last_run, last_task) VALUES (?, ?, ?, 1, ?, ?, ?, ?, ?)",
            (
                description,
                timing.to_json(),
                policy,
                iso(now - timedelta(days=30)),
                iso(next_run) if next_run else None,
                scheduler.describe(timing),
                iso(now - timedelta(days=1 + n)) if n < 2 else None,
                last if n == 0 else None,
            ),
        )
    # A paused example one and the weekly maintenance.
    timing = scheduler.interpret({"kind": "weekly", "days": ["friday"], "hour": "17:00"}, zone)
    await engine.db.run(
        "INSERT INTO scheduled_tasks(description, timing, policy, active, created, timing_text) "
        "VALUES (?, ?, 'skip', 0, ?, ?)",
        (
            "Remind me to water the plants and list what's in the fridge",
            timing.to_json(),
            iso(now - timedelta(days=60)),
            scheduler.describe(timing),
        ),
    )
    await ensure_scheduled_task(engine)


async def create_life(engine: Any, zone: ZoneInfo, now: datetime) -> None:
    from aide import life

    local = now.astimezone(zone)
    events = []
    for days, hour, minutes, title, place in EVENTS:
        day = local.date() + timedelta(days=days)
        if hour is None:
            events.append(
                {
                    "title": title,
                    "place": place,
                    "start": day.isoformat(),
                    "end": (day + timedelta(days=1)).isoformat(),
                    "all_day": True,
                }
            )
            continue
        h, m = map(int, hour.split(":"))
        start = datetime(day.year, day.month, day.day, h, m, tzinfo=zone)
        events.append(
            {
                "title": title,
                "place": place,
                "start": start.isoformat(),
                "end": (start + timedelta(minutes=minutes)).isoformat(),
                "all_day": False,
            }
        )
    await engine.db.kv_put(life.CALENDAR_KEY, {"updated": iso(now), "events": events})
    for n, (address, name, subject, category, hours) in enumerate(EMAILS, 1):
        moment = now - timedelta(hours=hours)
        await engine.db.run(
            "INSERT INTO emails(id, thread, received, arrival, from_name, from_address, subject, "
            "start, bulk, category, decision, reason, reviewed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?, 'notice', ?, ?)",
            (
                f"demo{n:04d}",
                f"demo{n:04d}",
                iso(moment),
                int(moment.timestamp()),
                name,
                address,
                subject,
                EXAMPLE_NOTICE,
                category,
                "sender in the list of important domains (example)",
                iso(moment),
            ),
        )
    # The plan limits, as the SDK sends them with each response.
    await engine.db.run(
        "INSERT INTO limits(hour, kind, state, utilization, reset, data) "
        "VALUES (?, 'seven_day', 'allowed', 0.34, ?, ?)",
        (iso(now), iso(now + timedelta(days=3)), json.dumps({"example": True})),
    )
    await engine.db.kv_put("demo", {"created": iso(now), "note": EXAMPLE_NOTICE})


def draw_meal(drawing: str, destination: Path) -> None:
    """A sample photo drawn by code (a plate seen from above), without metadata."""
    from PIL import Image, ImageDraw, ImageFilter

    side = 720
    image = Image.new("RGB", (side, side), (222, 205, 182))
    d = ImageDraw.Draw(image)
    # The table's wood: soft grain.
    for y in range(0, side, 18):
        d.line([(0, y), (side, y + 30)], fill=(212, 193, 168), width=3)
    d.ellipse((70, 80, 670, 680), fill=(200, 186, 170))  # shadow of the plate
    d.ellipse((60, 60, 660, 660), fill=(250, 249, 246), outline=(232, 229, 224), width=6)
    d.ellipse((130, 130, 590, 590), fill=(244, 242, 238))
    if drawing == "avena":
        d.ellipse((150, 150, 570, 570), fill=(226, 205, 163))
        for x, y in ((260, 250), (300, 330), (380, 270), (420, 360), (330, 410), (250, 380)):
            d.ellipse((x, y, x + 34, y + 34), fill=(70, 72, 140))
        for x, y in ((400, 220), (230, 320), (450, 300)):
            d.ellipse((x, y, x + 46, y + 32), fill=(150, 104, 62))
    elif drawing == "salad":
        for x, y, r in ((230, 230, 90), (390, 250, 80), (300, 380, 100), (420, 400, 70)):
            d.ellipse((x - r, y - r, x + r, y + r), fill=(108, 160, 76))
        for x, y in ((260, 300), (350, 260), (330, 420), (420, 330), (240, 410)):
            d.rectangle((x, y, x + 40, y + 40), fill=(246, 244, 230))
        for x in range(200, 460, 26):
            for y in range(320, 360, 14):
                d.ellipse((x, y, x + 10, y + 10), fill=(140, 96, 60))
        d.polygon([(520, 150), (640, 210), (600, 260)], fill=(196, 156, 98))
    else:
        d.ellipse((160, 180, 400, 420), fill=(252, 252, 250), outline=(225, 222, 215), width=8)
        d.ellipse((190, 210, 370, 390), fill=(247, 246, 240))
        d.ellipse((390, 300, 560, 470), fill=(196, 40, 44))
        d.ellipse((420, 320, 470, 360), fill=(226, 92, 84))
        d.line([(475, 300), (480, 268)], fill=(92, 64, 40), width=8)
        d.ellipse((480, 250, 530, 280), fill=(92, 150, 70))
    image = image.filter(ImageFilter.SMOOTH)
    image.save(destination, "JPEG", quality=88)
    destination.chmod(0o600)


async def create_health(engine: Any, today: date) -> None:
    """Sample health: several periods, gym with progress, meals, habits and
    profile. All invented; the photos are drawn by code."""
    from aide.health import cycle, gym, habits, meals
    from aide.health import recommendations as reco

    db = engine.db
    for field, value in HEALTH_PROFILE.items():
        await reco.put(db, field, value, today)
    for back, duration in PERIODS:
        for k in range(duration):
            await cycle.put(db, today - timedelta(days=back - k), "panel")
    for back, text in SESSIONS:
        lines = gym.read(text)
        assert lines is not None and all(x.exercise for x in lines), text
        await gym.note(db, lines, today - timedelta(days=back), "telegram")
    folder = engine.config.paths.photos_health
    folder.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    for n, (kind, drawing, foods) in enumerate(MEALS_TODAY):
        name = f"demo-{n}-{drawing}.jpg"
        draw_meal(drawing, folder / name)
        meal_id = await meals.create(db, kind, [name], today)
        await meals.save_foods(
            db,
            meal_id,
            [
                {
                    "name": a[0],
                    "quantity": a[1],
                    "unit": a[2],
                    "grams": a[3],
                    "kcal": a[4],
                    "protein": a[5],
                    "carbs": a[6],
                    "fats": a[7],
                    "trust": "medium" if i else "high",
                }
                for i, a in enumerate(foods)
            ],
        )
    for back, kcal_meals in WEEK_MEALS:
        day = today - timedelta(days=back)
        for kind, kcal in zip(("breakfast", "lunch", "dinner"), kcal_meals, strict=True):
            meal_id = await meals.create(db, kind, [], day)
            await meals.save_foods(
                db,
                meal_id,
                [
                    {
                        "name": WEEK_NAMES[kind],
                        "quantity": 1,
                        "unit": "serving",
                        "grams": round(kcal / 1.6),
                        "kcal": kcal,
                        "protein": round(kcal * 0.045, 1),
                        "carbs": round(kcal * 0.13, 1),
                        "fats": round(kcal * 0.035, 1),
                        "trust": "medium",
                    }
                ],
            )
    # Water, sleep and steps for the week; today, what the day has so far.
    week = [
        (6, 1750, 7.0, 9200),
        (5, 2000, 6.5, 7400),
        (4, 1500, 7.75, 11050),
        (3, 2250, 7.25, 8300),
        (2, 1750, 8.0, 5600),
        (1, 2000, 6.75, 10400),
        (0, 1250, 7.5, 6150),
    ]
    for back, water, sleep, steps in week:
        day = today - timedelta(days=back)
        for _ in range(water // 250):
            await habits.note(db, "water", 250, day, "telegram")
        await habits.note(db, "sleep", sleep, day, "telegram")
        await habits.note(db, "steps", steps, day, "telegram")
    # Weight, about twice a week for two months.
    weights = [62.9, 62.7, 62.9, 62.6, 62.8, 62.5, 62.6, 62.4, 62.6, 62.3, 62.5, 62.2, 62.4, 62.3]
    for i, value in enumerate(weights):
        await habits.note(
            db, "weight", value, today - timedelta(days=round(60 - i * 4.5)), "telegram"
        )


async def create(folder: Path, name: str = DEFAULT_NAME) -> Any:
    """Creates the sample data and returns the engine opened on it."""
    from aide.bot.channel import FakeChannel
    from aide.common.config import Config
    from aide.common.paths import default_paths
    from aide.core.engine import Engine

    prepare_folders(folder, name)
    paths = default_paths()
    if paths.root.resolve() == PUB_ROOT or paths.data.resolve() == (PUB_ROOT / "data"):
        raise SystemExit("The demo would use the real folders: stopping.")
    paths.ensure()
    paths.profile.write_text(profile_text(name), encoding="utf-8")
    paths.memory.write_text(memory_text(name), encoding="utf-8")
    config = Config.load(paths)
    zone = ZoneInfo(config.time_zone)
    engine = Engine(config, channel=FakeChannel(print_out=False))
    await engine.start(workers=False)
    now = datetime.now(UTC)
    ids = [await create_task(engine, data, now, name) for data in TASKS]
    await engine.memory.propose(f"{name} wants flats with a balcony only", ids[2])
    await create_documents(engine, now.astimezone(zone).date(), now)
    await create_scheduled_tasks(engine, zone, now, last=ids[0])
    await create_life(engine, zone, now)
    project = paths.projects / "budget-sheet"
    project.mkdir(parents=True, exist_ok=True)
    (project / "CONTEXT.md").write_text(
        f"# budget-sheet\nMonthly household budget in a spreadsheet. {EXAMPLE_NOTICE}\n",
        encoding="utf-8",
    )
    (project / "budget.csv").write_text("month,income,spent\n2026-09,3100,2650\n", encoding="utf-8")
    for name, text, file, content in EXTRA_PROJECTS:
        p_folder = paths.projects / name
        p_folder.mkdir(parents=True, exist_ok=True)
        (p_folder / "CONTEXT.md").write_text(
            f"# {name}\n{text} {EXAMPLE_NOTICE}\n", encoding="utf-8"
        )
        (p_folder / file).write_text(content, encoding="utf-8")
    await create_health(engine, now.astimezone(zone).date())
    return engine


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


async def serve(engine: Any, port: int | None) -> None:
    """The panel on the sample data, until Ctrl+C."""
    from aide.panel.app import Panel

    engine.config.environment.PANEL_HOST = "127.0.0.1"
    engine.config.environment.PANEL_PORT = port or free_port()
    engine.config.environment.PANEL_TAILSCALE = None
    panel = Panel(engine, token=secrets.token_urlsafe(24))
    stop = await panel.start()
    print(f"Demo panel: {panel.link('/')}", flush=True)
    try:
        await asyncio.Event().wait()
    finally:
        await stop()


def chosen_name(given: str | None) -> str:
    """The name for the sample data: --name or, in a terminal, what is answered; Alex if none."""
    if given is None and sys.stdin.isatty():
        try:
            given = input(f"Name for the example data [{DEFAULT_NAME}]: ")
        except EOFError:
            given = ""
    return " ".join((given or "").split()) or DEFAULT_NAME


async def run_main(args: argparse.Namespace) -> None:
    folder = safe_folder(Path(args.folder))
    engine = await create(folder, args.name)
    print(f"Example data in {folder}", flush=True)
    try:
        if args.serve:
            await serve(engine, args.port)
    finally:
        await engine.stop()


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Invented example data for the Aide panel")
    parser.add_argument("--folder", default=str(DEFAULT_FOLDER), help="where to create it")
    parser.add_argument("--serve", action="store_true", help="open the panel on the example data")
    parser.add_argument("--port", type=int, default=None, help="panel port (default: a free one)")
    parser.add_argument("--name", help=f"the person's name (default: asks, or {DEFAULT_NAME})")
    args = parser.parse_args(argv)
    sys.path.insert(0, str(PUB_ROOT / "src"))
    try:
        args.name = chosen_name(args.name)
        asyncio.run(run_main(args))
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
