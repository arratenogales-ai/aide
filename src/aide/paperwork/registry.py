"""The saved documents and their dates in the database.

The text of each saved document also goes to an FTS5 index (title, type, entity, number,
summary and text) so the agent can search them without reading the files.
"""

from __future__ import annotations

import json
import re
from datetime import date
from typing import Any

from aide.common.text import normalize
from aide.core.db import now

# Words in a question that do not help searching, in Spanish and English.
EMPTY = {
    "cuando",
    "cual",
    "cuales",
    "donde",
    "como",
    "que",
    "quien",
    "para",
    "por",
    "con",
    "sin",
    "del",
    "los",
    "las",
    "una",
    "uno",
    "unos",
    "mis",
    "tus",
    "sus",
    "este",
    "esta",
    "ese",
    "esa",
    "tengo",
    "tiene",
    "hay",
    "dice",
    "documento",
    "documentos",
    "papel",
    "papeles",
    # And in English.
    "what",
    "when",
    "which",
    "where",
    "who",
    "how",
    "does",
    "did",
    "have",
    "has",
    "the",
    "and",
    "for",
    "with",
    "without",
    "from",
    "about",
    "this",
    "that",
    "these",
    "those",
    "there",
    "are",
    "was",
    "will",
    "mine",
    "your",
    "our",
    "document",
    "paper",
    "papers",
}


async def create(db: Any, fields: dict[str, Any], dates: list[dict[str, Any]]) -> int:
    """Creates a proposed document and its dates, and returns its id."""
    fields = {**fields, "created": now(), "state": "proposed"}
    columns = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    document_id = await db.run(
        f"INSERT INTO documents({columns}) VALUES ({marks})", tuple(fields.values())
    )
    await put_dates(db, document_id, dates)
    return document_id


async def get(db: Any, document_id: int) -> dict[str, Any] | None:
    return await db.one("SELECT * FROM documents WHERE id=?", (document_id,))


async def dates_of(db: Any, document_id: int) -> list[dict[str, Any]]:
    """Returns the dates recorded for a document, in order."""
    return await db.everything(
        "SELECT * FROM document_dates WHERE document_id=? ORDER BY date, id", (document_id,)
    )


def given_notices(value: Any) -> list[int]:
    """The reminders already given for a date, whether it comes as a list or as the database
    JSON. Only numbers count: anything else counts as none."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return []
    if not isinstance(value, list):
        return []
    return sorted(
        {x for x in value if isinstance(x, int) and not isinstance(x, bool)}, reverse=True
    )


def rule_of(value: Any) -> dict[str, Any] | None:
    """The rule of a notice period, whether it comes as a dict or as the database JSON."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except ValueError:
            return None
    return value if isinstance(value, dict) else None


async def put_dates(db: Any, document_id: int, dates: list[dict[str, Any]]) -> None:
    """Changes all the dates of a document (of a proposal or when correcting it)."""
    statements: list[tuple[str, tuple[Any, ...]]] = [
        ("DELETE FROM document_dates WHERE document_id=?", (document_id,))
    ]
    for f in dates:
        rule = rule_of(f.get("rule"))
        statements.append(
            (
                "INSERT INTO document_dates(document_id, kind, date, description, phrase, "
                "checked, notices, rule) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    document_id,
                    f["kind"],
                    f["date"],
                    f["description"],
                    f.get("phrase") or "",
                    int(bool(f.get("checked"))),
                    json.dumps(given_notices(f.get("notices"))),
                    json.dumps(rule) if rule else None,
                ),
            )
        )
    await db.run_several(statements)


async def update(db: Any, document_id: int, **fields: Any) -> None:
    """Updates the given fields of a document."""
    if not fields:
        return
    assignments = ", ".join(f"{k}=?" for k in fields)
    await db.run(f"UPDATE documents SET {assignments} WHERE id=?", (*fields.values(), document_id))


async def save(db: Any, document_id: int) -> dict[str, Any] | None:
    """From proposed to saved, and into the search index. None if it was no longer proposed."""
    if not await db.change(
        "UPDATE documents SET state='saved', saved=? WHERE id=? AND state='proposed'",
        (now(), document_id),
    ):
        return None
    d = await get(db, document_id)
    assert d is not None
    # “ß” goes as “ss”, as `normalize` leaves it in searches (“Straße” and “Strasse”).
    fields = ("title", "kind", "entity", "number", "summary", "text")
    values = [for_index(d.get(c)) for c in fields]
    await db.run_several(
        [
            ("DELETE FROM documents_fts WHERE rowid=?", (document_id,)),
            (
                "INSERT INTO documents_fts(rowid, title, kind, entity, number, summary, text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (document_id, *values),
            ),
        ]
    )
    return d


async def discard(db: Any, document_id: int) -> dict[str, Any] | None:
    """From proposed to discarded, and what was read is forgotten: only the row remains, so
    that a button pressed again says it is no longer pending. Returns the document as it was
    before (to show it and delete its file), or None if it was no longer proposed."""
    d = await get(db, document_id)
    if d is None or not await db.change(
        "UPDATE documents SET state='discarded', title=NULL, entity=NULL, number=NULL, "
        "summary=NULL, text=NULL, note=NULL, doubts=NULL, original_name=NULL "
        "WHERE id=? AND state='proposed'",
        (document_id,),
    ):
        return None
    await db.run("DELETE FROM document_dates WHERE document_id=?", (document_id,))
    return d


async def remove(db: Any, document_id: int) -> dict[str, Any] | None:
    """A saved document that the user removes from the panel: it becomes discarded, without
    what was read, without dates (or reminders) and out of the search index, just like
    `discard`. Returns the document as it was (its file goes to the Trash), or None if it
    was not saved."""
    d = await get(db, document_id)
    if d is None or not await db.change(
        "UPDATE documents SET state='discarded', title=NULL, entity=NULL, number=NULL, "
        "summary=NULL, text=NULL, note=NULL, doubts=NULL, original_name=NULL "
        "WHERE id=? AND state='saved'",
        (document_id,),
    ):
        return None
    await db.run_several(
        [
            ("DELETE FROM document_dates WHERE document_id=?", (document_id,)),
            ("DELETE FROM documents_fts WHERE rowid=?", (document_id,)),
        ]
    )
    return d


async def saved(db: Any) -> list[dict[str, Any]]:
    return await db.everything("SELECT * FROM documents WHERE state='saved' ORDER BY id DESC")


def for_index(value: Any) -> str:
    return str(value or "").replace("ß", "ss").replace("ẞ", "SS")


def words_of(query: str) -> list[str]:
    """The words of a question that are useful for searching, without repeats."""
    views: list[str] = []
    for p in normalize(query).split():
        if len(p) >= 3 and p not in EMPTY and not p.isdigit() and p not in views:
            views.append(p)
    numbers = re.findall(r"\b\d[\d.\-/]{2,}\b", query)
    return views[:12] + [n for n in numbers if n not in views][:3]


async def search(db: Any, query: str, limit: int = 5) -> list[dict[str, Any]]:
    """The saved documents that best match the query, with a piece of their text."""
    words = words_of(query)
    if not words:
        return []
    # Each word as a prefix (“segur*” finds “seguro” and “seguros”), in quotes so it does not
    # count as FTS5 syntax.
    expression = " OR ".join(f'"{p.replace(chr(34), "")}"*' for p in words)
    try:
        rows = await db.everything(
            "SELECT rowid AS id, snippet(documents_fts, -1, '«', '»', '…', 24) AS chunk "
            "FROM documents_fts WHERE documents_fts MATCH ? ORDER BY bm25(documents_fts) "
            "LIMIT ?",
            (expression, limit),
        )
    except Exception:
        rows = []
    output = []
    for f in rows:
        d = await get(db, f["id"])
        if d is None or d["state"] != "saved":
            continue
        output.append({**d, "chunk": f.get("chunk") or "", "dates": await dates_of(db, d["id"])})
    return output


def days_until(date_: str, today: date) -> int | None:
    """Returns the days from today until a date, or None if the date is invalid."""
    try:
        return (date.fromisoformat(date_) - today).days
    except ValueError:
        return None
