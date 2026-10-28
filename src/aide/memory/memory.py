"""Long-term memory: facts approved by the user in agent/memory.md.

Each fact is a line `- (YYYY-MM-DD) text`. The agent proposes facts with
`memory_propose`; they are only written when the user presses Save. They are queried
with FTS5 and are not loaded whole into the context. The user can edit the file by hand
or from the panel.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from aide.core.db import Database, fts_query, now

LINE = re.compile(r"^\s*-\s*(?:\((\d{4}-\d{2}-\d{2})\)\s*)?(.+?)\s*$")


@dataclass
class Fact:
    """A remembered fact with its date and a short id based on its text."""

    text: str
    date: str | None

    @property
    def id(self) -> str:
        return hashlib.sha256(self.text.strip().lower().encode()).hexdigest()[:10]


class Memory:
    """The user's lasting facts, stored in a text file and indexed for search."""

    def __init__(self, path: Path, db: Database) -> None:
        self.path = path
        self.db = db
        self._lock = asyncio.Lock()
        self._indexed: float | None = None

    def _read(self) -> tuple[list[str], list[tuple[int, Fact]]]:
        if not self.path.exists():
            return [], []
        lines = self.path.read_text(encoding="utf-8").splitlines()
        facts = []
        for n, line in enumerate(lines):
            m = LINE.match(line)
            if m and m.group(2).strip():
                facts.append((n, Fact(m.group(2).strip(), m.group(1))))
        return lines, facts

    def facts(self) -> list[Fact]:
        return [h for _, h in self._read()[1]]

    async def _reindex_if_change(self) -> None:
        mark = self.path.stat().st_mtime if self.path.exists() else 0.0
        if self._indexed == mark:
            return
        statements = [("DELETE FROM memory_fts", ())]
        statements += [("INSERT INTO memory_fts(done) VALUES (?)", (h.text,)) for h in self.facts()]
        await self.db.run_several(statements)
        self._indexed = mark

    async def search(self, query: str, limit: int = 8) -> list[str]:
        """Searches the remembered facts, falling back to a plain text match."""
        await self._reindex_if_change()
        rows = await self.db.everything(
            "SELECT done FROM memory_fts WHERE memory_fts MATCH ? ORDER BY bm25(memory_fts) LIMIT ?",
            (fts_query(query), limit),
        )
        found = [f["done"] for f in rows]
        if not found:
            low = query.lower()
            found = [h.text for h in self.facts() if low in h.text.lower()][:limit]
        return found

    async def add(self, text: str) -> Fact:
        """Adds a fact to memory unless it is already there."""
        text = " ".join(text.split())
        async with self._lock:
            if any(h.text.lower() == text.lower() for h in self.facts()):
                return Fact(text, None)
            if not self.path.exists():
                self.path.write_text("# Aide memory\n\n", encoding="utf-8")
            content = self.path.read_text(encoding="utf-8")
            if content and not content.endswith("\n"):
                content += "\n"
            done = Fact(text, date.today().isoformat())
            self.path.write_text(content + f"- ({done.date}) {text}\n", encoding="utf-8")
        await self._reindex_if_change()
        return done

    def matches(self, text: str) -> list[Fact]:
        low = text.lower().strip()
        return [h for h in self.facts() if low and low in h.text.lower()]

    async def forget(self, done_id: str) -> Fact | None:
        """Removes a fact by id and returns it, or None if it is not found."""
        async with self._lock:
            lines, facts = self._read()
            for n, done in facts:
                if done.id == done_id:
                    del lines[n]
                    self.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
                    break
            else:
                return None
        await self._reindex_if_change()
        return done

    # --- proposals -------------------------------------------------------------------------

    async def propose(self, done: str, task_id: int | None) -> int:
        """Records a proposed fact, pending the user's approval."""
        return await self.db.run(
            "INSERT INTO memory_proposals(done, task_id, state, created) VALUES (?, ?, 'pending', ?)",
            (" ".join(done.split()), task_id, now()),
        )

    async def resolve_proposal(self, proposal_id: int, accept: bool) -> str | None:
        """Accepts or discards a memory proposal and returns its text if pending."""
        row = await self.db.one("SELECT * FROM memory_proposals WHERE id=?", (proposal_id,))
        if row is None or row["state"] != "pending":
            return None
        await self.db.run(
            "UPDATE memory_proposals SET state=?, resolved=? WHERE id=?",
            ("approved" if accept else "discarded", now(), proposal_id),
        )
        if accept:
            await self.add(row["done"])
        return row["done"]

    async def pending_proposals(self) -> list[dict]:
        """Returns the memory proposals still waiting for a decision."""
        return await self.db.everything(
            "SELECT * FROM memory_proposals WHERE state='pending' ORDER BY id"
        )
