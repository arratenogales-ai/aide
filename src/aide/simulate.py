"""Telegram simulator.

It follows the same path as Telegram with a fake channel and answers by script to the
approvals, plans, questions and caps:

    uv run python -m aide.simulate "message" [options]

Examples:
    uv run python -m aide.simulate "Create a file hello.txt"
    uv run python -m aide.simulate "Draft three emails" --approve batch:everything
    uv run python -m aide.simulate "Research X" --plan change:"only two sources" --plan approve
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from aide.bot.channel import Entry, FakeChannel, OutgoingMessage
from aide.core.states import TaskState

SIMULATED_USER = 0


@dataclass
class Script:
    """A scripted set of answers for the simulator: approvals, plans, replies and defaults."""

    approve: deque[str] = field(
        default_factory=deque
    )  # yes | no | batch:all | batch:one | batch:cancel
    plan: deque[str] = field(default_factory=deque)  # approve | cancel | change:text
    replies: deque[str] = field(default_factory=deque)  # label, number or free text
    cap: deque[str] = field(default_factory=deque)  # mas | no
    memory: deque[str] = field(default_factory=deque)  # yes | no (memory proposals)
    default_memory: str = "no"
    default_approve: str = "no"
    default_plan: str = "approve"
    pressed: list[str] = field(default_factory=list)


class Simulator:
    """A simulator of the Telegram chat that plays a script of answers against the engine."""

    def __init__(self, engine, script: Script, print_out: bool = True) -> None:
        self.engine = engine
        self.script = script
        self.channel = FakeChannel(print_out=print_out, on_send=self._on_send)
        engine.channel = self.channel
        self._pending: set[asyncio.Task] = set()

    def _press(self, datum: str) -> None:
        self.script.pressed.append(datum)
        print(f"   >> taps [{datum}]")
        t = asyncio.create_task(
            self.engine.receive(Entry("button", SIMULATED_USER, button_datum=datum))
        )
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    def _write(self, text: str, replies_to: int | None = None) -> None:
        print(f"   >> types “{text}”")
        t = asyncio.create_task(
            self.engine.receive(Entry("text", SIMULATED_USER, text=text, replies_to=replies_to))
        )
        self._pending.add(t)
        t.add_done_callback(self._pending.discard)

    async def _on_send(self, m: OutgoingMessage) -> None:
        data = m.buttons_data
        if not data:
            return
        first = data[0]
        kind = first.split(":")[0]
        if kind == "apr":
            decision = (
                self.script.approve.popleft()
                if self.script.approve
                else self.script.default_approve
            )
            decision = "yes" if decision.startswith("yes") else "no"
            self._press(next(d for d in data if d.endswith(f":{decision}")))
        elif kind == "batch":
            decision = self.script.approve.popleft() if self.script.approve else "batch:cancel"
            choice = (
                decision.split(":", 1)[1]
                if decision.startswith("batch:")
                else ("everything" if decision == "yes" else "cancel")
            )
            self._press(next(d for d in data if d.endswith(f":{choice}")))
        elif kind == "plan":
            decision = self.script.plan.popleft() if self.script.plan else self.script.default_plan
            if decision == "nothing":
                return
            if decision.startswith("change:"):
                self._press(next(d for d in data if d.endswith(":change")))
                await asyncio.sleep(0.2)
                self._write(decision.split(":", 1)[1])
            elif decision == "cancel":
                self._press(next(d for d in data if d.endswith(":no")))
            else:
                self._press(next(d for d in data if d.endswith(":yes")))
        elif kind == "sched":
            reply = self.script.replies.popleft() if self.script.replies else "1"
            options = [d for d in data if d.rsplit(":", 1)[1].isdigit()]
            labels = {}
            for row in m.buttons or []:
                for b in row:
                    labels[b.text.lower().lstrip("✓ ")] = b.datum
            if reply.isdigit() and 1 <= int(reply) <= len(options):
                self._press(options[int(reply) - 1])
            elif reply.lower() in labels:
                self._press(labels[reply.lower()])
            else:
                self._press(next(d for d in data if d.endswith(":other")))
                await asyncio.sleep(0.2)
                self._write(reply)
        elif kind == "mem":
            decision = (
                self.script.memory.popleft() if self.script.memory else self.script.default_memory
            )
            self._press(next(d for d in data if d.endswith(":yes" if decision == "yes" else ":no")))
        elif kind == "cap":
            decision = self.script.cap.popleft() if self.script.cap else "no"
            self._press(
                next(d for d in data if d.endswith(":more" if decision == "more" else ":no"))
            )

    async def send(
        self,
        text: str,
        *,
        replies_to_task: int | None = None,
        files: list[Path] | None = None,
        voice: Path | None = None,
        photo: Path | None = None,
    ) -> list[int]:
        """Sends a message and waits for everything it triggers to finish. Returns the affected
        tasks."""
        db = self.engine.db
        before = await db.value("SELECT COALESCE(MAX(id), 0) FROM tasks") or 0
        replies_to = None
        if replies_to_task:
            replies_to = await db.value(
                "SELECT message_id FROM telegram_messages WHERE task_id=? ORDER BY hour DESC LIMIT 1",
                (replies_to_task,),
            )
            if replies_to is None:
                replies_to = 900_000 + replies_to_task
                await self.engine.tasks.link_message(replies_to, replies_to_task, "output")
        print(f"\n[USER] {text}")
        if voice is not None:
            entry = Entry("voice", SIMULATED_USER, files=[voice], replies_to=replies_to)
        elif photo is not None:
            # Like a Telegram photo: the text is the caption (it may be empty).
            entry = Entry("image", SIMULATED_USER, text=text, replies_to=replies_to, files=[photo])
        elif files:
            entry = Entry(
                "document",
                SIMULATED_USER,
                text=text,
                replies_to=replies_to,
                files=list(files),
            )
        else:
            entry = Entry("text", SIMULATED_USER, text=text, replies_to=replies_to)
        await self.engine.receive(entry)
        await self.wait_rest()
        after = await db.value("SELECT COALESCE(MAX(id), 0) FROM tasks") or 0
        affected = list(range(before + 1, after + 1))
        if replies_to_task:
            affected.append(replies_to_task)
        return affected

    async def wait_rest(self, max_wait: float = 3600) -> None:
        """Waits until there is no work in progress or buttons left to press."""
        loop = asyncio.get_running_loop()
        limit = loop.time() + max_wait
        still = 0
        while loop.time() < limit:
            busy = bool(
                self._pending
                or self.engine.in_progress
                or self.engine._planning
                or self.engine._quick
                or not self.engine.queue.empty()
                or self.engine._in_queue
            )
            active = await self.engine.db.value(
                "SELECT COUNT(*) FROM tasks WHERE state IN (?, ?, ?, ?, ?, ?)",
                (
                    TaskState.RECEIVED,
                    TaskState.IN_TRIAGE,
                    TaskState.PLANNING,
                    TaskState.IN_QUEUE,
                    TaskState.RUNNING,
                    TaskState.REVIEWING,
                ),
            )
            if not busy and not active:
                still += 1
                if still >= 3:
                    return
            else:
                still = 0
            await asyncio.sleep(0.5)
        raise TimeoutError("the simulator did not settle in time")


async def task_summary(engine, task_id: int) -> dict:
    """Summarizes a task: its state, files, events, transitions and usage."""
    task = await engine.tasks.require(task_id)
    files = [a["path"] for a in await engine.tasks.files(task_id)]
    usage = await engine.usage.by_task(task_id)
    return {
        "task": task_id,
        "kind": task.kind,
        "state": task.state,
        "files": files,
        "events": len(await engine.tasks.events(task_id)),
        "transitions": [t["until"] for t in await engine.tasks.transitions(task_id)],
        "usage": {
            m: {"tokens": d["consumption"].total, "turns": d["turns"]} for m, d in usage.items()
        },
        "tokens": sum(d["consumption"].total for d in usage.values()),
        "turns_used": task.turns_used,
    }


async def run_main(args: argparse.Namespace) -> int:
    """Runs the simulator with the parsed arguments and returns an exit code."""
    from aide.application import configure_engine
    from aide.core.engine import Engine
    from aide.main import prepare

    config, report = prepare(models_file=args.models, console=args.log)
    for notice in report.notices():
        print(f"WARNING: {notice}")
    engine = Engine(config)
    script = Script(
        approve=deque(args.approve or []),
        plan=deque(args.plan or []),
        replies=deque(args.reply or []),
        cap=deque(args.cap or []),
        memory=deque(args.memory or []),
        default_approve=args.default_approve,
    )
    simulator = Simulator(engine, script)
    await engine.start()
    await configure_engine(engine, simulated=True)
    code = 0
    try:
        if args.start:
            # As at program startup: resumes what was left half done and launches the
            # scheduled tasks.
            print("\n[STARTUP] resuming what was pending")
            affected = await engine.recover_to_start()
            affected += await engine.check_scheduled_tasks()
            await simulator.wait_rest()
        elif args.size:
            # Direct task of that size, without triage (for example, medium so that it
            # goes through review).
            print(f"\n[USER] {args.message}")
            task = await engine.create_direct_task(args.message, size=args.size)
            await simulator.wait_rest()
            affected = [task.id]
        else:
            affected = await simulator.send(
                args.message,
                replies_to_task=args.replies_to,
                files=[Path(a).resolve() for a in args.file or []],
                voice=Path(args.voice).resolve() if args.voice else None,
                photo=Path(args.photo).resolve() if args.photo else None,
            )
        print("\n=== SUMMARY ===")
        for task_id in affected:
            data = await task_summary(engine, task_id)
            print(json.dumps(data, ensure_ascii=False, indent=1))
            if (
                data["state"] not in (TaskState.COMPLETED, TaskState.CANCELLED)
                and data["kind"] == "task"
            ):
                code = 1
    finally:
        await engine.stop()
    return code


def main(argv: list[str] | None = None) -> None:
    """Command line entry point of the Telegram simulator."""
    parser = argparse.ArgumentParser(prog="simular", description="Aide Telegram simulator")
    parser.add_argument("message", nargs="?", default="", help="text the user sends")
    parser.add_argument(
        "--start",
        action="store_true",
        help="no message: start up, resume what was left halfway (as after an outage) and wait",
    )
    parser.add_argument(
        "--models",
        default=None,
        help="models file in config/ (for example models.test.yaml)",
    )
    parser.add_argument(
        "--approve",
        action="append",
        help="answer to each approval: yes, no, batch:everything (all), batch:one (one by one), batch:cancel (cancel)",
    )
    parser.add_argument("--default-approve", default="no", choices=["yes", "no"])
    parser.add_argument(
        "--plan",
        action="append",
        help="approve, cancel or change:text, in order",
    )
    parser.add_argument(
        "--reply", action="append", help="answer to each question (number, label or text)"
    )
    parser.add_argument("--cap", action="append", help="more or no, in order")
    parser.add_argument(
        "--memory", action="append", help="yes or no to each memory proposal, in order"
    )
    parser.add_argument(
        "--replies-to", type=int, default=None, help="reply to a message from this task"
    )
    parser.add_argument("--file", action="append", help="attach a file")
    parser.add_argument("--voice", default=None, help="send a voice note (audio file)")
    parser.add_argument("--photo", default=None, help="send a photo (the message is the caption)")
    parser.add_argument(
        "--size",
        choices=["small", "medium", "large"],
        default=None,
        help="create the task directly with that size (small, medium or large), without triage",
    )
    parser.add_argument("--log", action="store_true", help="show the log in the console")
    args = parser.parse_args(argv)
    if not args.message and not args.start and not args.voice and not args.photo:
        parser.error("the message is missing (or use --start)")
    sys.exit(asyncio.run(run_main(args)))


if __name__ == "__main__":
    main()
