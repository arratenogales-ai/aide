"""can_use_tool callback: level 2 approvals and questions to the user.

What the PreToolUse hook marks with `ask` (level 2) and AskUserQuestion arrive here. The wait
has no time limit: the documentation allows the callback to stay pending.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from claude_agent_sdk import PermissionResultAllow, PermissionResultDeny, ToolPermissionContext

from aide.core.approvals import Request
from aide.core.context import SessionContext
from aide.core.hooks import note_progress
from aide.core.injection import external_reason
from aide.core.policy import APPROVAL, NOTICE, Decision

log = logging.getLogger(__name__)


@dataclass
class BatchItem:
    """One item of a batch approval, with its value, entry, text and fingerprint."""

    value: Any  # what is kept in the input list if approved
    entry: dict[str, Any]  # input of the individual action (gives the approval key)
    text: str
    fingerprint: str | None = None


@dataclass
class Detail:
    """How an action is presented to the user."""

    kind: str
    text: str
    # Batch actions: items, input field with the list and name of the individual action.
    batch: list[BatchItem] = field(default_factory=list)
    batch_field: str | None = None
    item_tool: str | None = None
    batch_title: str = ""
    screenshot: Path | None = None
    fingerprint: str | None = None


Formatter = Callable[[SessionContext, str, dict[str, Any], Decision], Awaitable[Detail]]


async def generic_detail(
    ses: SessionContext, tool: str, entry: dict[str, Any], d: Decision
) -> Detail:
    """Builds the approval Detail for a tool call, with reason and optional screenshot."""
    text = d.detail or f"{tool}\n{json.dumps(entry, ensure_ascii=False, indent=1, default=str)}"
    if d.reason and d.reason not in text:
        text += f"\n\nReason: {d.reason}"
    screenshot = tab_screenshot(ses, entry) if d.kind == "chrome_send" else None
    return Detail(d.kind, text, screenshot=screenshot)


def tab_screenshot(ses: SessionContext, entry: dict[str, Any]) -> Path | None:
    """The screenshot that still shows the page of the tab where the action will take place."""
    return ses.screenshots.get(str(entry.get("tabId")))


def formatters(ses: SessionContext) -> dict[str, Formatter]:
    return ses.core.services.setdefault("formatters", {})


def create_can_use_tool(ses: SessionContext) -> Any:
    """Builds the permission callback that decides whether each tool use is allowed."""
    core = ses.core

    async def can_use_tool(tool: str, entry: dict[str, Any], context: ToolPermissionContext) -> Any:
        if tool == "AskUserQuestion":
            if ses.role != "orchestrator" or core.channel is None:
                return PermissionResultDeny(
                    message="You can't ask the user right now. Decide what is most reasonable yourself and explain it."
                )
            reply = await core.questions.ask(ses.task_id, entry)
            return PermissionResultAllow(updated_input=reply)

        if tool == "SandboxNetworkAccess":
            return await _sandbox_network(ses, entry)

        agent = ses.agents_by_id.get(str(context.agent_id)) if context.agent_id else None
        decision = ses.decisions.pop(context.tool_use_id or "", None)
        if decision is None:
            decision = ses.policy.evaluate(tool, entry, ses.call(agent))
        if decision is None or decision.level <= NOTICE:
            return PermissionResultAllow(
                updated_input=ses.policy.search_entry(tool, entry) or entry
            )
        if decision.level > APPROVAL:
            return PermissionResultDeny(message=f"Blocked by the policy: {decision.reason}")
        if ses.role != "orchestrator":
            return PermissionResultDeny(
                message="This action needs the user's approval and can only be done inside a task."
            )
        if decision.kind == "chrome_send" and tab_screenshot(ses, entry) is None:
            # The user approves a send by seeing what will be sent.
            return PermissionResultDeny(
                message=(
                    "Before asking for approval, take a screenshot of that tab as it is now "
                    "(computer with action screenshot, on its own and not inside a batch) so "
                    "the user sees what will be sent, and then repeat the action."
                )
            )

        format_ = formatters(ses).get(tool, generic_detail)
        try:
            detail = await format_(ses, tool, entry, decision)
        except Exception as e:
            log.exception("couldn't prepare the details of %s", tool)
            return PermissionResultDeny(message=f"Couldn't prepare the approval: {e}")

        external = external_reason(
            entry,
            [(f.source, f.text) for f in ses.external_content],
            ses.user_texts,
        )
        if external is None and ses.suspicious_injection:
            external = (
                f"text with instructions was read during the task ({ses.suspicious_injection})"
            )

        if detail.batch and detail.batch_field:
            requests = [
                Request(
                    ses.task_id,
                    detail.item_tool or tool,
                    e.entry,
                    detail.kind,
                    e.text,
                    external_reason=external,
                    fingerprint=e.fingerprint,
                )
                for e in detail.batch
            ]
            resolutions = await core.approvals.request_batch(
                ses.task_id, detail.batch_title or "Actions", requests
            )
            approved = [
                e.value for e, r in zip(detail.batch, resolutions, strict=True) if r.approved
            ]
            note_progress(
                ses.folder,
                f"Batch approval: {len(approved)} of {len(resolutions)} approved.",
            )
            if not approved:
                return PermissionResultDeny(
                    message="The user rejected the whole batch. Don't repeat it; explain in the delivery what was left undone."
                )
            return PermissionResultAllow(updated_input={**entry, detail.batch_field: approved})

        resolution = await core.approvals.request(
            Request(
                ses.task_id,
                tool,
                entry,
                detail.kind,
                detail.text,
                external_reason=external,
                screenshot=detail.screenshot,
                fingerprint=detail.fingerprint,
            )
        )
        if resolution.approved:
            note_progress(ses.folder, f"Approved by the user: {detail.text.splitlines()[0][:120]}")
            # An approved Grep doesn't read what is forbidden below its folder either
            # .
            return PermissionResultAllow(
                updated_input=ses.policy.search_entry(tool, entry) or entry
            )
        note_progress(ses.folder, f"Rejected by the user: {detail.text.splitlines()[0][:120]}")
        return PermissionResultDeny(
            message="The user rejected this action. Don't repeat it or work around it; carry on without it and explain it in the delivery."
        )

    return can_use_tool


async def _sandbox_network(ses: SessionContext, entry: dict[str, Any]) -> Any:
    """Network connection of a sandboxed command (it doesn't go through the PreToolUse hook)."""
    host = str(entry.get("host", ""))
    decision = ses.policy.classify_network_sandbox(host)
    mentioned = any(host.lower() in t.lower() for t in ses.user_texts)
    if decision.level == 0 and ses.suspicious_injection and not mentioned:
        # After reading content with instructions, a new host can be an exfiltration route.
        decision = Decision(
            APPROVAL,
            f"connection to {host} after reading suspicious content",
            "network",
            detail=f"Allow a command to connect to {host}",
        )
    await ses.core.tasks.event(
        ses.task_id, "network", f"{host}: level {decision.level}", {"host": host}
    )
    if decision.level == 0:
        return PermissionResultAllow(updated_input=entry)
    if decision.level > APPROVAL or ses.role != "orchestrator":
        return PermissionResultDeny(message=f"Connection blocked by the policy: {decision.reason}")
    external = ses.suspicious_injection
    resolution = await ses.core.approvals.request(
        Request(
            ses.task_id,
            "SandboxNetworkAccess",
            {"host": host},
            "network",
            decision.detail or decision.reason,
            external_reason=external,
        )
    )
    if resolution.approved:
        return PermissionResultAllow(updated_input=entry)
    return PermissionResultDeny(message=f"The user didn't allow the connection to {host}.")
