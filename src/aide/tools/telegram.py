"""Telegram tools: milestone notices and sending files to the user."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from claude_agent_sdk import ToolAnnotations, tool

from aide.core.context import SessionContext
from aide.tools.server import error, log_factory, text


@log_factory
def telegram_tools(ses: SessionContext) -> list[Any]:
    """Builds the tools to send Telegram messages and files to the user."""
    core = ses.core
    agent = core.paths.agent.resolve()

    @tool(
        "telegram_send_message",
        "Sends the user a short notice on Telegram. Use it only at milestones (plan under way, draft "
        "ready, blocked); never to report every step. The system sends the final delivery.",
        {"text": Annotated[str, "Text of the notice, short"]},
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    )
    async def telegram_send_message(args: dict[str, Any]) -> dict[str, Any]:
        message = str(args.get("text", "")).strip()
        if not message:
            return error("The text is empty.")
        if core.notify is None:
            return error("There is no channel to the user right now.")
        await core.notify(ses.task_id, message[:3500])
        await core.tasks.event(ses.task_id, "agent_notice", message[:500])
        return text("Notice sent to the user.")

    @tool(
        "telegram_send_file",
        "Sends the user a file from the agent folder (a deliverable or a screenshot) with optional text.",
        {
            "path": Annotated[str, "Absolute path of the file"],
            "text": Annotated[str, "Accompanying text"],
        },
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, openWorldHint=False),
    )
    async def telegram_send_file(args: dict[str, Any]) -> dict[str, Any]:
        path = Path(str(args.get("path", ""))).expanduser()
        try:
            path = path.resolve(strict=True)
        except OSError:
            return error(f"The file {path} does not exist.")
        if not path.is_relative_to(agent):
            return error("Only files from the agent folder can be sent.")
        channel = core.channel
        if channel is None:
            return error("There is no channel to the user right now.")
        legend = str(args.get("text") or "")[:1000] or None
        if path.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
            message_id = await channel.send_image(path, legend)
        else:
            message_id = await channel.send_file(path, legend)
        await core.tasks.link_message(message_id, ses.task_id, "output")
        if ses.task_id:
            await core.tasks.log_file(ses.task_id, path, "sent")
        return text(f"File sent to the user: {path.name}")

    return [telegram_send_message, telegram_send_file]
