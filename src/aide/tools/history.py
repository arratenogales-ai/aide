"""History tools: FTS search in previous tasks."""

from __future__ import annotations

from typing import Annotated, Any

from claude_agent_sdk import ToolAnnotations, tool

from aide.core.context import SessionContext
from aide.tools.server import error, log_factory, text

READING = ToolAnnotations(readOnlyHint=True, openWorldHint=False)


@log_factory
def history_tools(ses: SessionContext) -> list[Any]:
    """Builds the tools to search the user's earlier tasks."""
    core = ses.core

    @tool(
        "history_search",
        "Searches the user's earlier tasks (request, summary and files). Returns the most "
        "relevant ones with their number, date and summary.",
        {"query": Annotated[str, "Keywords"]},
        annotations=READING,
    )
    async def history_search(args: dict[str, Any]) -> dict[str, Any]:
        query = str(args.get("query", "")).strip()
        if not query:
            return error("The query is empty.")
        rows = await core.tasks.search_history(query, limit=8)
        if not rows:
            return text("No earlier tasks match.")
        result = [
            {
                "task": f["id"],
                "date": f["created"][:10],
                "state": f["state"],
                "request": (f["request"] or "")[:300],
                "summary": (f["summary"] or "")[:600],
            }
            for f in rows
            if f["id"] != ses.task_id
        ]
        return text(result or "Only the current task matches.")

    @tool(
        "history_task",
        "Details of an earlier task: request, plan, summary, folder and files.",
        {"id": Annotated[int, "Task number"]},
        annotations=READING,
    )
    async def history_task(args: dict[str, Any]) -> dict[str, Any]:
        try:
            task_id = int(args.get("id"))
        except (TypeError, ValueError):
            return error("The task number is not valid.")
        task = await core.tasks.get(task_id)
        if task is None:
            return error(f"Task {task_id} does not exist.")
        files = await core.tasks.files(task_id)
        folder = core.paths.task(task_id)
        return text(
            {
                "task": task.id,
                "created": task.created,
                "state": task.state,
                "request": task.request,
                "plan": task.plan,
                "summary": task.summary,
                "folder": str(folder),
                "files": [a["path"] for a in files],
                "progress": str(folder / "progress.md"),
            }
        )

    return [history_search, history_task]
