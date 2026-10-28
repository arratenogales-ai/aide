"""From Telegram to a saved document.

A PDF or a photo with “documento” at the start of the caption goes here, without triage
or a task. The code takes it from the Telegram download cache (which the agent can read)
and saves it in data/documents; the model reads it once; the code checks the dates,
computes the deadline of each notice and shows the user what was read with “Save”,
“Correct” and “Discard”. Only “Save” saves it and activates its reminders. “Correct”
waits for a message from them with what changes (“vence 31-12-2027”, “preaviso 3 meses”,
“título Seguro del coche”...), which the code applies. “Discard” deletes the copy.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
import unicodedata
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any
from zoneinfo import ZoneInfo

from aide.bot.channel import Button
from aide.bot.formatting import bold, esc
from aide.common.files import Unreadable
from aide.common.text import normalize
from aide.core.call import NoReply
from aide.core.usage import limit_text
from aide.paperwork import reading, registry, reminders, short_date

if TYPE_CHECKING:
    from aide.core.engine import Engine

log = logging.getLogger(__name__)

# “document(s)” or “documento(s)” at the start of the caption, in any capitalization or with signs
# and emojis before or after (“📄 Documents:”, “Document!”, “documento: seguro”).
RE_CAPTION = re.compile(
    r"^[\W_]*(?:documentos?|documents?)\b[\W_]*(.*)$", re.IGNORECASE | re.DOTALL
)
# If the model does not respond when reading a document, it is retried once after this wait.
RETRY_SECONDS = 10
# What is shown for each type (the keys are those of reading.KINDS, which are what is saved).
NAME_KIND = {
    "insurance": "Insurance",
    "contract": "Contract",
    "permission": "Permit",
    "municipality": "Local government letter",
    "taxes": "Tax document",
    "car": "Car document",
    "bank": "Bank document",
    "health": "Health document",
    "work": "Work document",
    "housing": "Housing document",
    "education": "Education document",
    "other": "Document",
}
# The English name of each type, to correct it (“type insurance”); the one from reading.KINDS also
# works (“tipo seguro”).
ENGLISH_KIND = {
    "insurance": "insurance",
    "contract": "contract",
    "permission": "permit",
    "municipality": "local government",
    "taxes": "taxes",
    "car": "car",
    "bank": "bank",
    "health": "health",
    "work": "work",
    "housing": "housing",
    "education": "education",
    "other": "other",
}
# Other ways of naming the type when correcting it (already normalized).
ALIAS_KIND = {
    "seguro": "insurance",
    "contrato": "contract",
    "permiso": "permission",
    "impuestos": "taxes",
    "coche": "car",
    "banco": "bank",
    "salud": "health",
    "trabajo": "work",
    "vivienda": "housing",
    "estudios": "education",
    "otro": "other",
    **{v: k for k, v in ENGLISH_KIND.items()},
    "town hall": "municipality",
    "municipality": "municipality",
    "council": "municipality",
    "local": "municipality",
    "ayuntamiento": "municipality",
    "tax": "taxes",
    "vehicle": "car",
    "medical": "health",
    "job": "work",
    "employment": "work",
    "home": "housing",
    "rent": "housing",
    "studies": "education",
    "school": "education",
    "permission": "permission",
    "license": "permission",
}
MAIN_KINDS = ("expiry", "renewal", "end")
INSTRUCTIONS = (
    'Tell me what to change, one thing per line, for example "title Car insurance", "type '
    'insurance", "issuer Segurix", "number 12.345.678", "expires 2027-12-31", "notice 3 months", '
    '"date 2027-03-15 the second installment is due" or "remove date 2". The types are '
    + ", ".join(ENGLISH_KIND.values())
    + "."
)


SAVED = (
    "Saved. I'll remind you of each date 7 days before and on the day, and also 60 and 30 days "
    "before if it's that far off."
)


def today_of(engine: Engine) -> date:
    """Today's date in the user's time zone."""
    return datetime.now(UTC).astimezone(ZoneInfo(engine.config.time_zone)).date()


# Believable dates in a document (a year 1 would break the notice calculation).
MIN_YEAR, MAX_YEAR = 1900, 2200
# The start of each month's name in English, Spanish, German, French, Italian, Portuguese and Dutch
# (already without accents, as `normalize` leaves it), to find the date written in words in its
# phrase.
MONTHS = {
    1: ("jan", "gen", "ene"),
    2: ("feb", "fev"),
    3: ("mar", "maer", "maa"),
    4: ("apr", "abr", "avr"),
    5: ("mai", "may", "mag", "mei"),
    6: ("jun", "juin", "giu"),
    7: ("jul", "juil", "lug"),
    8: ("aug", "ago", "aou"),
    9: ("sep", "set"),
    10: ("okt", "oct", "ott", "out"),
    11: ("nov",),
    12: ("dez", "dic", "dec"),
}


def caption(text: str) -> str | None:
    """The caption note if it starts with “documento”; None if it is not for documents."""
    m = RE_CAPTION.match(text or "")
    return m.group(1).strip() if m else None


def safe_name(name: str) -> str:
    """Turns a file name into a safe name that keeps only simple characters."""
    name = unicodedata.normalize("NFC", name)
    clean = re.sub(r"[^\w.\-]+", "_", name).strip("._") or "document"
    return clean[-80:]


def move(files: list[Path], folder: Path) -> list[Path]:
    """Moves the files from the Telegram cache to data/documents/YYYY/, with the date in front,
    readable only by the user (the folders too: not even the names are visible). The cache
    is left without them."""
    moment = datetime.now()
    destination_dir = folder / f"{moment:%Y}"
    destination_dir.mkdir(parents=True, exist_ok=True)
    folder.chmod(0o700)
    destination_dir.chmod(0o700)
    moved = []
    for i, origin in enumerate(files):
        destination = destination_dir / f"{moment:%Y%m%d-%H%M%S-%f}-{i}-{safe_name(origin.name)}"
        shutil.move(str(origin), destination)
        destination.chmod(0o600)
        moved.append(destination)
        try:
            origin.parent.rmdir()  # the message's folder in the cache, if it has been left empty
        except OSError:
            pass
    return moved


def date_of(text: object) -> date | None:
    """A date in YYYY-MM-DD, DD-MM-YYYY, DD.MM.YYYY or DD/MM/YYYY, of a believable year."""
    value = str(text or "").strip()
    for format_ in ("%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            day = datetime.strptime(value, format_).date()
        except ValueError:
            continue
        return day if MIN_YEAR <= day.year <= MAX_YEAR else None
    return None


def date_in_phrase(day: date, phrase: str) -> bool:
    """Whether the phrase carries that date written out: 31.12.2027, 31/12/27, 2027-12-31, “31.
    Dezember 2027”, “31 de diciembre de 2027”, “31st of December 2027” or “December 31,
    2027”."""
    t = normalize(phrase)
    d, m, y = day.day, day.month, day.year
    numbers = (
        rf"\b0?{d} 0?{m} (?:{y}|{y % 100:02d})\b",
        rf"\b{y} 0?{m} 0?{d}\b",
    )
    if any(re.search(x, t) for x in numbers):
        return True
    months = "|".join(MONTHS[m])
    letters = (
        rf"\b0?{d}(?:er|st|nd|rd|th)? (?:de |of )?(?:{months})[a-z]* (?:de )?{y}\b",
        rf"\b(?:{months})[a-z]* 0?{d}(?:st|nd|rd|th)? {y}\b",
    )
    return any(re.search(x, t) for x in letters)


def subtract_months(day: date, months: int) -> date:
    """The same day, `months` earlier; if that month is shorter, its last day."""
    total = day.year * 12 + (day.month - 1) - months
    year, month = divmod(total, 12)
    month += 1
    return date(year, month, min(day.day, monthrange(year, month)[1]))


def advance_notice_text(months: int, days: int) -> str:
    """Describes an advance notice in months and days as text."""
    parts = []
    if months:
        parts.append(f"{months} {'month' if months == 1 else 'months'}")
    if days:
        parts.append(f"{days} {'day' if days == 1 else 'days'}")
    return " and ".join(parts)


def advance_notice_date(day: date, months: int, days: int, phrase: str = "") -> dict[str, Any]:
    """The deadline for giving notice, computed by the code. It keeps how it was computed, to
    redo it if the date it comes from is corrected."""
    limit = subtract_months(day, months) - timedelta(days=days)
    return {
        "kind": "advance_notice",
        "date": limit.isoformat(),
        "description": (
            "it's the last day to give notice if you want to cancel it, "
            f"{advance_notice_text(months, days)} before {short_date(day)}"
        ),
        "phrase": phrase,
        "checked": False,
        "rule": {"months": months, "days": days, "base": day.isoformat()},
    }


def in_text(phrase: str, text: str) -> bool:
    f = normalize(phrase)
    return bool(f) and len(f) >= 8 and f in normalize(text)


def valid_dates(read: list[Any], text: str) -> list[dict[str, Any]]:
    """The dates the model read that are valid, and the deadline of each notice."""
    output: list[dict[str, Any]] = []
    for f in (read or [])[: reading.MAX_DATES]:
        if not isinstance(f, dict):
            continue
        day = date_of(f.get("date"))
        if day is None:
            continue
        phrase = str(f.get("phrase") or "").strip()[:300]
        description = str(f.get("description") or "").strip()[:150] or "something happens"
        kind = f.get("kind") if f.get("kind") in reading.DATE_KINDS else "other"
        output.append(
            {
                "kind": kind,
                "date": day.isoformat(),
                "description": description,
                "phrase": phrase,
                # The phrase is in the document and carries that date (not a deduced
                # one or one with the year misread).
                "checked": bool(text) and in_text(phrase, text) and date_in_phrase(day, phrase),
            }
        )
        try:
            months = int(f.get("months_advance_notice") or 0)
            days = int(f.get("days_advance_notice") or 0)
        except (TypeError, ValueError):
            months = days = 0
        if (months or days) and 0 <= months <= 36 and 0 <= days <= 366:
            try:
                advance_notice = advance_notice_date(day, months, days, phrase)
            except (ValueError, OverflowError):
                continue
            advance_notice["checked"] = output[-1]["checked"]  # the same phrase of its date
            output.append(advance_notice)
    return output


def buttons(document_id: int) -> list[list[Button]]:
    """Returns the Save, Edit and Discard buttons for a proposed document."""
    return [
        [
            Button("Save", f"doc:sav:{document_id}"),
            Button("Edit", f"doc:cor:{document_id}"),
            Button("Discard", f"doc:no:{document_id}"),
        ]
    ]


def _timing(days: int) -> str:
    if days < 0:
        return "already past"
    if days == 0:
        return "today"
    if days == 1:
        return "tomorrow"
    return f"in {days} days"


def date_note(f: dict[str, Any], d: dict[str, Any]) -> str:
    """What needs review in a date that was read: its phrase is not in the document, or it is
    but does not carry that date (I deduced it or misread the year)."""
    if not f.get("phrase") or f["checked"] or f["kind"] == "advance_notice":
        return ""
    text = d.get("text") or ""
    if d.get("origin_text") == "pdf" and not in_text(f["phrase"], text):
        return " I can't find that sentence in the document: please check it."
    if not date_in_phrase(date.fromisoformat(f["date"]), f["phrase"]):
        return " That date isn't written like that in the document, I inferred it: please check it."
    return ""


def detail_of(d: dict[str, Any]) -> str:
    """“Insurance from Segurix”: the type and who issues it."""
    entity = d.get("entity")
    if entity and d.get("kind") == "municipality":
        return f"Letter from {entity}"  # not “Local government letter from Town of X”
    detail = NAME_KIND.get(d.get("kind") or "other", "Document")
    return f"{detail} from {entity}" if entity else detail


async def proposal_text(
    db: Any, d: dict[str, Any], today: date | None = None, closed: bool = False
) -> str:
    """Builds the text that shows a proposed document, with its summary and dates."""
    today = today or date.today()
    dates = await registry.dates_of(db, d["id"])
    lines = [bold(d.get("title") or "Untitled document")]
    detail = detail_of(d)
    if d.get("number"):
        detail += f", no. {d['number']}"
    lines.append(esc(detail + "."))
    if d.get("summary"):
        lines.append(esc(d["summary"]))
    if dates:
        lines.append(esc("The dates I found:"))
        for i, f in enumerate(dates, 1):
            day = date.fromisoformat(f["date"])
            line = f"{i}. On {short_date(day)} {f['description']} ({_timing((day - today).days)})."
            lines.append(esc(line + date_note(f, d)))
    else:
        lines.append(esc("I didn't find any dates to keep an eye on."))
    if d.get("doubts"):
        lines.append(esc(d["doubts"]))
    if closed:
        return "\n".join(lines)
    future = [f for f in dates if f["date"] >= today.isoformat()]
    lines.append(
        esc(
            "If you save it, I'll remind you of each date 7 days before and on the day, and also "
            "60 and 30 days before if it's that far off."
            if future
            else "If you save it, you'll have it to search and ask me about."
        )
    )
    return "\n".join(lines)


async def show(engine: Engine, document_id: int) -> None:
    """Sends the user a proposal for a document with its action buttons."""
    d = await registry.get(engine.db, document_id)
    if d is None:
        return
    message = await engine.notify(
        None,
        await proposal_text(engine.db, d, today_of(engine)),
        html=True,
        buttons=buttons(document_id),
    )
    if message:
        await registry.update(engine.db, document_id, message_id=message)


async def _close(engine: Engine, d: dict[str, Any], note: str, discarded: bool = False) -> None:
    """Closes the proposal message (without buttons) with what happened. If it was discarded,
    its data is gone: only the title remains."""
    if d.get("message_id") and engine.channel is not None:
        body = (
            bold(d.get("title") or "Document")
            if discarded
            else await proposal_text(engine.db, d, today_of(engine), closed=True)
        )
        await engine.channel.edit(d["message_id"], body + f"\n\n<i>{esc(note)}</i>")
    else:
        await engine.notify(None, note)


def _no_wait(engine: Engine, document_id: int) -> None:
    """If the correction of this document was being awaited, it no longer is."""
    wait = engine.text_wait
    if wait is not None and (wait.kind, wait.task_id) == (
        "document_correction",
        document_id,
    ):
        engine.text_wait = None


def path_of(folder: Path, d: dict[str, Any]) -> Path | None:
    """The file of a document, only if it is inside data/documents."""
    folder = folder.resolve()
    path = (folder / str(d.get("file") or "")).resolve()
    return path if path.is_relative_to(folder) and path != folder else None


def _delete(engine: Engine, d: dict[str, Any]) -> None:
    path = path_of(engine.config.paths.documents, d)
    if path is not None:
        path.unlink(missing_ok=True)


def fingerprint(path: Path) -> str:
    """SHA-256 of the file, to avoid reading the same inbox document twice."""
    summary = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            summary.update(chunk)
    return summary.hexdigest()


async def since_telegram(engine: Engine, text: str, files: list[Path]) -> bool:
    """One or several PDFs or photos with “documento” in the caption: each file, its own
    document. Returns whether it was for documents."""
    note = caption(text)
    if note is None or not files:
        return False
    folder = engine.config.paths.documents
    for origin in files:
        await _one_from_telegram(engine, origin, folder, note)
    return True


async def _one_from_telegram(engine: Engine, origin: Path, folder: Path, note: str) -> None:
    from aide.paperwork import duplicates

    name = origin.name
    try:
        [file] = move([origin], folder)
    except OSError:
        log.exception("could not save the document")
        await engine.notify(None, f'I couldn\'t save "{name}". Please send it again.')
        return
    h = await asyncio.to_thread(fingerprint, file)
    # A document that is already saved is not read again.
    saved = await duplicates.saved(engine)
    equal = duplicates.by_fingerprint(h, saved)
    if equal is None and file.suffix.lower() == ".pdf":
        equal = duplicates.by_text(await asyncio.to_thread(reading.pdf_text, file), saved)
    if equal is not None:
        file.unlink(missing_ok=True)
        await engine.notify(
            None,
            f'You already have "{name}" saved as "{equal["title"]}". I won\'t read or save it '
            "again.",
        )
        return
    try:
        document_id = await create_from(
            engine,
            file,
            note,
            name,
            rel_file=str(file.relative_to(folder)),
            file_fingerprint=h,
        )
    except Unreadable as e:
        file.unlink(missing_ok=True)
        await engine.notify(None, f'"{name}": {e} I haven\'t saved it.')
        return
    except NoReply as e:
        file.unlink(missing_ok=True)
        await engine.notify(
            None,
            f"I couldn't read \"{name}\" because {why(engine, e)}. I haven't saved it: "
            + {
                "limit": "send it again when it resets.",
                "image": "send me a photo of the document.",
            }.get(e.reason, "send it again in a little while."),
        )
        return
    except Exception:
        file.unlink(missing_ok=True)
        log.exception("could not read the document")
        await engine.notify(
            None, f"I couldn't read \"{name}\". I haven't saved it: please send it again."
        )
        return
    await show(engine, document_id)


def why(engine: Engine, e: NoReply) -> str:
    """Why a document could not be read, told to the user."""
    if e.reason == "limit":
        return limit_text(e.kind, e.reset, engine.config.time_zone)
    if e.reason == "image":
        return "its pages can't be turned into images the model accepts"
    return "the model didn't respond, even after retrying"


async def read_with_retry(engine: Engine, file: Path, note: str) -> tuple[Any, str]:
    """What the model reads. If it does not respond, it is retried once before giving up
    ; if it is because of the plan limit or because the image is not valid, it is not
        retried. NoReply with the reason if there is no reading."""
    try:
        return await reading.read(engine.core, file, note)
    except NoReply as e:
        if e.reason != "model":
            raise
    log.info("the model did not respond while reading a document: retrying")
    await asyncio.sleep(RETRY_SECONDS)
    return await reading.read(engine.core, file, note)


async def create_from(
    engine: Engine,
    file: Path,
    note: str,
    name: str,
    *,
    rel_file: str,
    rel_inbox: str | None = None,
    file_fingerprint: str | None = None,
) -> int:
    """The model reads it and leaves it as a proposal. NoReply if the model did not read it
    (with the reason) and Unreadable if there is nothing to read. It neither notifies nor
    deletes: the caller does that."""
    read, pdf_text = await read_with_retry(engine, file, note)
    doubts = str(read.get("doubts") or "").strip()
    if not read.get("is_document"):
        doubts = (doubts + " It doesn't look like one of your documents.").strip()
    doc_text = pdf_text or str(read.get("text") or "").strip()[:20_000]
    fields = {
        "title": str(read.get("title") or "").strip()[:150] or name,
        "kind": read.get("kind") if read.get("kind") in reading.KINDS else "other",
        "entity": str(read.get("entity") or "").strip()[:150],
        "number": str(read.get("number") or "").strip()[:80],
        "summary": str(read.get("summary") or "").strip()[:600],
        "file": rel_file,
        "original_name": name[:200],
        "text": doc_text,
        "origin_text": "pdf" if pdf_text else "image",
        "note": note[:500],
        "doubts": doubts[:500],
        "inbox": rel_inbox,
        "fingerprint": file_fingerprint,
    }
    document_id = await registry.create(
        engine.db, fields, valid_dates(read.get("dates") or [], pdf_text or doc_text)
    )
    await engine.tasks.event(None, "document", fields["title"], {"document": document_id})
    return document_id


async def button(engine: Engine, action: str, document_id: int) -> None:
    """“Save”, “Correct” or “Discard” of a proposed document. Each one works once."""
    from aide.core.engine import TextWait

    db = engine.db
    async with db.lock(f"document:{document_id}"):
        d = await registry.get(db, document_id)
        if d is None or d["state"] != "proposed":
            await engine.notify(None, "That document is no longer pending.")
            return
        if action == "no":
            await registry.discard(db, document_id)
            _delete(engine, d)
            _no_wait(engine, document_id)
            note = "You discarded it: I won't save it, and I've deleted my copy and what I read."
            if d.get("inbox"):
                # From the inbox: its file goes to the Trash, so the inbox ends up
                # empty.
                from aide.paperwork import inbox

                path = inbox.file_in_inbox(engine, d)
                note = "You discarded it: I won't save it."
                if path is not None and await inbox.discard(engine, [path]):
                    note += (
                        " I couldn't move its file to the Trash: please delete it from the inbox "
                        "yourself."
                    )
                elif path is not None:
                    note += " Its file is in the Trash."
            await _close(engine, d, note, discarded=True)
            return
        if action == "cor":
            wait = engine.text_wait
            if wait is not None and wait.kind == "plan_change":
                await engine.notify(
                    None,
                    f"First tell me what to change in the plan of task {wait.task_id}: I'm "
                    'waiting for that. Then tap "Edit" again.',
                )
                return
            engine.text_wait = TextWait("document_correction", document_id)
            await engine.notify(None, INSTRUCTIONS)
            return
        if action != "sav":
            return
        if d.get("inbox"):
            from aide.paperwork import inbox

            if not await inbox.take_to_documents(engine, d):
                await engine.notify(
                    None, f"I can't find \"{d['inbox']}\" in the inbox: I haven't saved it."
                )
                return
        saved = await registry.save(db, document_id)
        if saved is None:
            return
        # What was due to be reminded today has already been seen: the next reminder is the
        # one for the coming stage.
        await reminders.mark_as_seen(db, document_id, today_of(engine))
        _no_wait(engine, document_id)
    today = today_of(engine).isoformat()
    future = [f for f in await registry.dates_of(db, document_id) if f["date"] >= today]
    await _close(engine, saved, SAVED if future else "Saved.")


# --- corrections ----------------------------------------------------------------------------

# A correction line: the key at the start (with or without a colon) and its value. In English and
# Spanish (“expires 2027-12-31” or “vence 31-12-2027”).
RE_LINE = re.compile(
    r"^(?P<key>t[ií]tulo|title|tipo|type|entidad|issuer|entity|n[uú]mero|number|"
    r"n\.?\s*[º°]\.?|n\.o\.?|no(?=\.)|p[oó]liza|policy|resumen|summary|vence|expires?|expiry|"
    r"caduca|preaviso|notice|(?:quita|borra)\s+(?:la\s+)?fecha|"
    r"(?:remove|delete)\s+(?:the\s+)?date|fecha|date)"
    r"(?=[\s:.-]|$)[\s:.-]*(?P<value>.*)$",
    re.IGNORECASE,
)
FIELD = {
    "titulo": "title",
    "title": "title",
    "tipo": "kind",
    "type": "kind",
    "entidad": "entity",
    "issuer": "entity",
    "entity": "entity",
    "numero": "number",
    "number": "number",
    "poliza": "number",
    "policy": "number",
    "resumen": "summary",
    "summary": "summary",
    "vence": "expires",
    "caduca": "expires",
    "expire": "expires",
    "expires": "expires",
    "expiry": "expires",
    "preaviso": "advance_notice",
    "notice": "advance_notice",
    "fecha": "date",
    "date": "date",
}
# Months, days and weeks, in Spanish and English.
UNIT = r"(?:mes(?:es)?|months?|d[ií]as?|days?|semanas?|weeks?)"
RE_QUANTITY = re.compile(rf"(\d{{1,3}})\s*({UNIT})", re.IGNORECASE)
RE_ONLY_QUANTITIES = re.compile(
    rf"^(?:de\s+|of\s+)?\d{{1,3}}\s*{UNIT}"
    rf"(?:\s*(?:,|y|and)\s*\d{{1,3}}\s*{UNIT})*(?:\s+(?:antes|before))?$",
    re.IGNORECASE,
)


def _without_the(value: str) -> str:
    """“el 31-12-2027”, “on 2027-12-31” and “31-12-2027” are all equivalent."""
    return re.sub(r"^(?:el|la|para el|on|the|on the)\s+", "", value.strip(), flags=re.IGNORECASE)


def advance_notice_of(value: str) -> tuple[int, int] | None:
    """(months, days) from “3 months”, “1 month and 15 days”, “2 weeks” or “3 meses”; None if
    it is not just that."""
    if not RE_ONLY_QUANTITIES.match(value.strip()):
        return None
    months = days = 0
    for quantity, unit in RE_QUANTITY.findall(value):
        n = int(quantity)
        u = normalize(unit)
        if u.startswith(("mes", "month")):
            months += n
        elif u.startswith(("semana", "week")):
            days += 7 * n
        else:
            days += n
    if not (months or days) or months > 36 or days > 366:
        return None
    return months, days


def line(text: str) -> tuple[str, Any] | None:
    """(field, already checked value) of a correction line, or None if it is not entirely one:
    that way a normal message (“Number of the insurer?”) is not taken for a correction."""
    m = RE_LINE.match(text.strip())
    if m is None or text.strip().endswith("?"):
        return None
    key = normalize(m.group("key"))
    value = m.group("value").strip().strip("«»“”\"'").strip()
    if key.startswith(("quita", "borra", "remove", "delete")):
        return ("quita", int(value)) if value.isdigit() else None
    if key.startswith("n ") or key in ("n", "n o", "no"):
        key = "number"
    field = FIELD.get(key)
    if field is None or not value:
        return None
    if field in ("title", "entity", "summary"):
        # “Summary of today's meeting” or “Resumen de la reunión de hoy” is a phrase, not a
        # new summary.
        if re.match(r"(?:de|del|of|for|about)\s", value, re.IGNORECASE):
            return None
        return (field, value[:600])
    if field == "number":
        return ("number", value[:80]) if re.search(r"\d", value) else None
    if field == "kind":
        kind = normalize(value)
        kind = kind if kind in reading.KINDS else ALIAS_KIND.get(kind, "")
        return ("kind", kind) if kind in reading.KINDS else None
    if field == "expires":
        day = date_of(_without_the(value))
        return ("expires", day) if day else None
    if field == "date":
        parts = _without_the(value).split(maxsplit=1)
        day = date_of(parts[0]) if parts else None
        if day is None or len(parts) < 2:
            return None
        return ("date", (day, parts[1].strip()[:150]))
    if field == "advance_notice":
        quantities = advance_notice_of(value)
        return ("advance_notice", quantities) if quantities else None
    return None


def is_correction(text: str) -> bool:
    """Only if all its lines are well-written corrections."""
    lines = [x for x in text.splitlines() if x.strip()]
    return bool(lines) and all(line(x) is not None for x in lines)


def _clean_date(f: dict[str, Any]) -> dict[str, Any]:
    """A date as it is saved again when correcting (without the id or the reminders, which a
    proposal does not have yet)."""
    return {
        "kind": f["kind"],
        "date": f["date"],
        "description": f["description"],
        "phrase": f.get("phrase") or "",
        "checked": bool(f.get("checked")),
        "rule": registry.rule_of(f.get("rule")),
    }


def apply(
    d: dict[str, Any], dates: list[dict[str, Any]], text: str
) -> tuple[dict[str, Any], list[dict[str, Any]], list[str]]:
    """Applies the correction lines. Returns the fields that change, the dates that remain and
    what it did not understand."""
    fields: dict[str, Any] = {}
    dates = [_clean_date(f) for f in sorted(dates, key=lambda f: (f["date"], f.get("id") or 0))]
    no_understood: list[str] = []
    for line_text in (x.strip() for x in text.splitlines()):
        if not line_text:
            continue
        found = line(line_text)
        if found is None:
            no_understood.append(line_text)
            continue
        field, value = found
        if field in ("title", "entity", "number", "summary", "kind"):
            fields[field] = value
        elif field == "expires":
            before = {f["date"] for f in dates if f["kind"] == "expiry"}
            dates = [f for f in dates if f["kind"] != "expiry"]
            dates.append(
                {
                    "kind": "expiry",
                    "date": value.isoformat(),
                    "description": "it expires",
                    "phrase": "",
                    "checked": False,
                    "rule": None,
                }
            )
            # The notice period that came from the previous expiration is redone with
            # the new one.
            redone = []
            for f in dates:
                rule = f.get("rule")
                if f["kind"] == "advance_notice" and rule and rule.get("base") in before:
                    f = advance_notice_date(
                        value, int(rule.get("months") or 0), int(rule.get("days") or 0)
                    )
                redone.append(f)
            dates = redone
        elif field == "date":
            day, description = value
            dates.append(
                {
                    "kind": "other",
                    "date": day.isoformat(),
                    "description": description,
                    "phrase": "",
                    "checked": False,
                    "rule": None,
                }
            )
        elif field == "advance_notice":
            main = sorted((f for f in dates if f["kind"] in MAIN_KINDS), key=lambda f: f["date"])
            if not main:
                no_understood.append(
                    f'{line_text} (first tell me when it expires: "expires 2027-12-31")'
                )
                continue
            months, days = value
            base = main[-1]
            dates = [f for f in dates if f["kind"] != "advance_notice"]
            dates.append(advance_notice_date(date.fromisoformat(base["date"]), months, days))
        elif field == "quita":
            order = sorted(dates, key=lambda f: f["date"])
            if 1 <= value <= len(order):
                removed = order[value - 1]
                dates = [f for f in dates if f is not removed]
            else:
                no_understood.append(line_text)
    return fields, dates, no_understood


async def correct(engine: Engine, document_id: int, text: str) -> None:
    """Applies the user's text corrections to a proposed document."""
    from aide.core.engine import TextWait

    db = engine.db
    async with db.lock(f"document:{document_id}"):
        d = await registry.get(db, document_id)
        if d is None or d["state"] != "proposed":
            await engine.notify(None, "That document is no longer pending.")
            return
        fields, dates, no_understood = apply(d, await registry.dates_of(db, document_id), text)
        await registry.update(db, document_id, **fields)
        await registry.put_dates(db, document_id, dates)
    if no_understood:
        # They can write again what I did not understand without pressing “Correct” once
        # more.
        engine.text_wait = TextWait("document_correction", document_id)
        await engine.notify(
            None,
            "I didn't understand this: "
            + "; ".join(f'"{x}"' for x in no_understood)
            + '. Write it again, one thing per line, for example "expires 2027-12-31", '
            '"notice 3 months" or "remove date 2".',
        )
    await show(engine, document_id)
