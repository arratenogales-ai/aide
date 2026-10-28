"""Delivery of a task.

The final message is brief: orchestrator text, files from results/ as documents, what failed, the
final folder (copy made by the core), token usage (computed by code) and next step. Texts over
`text_limit` characters are sent in a Word file.

Before sending, the code checks each docx, xlsx, pptx and pdf (that it opens and is not
empty) and moves the .md files from results/ aside to data/drafts/: the draft is not
delivered unless the user asked for Markdown.
"""

from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path

from aide.bot.formatting import basic_markdown_to_html, bold, esc
from aide.common.paths import FINISHED_FOLDER
from aide.core.context import Core
from aide.core.documents import FORMATS, generate_docx, verify
from aide.core.finished import export_task
from aide.core.hooks import note_progress
from aide.core.tasks import Task

log = logging.getLogger(__name__)

IMAGES = {".png", ".jpg", ".jpeg", ".webp", ".gif"}


def extract_block(text: str, label: str) -> str | None:
    m = re.search(rf"<{label}>\s*(.*?)\s*</{label}>", text or "", re.DOTALL | re.IGNORECASE)
    return m.group(1).strip() if m else None


def separate_delivery(text: str) -> tuple[str, str]:
    """Returns (delivery, summary). Without blocks, the delivery is the text and the summary its
    first lines."""
    delivery = extract_block(text, "delivery")
    summary = extract_block(text, "summary")
    if delivery is None:
        delivery = re.sub(r"<summary>.*?</summary>", "", text or "", flags=re.DOTALL).strip()
    if not summary:
        lines = [line.strip() for line in delivery.splitlines() if line.strip()]
        summary = "\n".join(lines[:5])
    return delivery, summary


RE_ASKS_MARKDOWN = re.compile(r"\b(markdown|\.?md)\b", re.IGNORECASE)


def reserve_drafts(folder: Path, request: str) -> list[Path]:
    """Moves the .md files from results/ to data/drafts/, unless Markdown was requested."""
    if RE_ASKS_MARKDOWN.search(request or ""):
        return []
    results = folder / "results"
    if not results.is_dir():
        return []
    moved = []
    for md in sorted(results.rglob("*.md")):
        if not md.is_file():
            continue
        destination = folder / "data" / "drafts" / md.relative_to(results)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists():
            destination = destination.with_name(
                f"{destination.stem} (deliverable){destination.suffix}"
            )
        md.rename(destination)
        moved.append(destination)
    return moved


def result_files(folder: Path) -> list[Path]:
    """Returns the sorted non-hidden files under the results folder."""
    results = folder / "results"
    if not results.is_dir():
        return []
    return sorted(p for p in results.rglob("*") if p.is_file() and not p.name.startswith("."))


INJECTION_NOTICE = (
    "Heads up: some external content in this task had text that looked like instructions for me. "
    "The details are in the panel."
)


async def deliver(
    core: Core,
    task: Task,
    final_text: str,
    *,
    review_failures: list[str] | None = None,
) -> Task:
    """Sends the delivery to the user and saves result, summary and history. Does not change the
    status."""
    folder = core.paths.task(task.id)
    delivery, summary = separate_delivery(final_text)
    sections = reserve_drafts(folder, task.request)
    if sections:
        await core.tasks.event(
            task.id,
            "draft_reserved",
            ", ".join(p.name for p in sections),
            {"paths": [str(p) for p in sections]},
        )
    files = result_files(folder)
    # Each final document is checked by code before being sent.
    problems = []
    for file in files:
        if file.suffix.lower().lstrip(".") in FORMATS:
            v = await asyncio.to_thread(verify, file)
            await core.tasks.event(
                task.id, "verification", v.text(), {"path": str(file), "ok": v.ok}
            )
            if not v.ok:
                problems.append(v.text())
    for file in files:
        await core.tasks.log_file(task.id, file, "result")
    usage = await core.usage.task_text(task.id)

    parts = [bold(f"Task {task.id} done"), basic_markdown_to_html(delivery or "Done.")]
    if review_failures:
        items = "\n".join(f"- {f}" for f in review_failures)
        parts.append(bold("The independent review found unresolved issues") + "\n" + esc(items))
    if problems:
        parts.append(
            bold("Heads up: a file did not pass the check")
            + "\n"
            + esc("\n".join(f"- {p}" for p in problems))
        )
    # External content with instructions: the user knows it even if the model does not say so,
    # also if the delivery is long and goes in a Word file.
    injection_notice = (
        INJECTION_NOTICE if await core.tasks.events(task.id, ["possible_injection"]) else None
    )
    if injection_notice:
        parts.append(esc(injection_notice))
    if files:
        parts.append(esc("Files: " + ", ".join(a.name for a in files)))

    channel = core.channel
    limit = core.config.settings.telegram.text_limit
    # A long message goes as a file to results/, before copying them to the final folder (the
    # margin covers the folder line and the usage line).
    long: Path | None = None
    if channel is not None and len("\n\n".join(parts)) + 300 > limit:
        long = folder / "results" / "delivery.docx"
        long_text = delivery + "".join(f"\n\n- {f}" for f in review_failures or [])
        if injection_notice:
            long_text += f"\n\n{injection_notice}"
        await asyncio.to_thread(generate_docx, long_text, long)
        await core.tasks.log_file(task.id, long, "result")

    final = await export_task(core.tasks, core.paths, task, core.config.time_zone)
    closing = [esc(f"Saved in {FINISHED_FOLDER}/{final.name}.")] if final else []
    closing.append(esc(usage))
    message = "\n\n".join(parts + closing)

    if channel is not None:
        if long is not None:
            short = "\n\n".join(
                [
                    bold(f"Task {task.id} done"),
                    esc("This message is long, so I'm sending it to you as a Word file."),
                    *([esc(injection_notice)] if injection_notice else []),
                    *closing,
                ]
            )
            message_id = await channel.send(short)
            await core.tasks.link_message(message_id, task.id, "delivery")
            files = [long, *[a for a in files if a != long]]
        else:
            message_id = await channel.send(message)
            await core.tasks.link_message(message_id, task.id, "delivery")
        for file in files[:20]:
            try:
                if file.suffix.lower() in IMAGES:
                    mid = await channel.send_image(file, file.name)
                else:
                    mid = await channel.send_file(file, file.name)
                await core.tasks.link_message(mid, task.id, "delivery")
            except Exception:
                log.exception("could not send %s", file)

    task = await core.tasks.update(task.id, result=delivery, summary=summary)
    await core.tasks.index_history(task, [str(a) for a in files])
    await core.tasks.event(task.id, "delivery", delivery[:1000], {"files": [str(a) for a in files]})
    note_progress(folder, "Delivery sent.")
    return task
