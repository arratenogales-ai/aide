"""Duplicate documents.

Before reading a document, at no cost: the same file as one already saved or as another
in the inbox (by its SHA-256, including those saved before the fingerprint was
recorded), or a PDF with the same text as a saved one. “The same text” means equal
ignoring spaces, punctuation and capitalization, or almost equal and with the same
numbers: that way two policies from different years, with the same template, do not come
out as equal. After reading it (a scan can only be compared then): same type, the same
main dates and the same entity or number.
"""

from __future__ import annotations

import asyncio
import re
from difflib import SequenceMatcher
from typing import TYPE_CHECKING, Any

from aide.common.files import MIN_PDF_TEXT
from aide.paperwork import flow, registry

if TYPE_CHECKING:
    from aide.core.engine import Engine

# How similar two texts with the same numbers must be to count as the same document.
MIN_SIMILARITY = 0.95


async def saved(engine: Engine) -> list[dict[str, Any]]:
    """The saved documents with their fingerprint: the one in the database or, if it has none
    (the older ones), that of its file, computed now and not written."""
    rows = await engine.db.everything(
        "SELECT id, title, kind, entity, number, text, origin_text, file, fingerprint "
        "FROM documents WHERE state='saved' ORDER BY id"
    )
    for f in rows:
        if f.get("fingerprint"):
            continue
        path = flow.path_of(engine.config.paths.documents, f)
        if path is not None and path.is_file():
            try:
                f["fingerprint"] = await asyncio.to_thread(flow.fingerprint, path)
            except OSError:
                pass
    return rows


def plain(text: str | None) -> str:
    return " ".join(re.sub(r"[^\w]+", " ", (text or "").lower()).split())


def same_text(a: str, b: str) -> bool:
    """Tells whether two texts are the same document, allowing for small differences."""
    pa, pb = plain(a), plain(b)
    if not pa or not pb:
        return False
    if pa == pb:
        return True
    if sorted(re.findall(r"\d+", pa)) != sorted(re.findall(r"\d+", pb)):
        return False
    wa, wb = pa.split(), pb.split()
    if min(len(wa), len(wb)) < 0.8 * max(len(wa), len(wb)):
        return False
    return SequenceMatcher(None, wa, wb, autojunk=False).ratio() >= MIN_SIMILARITY


def by_fingerprint(fingerprint: str, docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    return next((d for d in docs if d.get("fingerprint") == fingerprint), None)


def by_text(text: str, docs: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The saved one with the same PDF text (only texts extracted by the code are compared)."""
    if len(text) < MIN_PDF_TEXT:
        return None
    return next(
        (d for d in docs if d.get("origin_text") == "pdf" and same_text(text, d.get("text") or "")),
        None,
    )


async def _main(db: Any, document_id: int) -> set[tuple[str, str]]:
    return {
        (f["kind"], f["date"])
        for f in await registry.dates_of(db, document_id)
        if f["kind"] in flow.MAIN_KINDS
    }


async def same_document(engine: Engine, document_id: int) -> dict[str, Any] | None:
    """After reading it: the saved document that seems to be the same one, or None."""
    db = engine.db
    d = await registry.get(db, document_id)
    if d is None:
        return None
    dates = await _main(db, document_id)
    if not dates:
        return None
    candidates = await db.everything(
        "SELECT id, title, entity, number FROM documents "
        "WHERE state='saved' AND kind=? AND id<>? ORDER BY id",
        (d.get("kind"), document_id),
    )
    for g in candidates:
        if await _main(db, int(g["id"])) != dates:
            continue
        number, entity = plain(d.get("number")), plain(d.get("entity"))
        if (number and number == plain(g.get("number"))) or (
            entity and entity == plain(g.get("entity"))
        ):
            return g
    return None
