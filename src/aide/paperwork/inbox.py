"""The document inbox.

A folder on the Mac, “Document Inbox”, next to “Finished Tasks”, where the user drags
documents from Finder: PDFs, photos or Word files, several at once or in subfolders.
With /documents or the panel button, the code counts the new files and calculates how
many tokens reading them will use; with their “Read them”, the model reads each one
once, like those that arrive by Telegram, and a single message arrives with what each
one is and its dates, with “Save all”, “Review one by one” and “Discard”. When saving
them, each file leaves the inbox for the protected folder (data/documents) and the inbox
is left empty. What cannot be read stays in the inbox and the user is told which. A file
already read (by its SHA-256) is not read again, even if it is discarded.

Duplicates: what is already saved (the same file or a PDF with the same text) or
what is in the inbox twice is not read. A scan that, once read, looks like one already
saved is flagged and is not included in “Save all”. If the plan limit is reached, it
stops and the user is told, with the time it resets.

The inbox empties itself: duplicates, what is discarded and what had already
been discarded go to the Trash (from where it can be recovered), as do subfolders left
with nothing. Only what could not be read or is not known how to read stays, and the
user is told.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aide.bot.channel import Button
from aide.bot.formatting import bold, esc
from aide.common.files import CONVERTIBLE, IMAGES, Unreadable
from aide.core.call import NoReply
from aide.paperwork import duplicates, flow, reading, registry, reminders, short_date

if TYPE_CHECKING:
    from aide.core.engine import Engine

log = logging.getLogger(__name__)

READABLE = {".pdf"} | IMAGES | CONVERTIBLE | reading.OFFICE
BATCH_KEY = "inbox:batch:"
NEXT_KEY = "inbox:next_step"
# Tokens per read, measured with those of 10-26 (a PDF with text, about 10 thousand; a scanned one,
# about 18 thousand): the fixed cost of each call, each page sent as an image and a Word file.
TOKENS_BASE = 8_000
TOKENS_BY_IMAGE = 3_500
TOKENS_WORD = 12_000
# A Telegram message cannot exceed 4,096 characters: if the list is longer, it goes in several.
MAX_MESSAGE = 3_800


# Uploading from the panel: at most 20 files at a time and 20 MB each.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024
MAX_UPLOAD_FILES = 20


def _safe_name(original: str) -> str:
    """The name of an uploaded file, without folders, control characters or a leading dot."""
    name = original.replace("\\", "/").rsplit("/", 1)[-1]
    name = "".join(c for c in name if c.isprintable()).strip().lstrip(". ")
    path = Path(name or "document")
    return path.stem[:120] + path.suffix.lower()


def save_uploaded(inbox: Path, files: list[Any]) -> tuple[list[Path], list[str]]:
    """Files uploaded in the panel go to the inbox. Only the types that can be read, up to 20
    MB and 20 at a time; if one with that name already exists, “(2)” is added. Returns what
    was saved and, for what was not, the name and the reason. It never leaves the inbox (the
    name has no folders)."""
    inbox.mkdir(parents=True, exist_ok=True)
    saved: list[Path] = []
    rejected: list[str] = []
    for i, file in enumerate(files):
        name = _safe_name(str(getattr(file, "filename", "") or ""))
        if i >= MAX_UPLOAD_FILES:
            rejected.append(f"“{name}” (more than {MAX_UPLOAD_FILES} at once)")
            continue
        if Path(name).suffix not in READABLE:
            rejected.append(f"“{name}” (a type I can't read)")
            continue
        destination = inbox / name
        n = 2
        while destination.exists():
            destination = inbox / f"{Path(name).stem} ({n}){Path(name).suffix}"
            n += 1
        written = 0
        too_much = False
        with destination.open("xb") as output:
            while chunk := file.file.read(1024 * 1024):
                written += len(chunk)
                if written > MAX_UPLOAD_BYTES:
                    too_much = True
                    break
                output.write(chunk)
        if too_much or written == 0:
            destination.unlink(missing_ok=True)
            rejected.append(f"“{name}” ({'more than 20 MB' if too_much else 'empty'})")
            continue
        saved.append(destination)
    return saved, rejected


def _relative(engine: Engine, path: Path) -> str:
    return str(path.relative_to(engine.config.paths.inbox))


def _visible(folder: Path) -> list[Path]:
    """The files in the inbox and its subfolders, without hidden ones (.DS_Store and similar)."""
    if not folder.is_dir():
        return []
    output = []
    for path in sorted(folder.rglob("*")):
        parts = path.relative_to(folder).parts
        if path.is_file() and not any(p.startswith(".") for p in parts):
            output.append(path)
    return output


# The macOS command that moves to the Trash, from where it can be recovered with Finder.
TRASH = "/usr/bin/trash"


def to_trash(path: Path) -> bool:
    """Moves a file or a folder to the Trash. False if it could not: it stays where it was."""
    binary = TRASH if Path(TRASH).exists() else shutil.which("trash")
    if not binary:
        log.warning("cannot find the trash command to move %s to the Trash", path.name)
        return False
    try:
        subprocess.run([binary, str(path)], check=True, capture_output=True, timeout=60)
    except (subprocess.SubprocessError, OSError) as e:
        log.warning("could not move %s to the Trash: %s", path.name, e)
        return False
    return not path.exists()


def _empty_folders(inbox: Path) -> None:
    """The inbox subfolders with nothing visible (only .DS_Store and similar) go to the Trash,
    from the deepest to the first; the inbox itself stays."""
    if not inbox.is_dir():
        return
    subfolders = sorted(
        (p for p in inbox.rglob("*") if p.is_dir()), key=lambda p: len(p.parts), reverse=True
    )
    for folder in subfolders:
        if folder.exists() and not _visible(folder):
            to_trash(folder)


async def discard(engine: Engine, paths: list[Path]) -> list[Path]:
    """Moves these inbox files to the Trash (only if they are inside) and the subfolders that
    are left empty. Returns those that could not be moved."""
    folder = engine.config.paths.inbox.resolve()
    failed = []
    for path in paths:
        real = path.resolve()
        if real == folder or not real.is_relative_to(folder) or not real.exists():
            continue
        if not await asyncio.to_thread(to_trash, real):
            failed.append(path)
    await asyncio.to_thread(_empty_folders, folder)
    return failed


def file_in_inbox(engine: Engine, d: dict[str, Any]) -> Path | None:
    """The file of a document that is still in the inbox, or None."""
    if not d.get("inbox"):
        return None
    folder = engine.config.paths.inbox.resolve()
    path = (folder / str(d["inbox"])).resolve()
    if path == folder or not path.is_relative_to(folder) or not path.is_file():
        return None
    return path


@dataclass
class Review:
    """What is in the inbox: what is read and the duplicates, which is not (with the reason)."""

    new: list[tuple[Path, str]] = field(default_factory=list)
    duplicates: list[tuple[Path, str]] = field(default_factory=list)


async def review(engine: Engine) -> Review:
    """The inbox files that have never been read, with their fingerprint, and the duplicates
    : the same file as one already saved or as another in the inbox, or a PDF with
        the same text, and what was already discarded. What has been read and awaits a
        decision is not touched."""
    read = {
        f["fingerprint"]: f["state"]
        for f in await engine.db.everything(
            "SELECT fingerprint, state FROM documents WHERE fingerprint IS NOT NULL ORDER BY id"
        )
    }
    saved = await duplicates.saved(engine)
    review_result = Review()
    seen: dict[str, Path] = {}
    texts: list[tuple[str, Path]] = []
    for path in _visible(engine.config.paths.inbox):
        try:
            h = await asyncio.to_thread(flow.fingerprint, path)
        except OSError:
            continue
        equal = duplicates.by_fingerprint(h, saved)
        if equal is not None:
            review_result.duplicates.append(
                (path, f'it\'s the same file as "{equal["title"]}", already saved')
            )
            continue
        if h in seen:
            review_result.duplicates.append(
                (path, f'it\'s the same file as "{_relative(engine, seen[h])}"')
            )
            continue
        seen[h] = path
        if h in read:
            if read[h] == "discarded":
                review_result.duplicates.append((path, "you already discarded it"))
            continue
        if path.suffix.lower() == ".pdf":
            text = await asyncio.to_thread(reading.pdf_text, path)
            equal = duplicates.by_text(text, saved)
            if equal is not None:
                review_result.duplicates.append(
                    (path, f'it has the same text as "{equal["title"]}", already saved')
                )
                continue
            other = next((r for t, r in texts if duplicates.same_text(text, t)), None)
            if other is not None:
                review_result.duplicates.append(
                    (path, f'it has the same text as "{_relative(engine, other)}"')
                )
                continue
            if len(text) >= reading.MIN_PDF_TEXT:
                texts.append((text, path))
        review_result.new.append((path, h))
    return review_result


async def new(engine: Engine) -> list[tuple[Path, str]]:
    """The inbox files that would be read now, with their fingerprint."""
    return (await review(engine)).new


def _duplicates_text(engine: Engine, items: list[tuple[Path, str]], failed: list[Path]) -> str:
    """“I moved 2 duplicate files to the Trash: "a" (…) and "b" (…).”, and those that could not
    be."""
    if not items:
        return ""
    no = set(failed)
    discarded = [(r, reason) for r, reason in items if r not in no]
    phrases = []
    if discarded:
        parts = [f'"{_relative(engine, r)}" ({reason})' for r, reason in discarded[:8]]
        if len(discarded) > 8:
            parts.append(f"{len(discarded) - 8} more")
        if len(discarded) == 1:
            phrases.append(f"I moved a duplicate file to the Trash: {parts[0]}.")
        else:
            phrases.append(
                f"I moved {len(discarded)} duplicate files to the Trash: {_items(parts)}."
            )
    if no:
        names = _items([f'"{_relative(engine, r)}"' for r in failed[:8]])
        phrases.append(
            f"I couldn't move {names} to the Trash: please delete "
            f"{'it' if len(failed) == 1 else 'them'} from the inbox yourself."
        )
    return " ".join(phrases)


def tokens_of(path: Path) -> int:
    """How many tokens I estimate reading this file will use (0 if it is not known how to
    read)."""
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        try:
            text = reading.pdf_text(path)
        except Exception:
            text = ""
        if len(text) >= reading.MIN_PDF_TEXT:
            return TOKENS_BASE + min(len(text), reading.MAX_TEXT) // 4
        try:
            from pypdf import PdfReader

            pages = len(PdfReader(str(path)).pages)
        except Exception:
            pages = reading.MAX_IMAGE_PAGES
        return TOKENS_BASE + min(max(pages, 1), reading.MAX_IMAGE_PAGES) * TOKENS_BY_IMAGE
    if suffix in IMAGES | CONVERTIBLE:
        return TOKENS_BASE + TOKENS_BY_IMAGE
    if suffix in reading.OFFICE:
        return TOKENS_WORD
    return 0


def _thousands(tokens: int) -> str:
    if tokens >= 1_000_000:
        return f"{tokens / 1_000_000:.1f} million"
    return f"{max(1, round(tokens / 1000))} thousand"


def _plural(n: int, one: str, several: str) -> str:
    return f"{n} {one if n == 1 else several}"


def _items(parts: list[str]) -> str:
    return parts[0] if len(parts) == 1 else ", ".join(parts[:-1]) + " and " + parts[-1]


def _kinds(paths: list[Path]) -> str:
    """“3 PDFs, 2 photos and 1 Word file”."""
    account = {"PDF": 0, "photos": 0, "Word": 0}
    for r in paths:
        s = r.suffix.lower()
        if s == ".pdf":
            account["PDF"] += 1
        elif s in IMAGES | CONVERTIBLE:
            account["photos"] += 1
        else:
            account["Word"] += 1
    parts = []
    if account["PDF"]:
        parts.append(_plural(account["PDF"], "PDF", "PDFs"))
    if account["photos"]:
        parts.append(_plural(account["photos"], "photo", "photos"))
    if account["Word"]:
        parts.append(_plural(account["Word"], "Word file", "Word files"))
    return _items(parts) if parts else ""


async def ask(engine: Engine) -> str:
    """The first step of /documents: how many new files there are and how many tokens reading
    them would use, with “Read them” and “Not now”. Returns the text it sends (for the
    panel)."""
    review_result = await review(engine)
    files = review_result.new
    readable = [r for r, _h in files if r.suffix.lower() in READABLE]
    rare = [r for r, _h in files if r.suffix.lower() not in READABLE]
    failed = await discard(engine, [r for r, _m in review_result.duplicates])
    repeats = _duplicates_text(engine, review_result.duplicates, failed)
    if not files and repeats:
        text = f"There are no new documents in the inbox. {repeats}"
        await engine.notify(None, text)
        return text
    if not files:
        text = (
            'There are no new files in the inbox. Drag your documents into the "'
            f'{engine.config.paths.inbox.name}" folder, next to "Finished Tasks", and send me '
            "/documents again."
        )
        await engine.notify(None, text)
        return text
    tokens = sum(await asyncio.gather(*(asyncio.to_thread(tokens_of, r) for r in readable)))
    parts = []
    if readable:
        parts.append(
            f"There {'is' if len(readable) == 1 else 'are'} "
            f"{_plural(len(readable), 'new document', 'new documents')} in the inbox "
            f"({_kinds(readable)}). Reading {'it' if len(readable) == 1 else 'them'} will use "
            f"about {_thousands(tokens)} tokens."
        )
    if rare:
        names = [f'"{_relative(engine, r)}"' for r in rare[:5]]
        if len(rare) > 5:
            names.append(f"{len(rare) - 5} more")
        one = len(rare) == 1
        parts.append(
            f"I can't read {_items(names)}: {'it stays' if one else 'they stay'} in the inbox. "
            f"Save {'it as a PDF' if one else 'them as PDFs'} and I'll read "
            f"{'it' if one else 'them'}."
        )
    if repeats:
        parts.append(repeats)
    if not readable:
        text = " ".join(parts)
        await engine.notify(None, text)
        return text
    text = " ".join(parts) + (" Shall I read it?" if len(readable) == 1 else " Shall I read them?")
    await engine.notify(
        None,
        text,
        buttons=[[Button("Read them", "inbox:read:0"), Button("Not now", "inbox:nothing:0")]],
    )
    return text


def _line(i: int, d: dict[str, Any], dates: list[dict[str, Any]]) -> str:
    line = f"{i}. {d.get('title') or 'Document'} ({flow.detail_of(d)})."
    if dates:
        parts = [
            f"on {short_date(date.fromisoformat(f['date']))} {f['description']}" for f in dates[:3]
        ]
        line += " " + _items(parts)[0].upper() + _items(parts)[1:] + "."
    else:
        line += " No dates to keep an eye on."
    if d.get("inbox"):
        line += f' File: "{d["inbox"]}".'
    return line


async def read_all(engine: Engine) -> None:
    """With “Read them”: the model reads each new file once, and at the end a single message
    with what each one is, its dates and the buttons."""
    db = engine.db
    async with db.lock("inbox"):
        review_result = await review(engine)
        readable = [(r, h) for r, h in review_result.new if r.suffix.lower() in READABLE]
        failed = await discard(engine, [r for r, _m in review_result.duplicates])
        repeats = _duplicates_text(engine, review_result.duplicates, failed)
        if not readable:
            await engine.notify(
                None, f"There are no new documents to read in the inbox. {repeats}".strip()
            )
            return
        await engine.notify(
            None,
            f"Reading {_plural(len(readable), 'document', 'documents')} from the inbox. I'll "
            "write when I'm done.",
        )
        start = datetime.now(UTC).isoformat(timespec="seconds")
        read: list[int] = []
        similar: dict[int, str] = {}
        unreadable: list[tuple[str, str]] = []
        for i, (path, h) in enumerate(readable):
            rel = _relative(engine, path)
            try:
                document_id = await flow.create_from(
                    engine, path, "", path.name, rel_file="", rel_inbox=rel, file_fingerprint=h
                )
            except Unreadable as e:
                unreadable.append((rel, str(e)))
                continue
            except NoReply as e:
                unreadable.append((rel, flow.why(engine, e)))
                if e.reason == "limit":
                    # With the plan limit reached, the rest cannot be read
                    # either.
                    unreadable += [
                        (_relative(engine, r), "I didn't get to read it")
                        for r, _h in readable[i + 1 :]
                    ]
                    break
                continue
            except Exception as e:
                log.exception("could not read a document from the inbox")
                unreadable.append((rel, f"reading it failed ({type(e).__name__})"))
                continue
            read.append(document_id)
            equal = await duplicates.same_document(engine, document_id)
            if equal is not None:
                similar[document_id] = str(equal.get("title") or "another document")
        spent = await db.value(
            "SELECT SUM(input_tokens + output_tokens + cache_read + cache_creation) FROM usage "
            "WHERE origin='documents' AND hour >= ?",
            (start,),
        )
        await _summary(engine, read, similar, unreadable, repeats, int(spent or 0))


async def _summary(
    engine: Engine,
    read: list[int],
    similar: dict[int, str],
    unreadable: list[tuple[str, str]],
    repeats: str,
    spent: int,
) -> None:
    db = engine.db
    lines: list[str] = []
    for i, document_id in enumerate(read, 1):
        d = await registry.get(db, document_id)
        if d is None:
            continue
        line = _line(i, d, await registry.dates_of(db, document_id))
        if document_id in similar:
            line += (
                f' It looks like the same one as "{similar[document_id]}", already saved: '
                'it\'s left out of "Save all".'
            )
        lines.append(line)
    header = (
        f"I read {_plural(len(read), 'document', 'documents')} from the inbox "
        f"(about {_thousands(spent)} tokens)."
        if read
        else "I couldn't read any document from the inbox."
    )
    caption = ""
    if unreadable:
        caption = (
            "I couldn't read these, which are still in the inbox: "
            + "; ".join(f'"{rel}" ({reason.rstrip(".")})' for rel, reason in unreadable)
            + "."
        )
    caption = f"{caption} {repeats}".strip()
    if not read:
        await engine.notify(None, f"{header} {caption}".strip())
        return
    n = int(await db.kv_read(NEXT_KEY) or 0) + 1
    await db.kv_put(NEXT_KEY, n)
    await db.kv_put(
        f"{BATCH_KEY}{n}",
        {"documents": read, "similar": list(similar), "state": "pending"},
    )
    buttons = [
        [Button("Save all", f"inbox:sav:{n}")],
        [Button("Review one by one", f"inbox:rev:{n}"), Button("Discard", f"inbox:des:{n}")],
    ]
    # The list, in one or several messages; the buttons, in the last one.
    chunks: list[str] = []
    current = bold(header)
    for line in [*lines, caption]:
        if not line:
            continue
        if len(current) + len(line) + 1 > MAX_MESSAGE:
            chunks.append(current)
            current = ""
        current = f"{current}\n{esc(line)}" if current else esc(line)
    chunks.append(current)
    for chunk in chunks[:-1]:
        await engine.notify(None, chunk, html=True)
    await engine.notify(None, chunks[-1], html=True, buttons=buttons)


async def take_to_documents(engine: Engine, d: dict[str, Any]) -> bool:
    """Takes a document out of the inbox to the protected folder when saving it; subfolders
    left empty go to the Trash (the inbox itself does not). False if the file is no longer
    in the inbox."""
    inbox_folder = engine.config.paths.inbox.resolve()
    origin = (inbox_folder / str(d.get("inbox") or "")).resolve()
    if not origin.is_relative_to(inbox_folder) or origin == inbox_folder or not origin.is_file():
        return False
    documents = engine.config.paths.documents
    destination = await asyncio.to_thread(_move_from_inbox, origin, documents)
    await registry.update(
        engine.db, int(d["id"]), file=str(destination.relative_to(documents)), inbox=None
    )
    await asyncio.to_thread(_empty_folders, inbox_folder)
    return True


def _move_from_inbox(origin: Path, documents: Path) -> Path:
    import shutil

    moment = datetime.now()
    folder = documents / f"{moment:%Y}"
    folder.mkdir(parents=True, exist_ok=True)
    documents.chmod(0o700)
    folder.chmod(0o700)
    destination = folder / f"{moment:%Y%m%d-%H%M%S-%f}-{flow.safe_name(origin.name)}"
    shutil.move(str(origin), destination)
    destination.chmod(0o600)
    return destination


async def button(engine: Engine, action: str, n: int) -> None:
    """“Read them” and “Not now” at the start; “Save all”, “Review one by one” and “Discard” at
    the end. Each batch is resolved once."""
    db = engine.db
    if action == "read":
        engine.launch(read_all(engine))
        return
    if action == "nothing":
        await engine.notify(
            None, "OK, I won't read them. They stay in the inbox for when you want."
        )
        return
    async with db.lock("inbox"):
        batch = await db.kv_read(f"{BATCH_KEY}{n}")
        if not batch or batch.get("state") != "pending":
            await engine.notify(None, "Those inbox documents have already been handled.")
            return
        ids = [int(x) for x in batch.get("documents") or []]
        similar = {int(x) for x in batch.get("similar") or []}
        pending = []
        for document_id in ids:
            d = await registry.get(db, document_id)
            if d is not None and d["state"] == "proposed":
                pending.append(d)
        if action == "rev":
            await db.kv_put(f"{BATCH_KEY}{n}", {**batch, "state": "one_by_one"})
        elif action in ("sav", "des"):
            await db.kv_put(f"{BATCH_KEY}{n}", {**batch, "state": action})
        else:
            return
    if action == "rev":
        for d in pending:
            await flow.show(engine, int(d["id"]))
        return
    if action == "des":
        for d in pending:
            await registry.discard(db, int(d["id"]))
        paths = [r for d in pending if (r := file_in_inbox(engine, d)) is not None]
        failed = await discard(engine, paths)
        text = f"Discarded {_plural(len(pending), 'document', 'documents')}."
        if paths and not failed:
            text += (
                " Their files are in the Trash." if len(paths) > 1 else " Its file is in the Trash."
            )
        elif failed:
            text += " " + _duplicates_text(engine, [(r, "discarded") for r in failed], failed)
        await engine.notify(None, text)
        return
    saved = 0
    missing: list[str] = []
    sections: list[str] = []
    failed_sections: list[Path] = []
    today = flow.today_of(engine)
    for d in pending:
        if int(d["id"]) in similar:
            # It looks like one already saved: it is not saved again and its file goes
            # to the Trash.
            if await registry.discard(db, int(d["id"])) is not None:
                sections.append(f'"{d.get("title") or d.get("inbox")}"')
                path = file_in_inbox(engine, d)
                if path is not None:
                    failed_sections += await discard(engine, [path])
            continue
        async with db.lock(f"document:{d['id']}"):
            current = await registry.get(db, int(d["id"]))
            if current is None or current["state"] != "proposed":
                continue
            if current.get("inbox") and not await take_to_documents(engine, current):
                missing.append(str(current.get("inbox")))
                continue
            if await registry.save(db, int(d["id"])) is not None:
                await reminders.mark_as_seen(db, int(d["id"]), today)
                saved += 1
    remain = _visible(engine.config.paths.inbox)
    text = f"Saved {_plural(saved, 'document', 'documents')}."
    if sections:
        one = len(sections) == 1
        text += (
            f" I didn't save {_items(sections)}, which {'looks' if one else 'look'} like "
            f"{'a duplicate' if one else 'duplicates'} of what you already have"
        )
        if failed_sections:
            text += ". " + _duplicates_text(
                engine, [(r, "duplicate") for r in failed_sections], failed_sections
            )
        else:
            text += " and is in the Trash." if one else " and they are in the Trash."
    if missing:
        text += " I can't find " + _items([f'"{x}"' for x in missing]) + " in the inbox."
    if remain:
        names = [f'"{_relative(engine, r)}"' for r in remain[:5]]
        if len(remain) > 5:
            names.append(f"{len(remain) - 5} more")
        text += f" Still in the inbox: {_items(names)}."
    else:
        text += " The inbox is now empty."
    await engine.notify(None, text)
