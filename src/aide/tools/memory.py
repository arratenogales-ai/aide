"""Memory tools.

- `memory_search`: queries the approved facts in agent/memory.md (FTS5).
- `memory_propose`: writes nothing. It records the proposal and sends the user a message
  with Save and Discard; the fact only reaches memory.md when the user presses Save.

The proposal warns visibly if the fact contains data (emails, links) that appear only in
external content, or if text with instructions was read during the task: that way an
email or a website cannot sneak in a "memory" without the user seeing it.
"""

from __future__ import annotations

from typing import Annotated, Any

from claude_agent_sdk import ToolAnnotations, tool

from aide.bot.channel import Button
from aide.bot.formatting import bold, esc
from aide.core.context import SessionContext
from aide.core.injection import external_reason
from aide.tools.server import error, log_factory, text

MAX_PROPOSALS_PER_TASK = 3
MAX_DONE = 300


@log_factory
def memory_tools(ses: SessionContext) -> list[Any]:
    """Builds the tools to search and manage the user's memory."""
    core = ses.core
    memory = core.services.get("memory")
    if memory is None:
        return []

    @tool(
        "memory_search",
        "Searches the user's memory (lasting facts they have approved: preferences, details, "
        "decisions). Returns the matching facts.",
        {"query": Annotated[str, "Keywords"]},
        annotations=ToolAnnotations(readOnlyHint=True, openWorldHint=False),
    )
    async def memory_search(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            return error("The query is empty.")
        facts = await memory.search(query)
        return text(facts or "No facts in memory match.")

    @tool(
        "memory_propose",
        "Proposes to the user saving a lasting, useful fact in their memory (one sentence). It is not saved "
        "until the user approves it with a button; do not wait for the answer. Nothing temporary or trivial.",
        {"done": Annotated[str, "The fact, as one complete sentence"]},
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    )
    async def memory_propose(args: dict[str, Any]) -> dict[str, Any]:
        done = " ".join(str(args.get("done", "")).split())
        if not done:
            return error("The fact is empty.")
        if len(done) > MAX_DONE:
            return error(
                f"The fact is too long ({MAX_DONE} characters at most): sum it up in one sentence."
            )
        if any(h.text.lower() == done.lower() for h in memory.facts()):
            return text("That fact is already in memory.")
        if ses.task_id is not None:
            already = await core.db.value(
                "SELECT COUNT(*) FROM memory_proposals WHERE task_id=?", (ses.task_id,)
            )
            if (already or 0) >= MAX_PROPOSALS_PER_TASK:
                return error(
                    "You have already proposed enough facts in this task; do not propose any more."
                )
        if core.notify is None:
            return error("There is no channel to the user right now; the fact cannot be proposed.")
        proposal_id = await memory.propose(done, ses.task_id)
        external = external_reason(
            done, [(f.source, f.text) for f in ses.external_content], ses.user_texts
        )
        if external is None and ses.suspicious_injection:
            external = (
                f"text with instructions was read during the task ({ses.suspicious_injection})"
            )
        parts = [
            bold("Save this to memory?") + esc(f" (task {ses.task_id})" if ses.task_id else ""),
            esc(f"“{done}”"),
        ]
        if external:
            parts.append(
                bold("Heads up") + esc(f". This may come from external content: {external}")
            )
        buttons = [
            [
                Button("Save", f"mem:{proposal_id}:yes"),
                Button("Discard", f"mem:{proposal_id}:no"),
            ]
        ]
        await core.notify(ses.task_id, "\n\n".join(parts), buttons=buttons, html=True)
        await core.tasks.event(
            ses.task_id,
            "proposal_memory",
            done,
            {"proposal": proposal_id, "external": external},
        )
        return text(
            "Proposal sent to the user. It will be saved only if they approve it; carry on with the task without waiting."
        )

    return [memory_search, memory_propose]
