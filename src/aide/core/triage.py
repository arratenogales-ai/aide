"""Triage of each new message.

Haiku with low effort, without tools and with structured output validated against a JSON
schema. Deterministic rules are applied on top: large work or work with external actions gets an
upfront plan, and the related task must exist among the recent ones.

Only what leaves a file, takes several steps or has an action to approve, or needs its
Chrome, is a task, with its folder. The rest is answered in Telegram. The code decides it from
what the model sees; if the model was unsure or thought it was a task, the reply carries the
“Do it as a task” button.
"""

from __future__ import annotations

import logging
import re
from typing import Literal

from pydantic import BaseModel, ValidationError

from aide.core.context import Core, SessionContext
from aide.core.engine import Triage
from aide.core.orchestrator import AgentSession
from aide.core.session import triage_options
from aide.core.states import FINAL, PAUSED
from aide.core.tasks import Task

log = logging.getLogger(__name__)

SCHEMA = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["conversation", "quick_question", "new_task", "continuation", "command"],
        },
        "size": {"type": "string", "enum": ["small", "medium", "large"]},
        "external_actions": {"type": "boolean"},
        "related_task": {"type": ["integer", "null"]},
        "needs_plan": {"type": "boolean"},
        "needs_real_browser": {"type": "boolean"},
        "title": {"type": "string"},
        "deliverable": {"type": "boolean"},
        "steps_several": {"type": "boolean"},
        "doubtful": {"type": "boolean"},
    },
    "required": [
        "kind",
        "size",
        "external_actions",
        "related_task",
        "needs_plan",
        "needs_real_browser",
        "title",
        "deliverable",
        "steps_several",
        "doubtful",
    ],
    "additionalProperties": False,
}


class TriageData(BaseModel):
    """The triage verdict on a message: its kind, size, plan and browser needs."""

    kind: Literal["conversation", "quick_question", "new_task", "continuation", "command"]
    size: Literal["small", "medium", "large"]
    external_actions: bool
    related_task: int | None = None
    needs_plan: bool
    needs_real_browser: bool
    title: str = ""
    deliverable: bool = False
    steps_several: bool = False
    doubtful: bool = False


INSURANCE = Triage(kind="new_task", size="medium", needs_plan=True, title=None)

# Asking for a file to be made (“hazme un Excel”, “prepárame el informe en PDF”): it is a task even
# if the model doesn't see it. Only with a make verb: “¿qué dice el PDF?” is not. In Spanish
# and English (“make me an Excel”, “turn this into a PDF”).
RE_FILE = re.compile(
    r"\b(haz|hazme|hacer|hacerme|crea|cr[eé]ame|crear|prepara|prep[aá]rame|preparar|genera|"
    r"gen[eé]rame|generar|redacta|red[aá]ctame|redactar|escribe|escr[ií]beme|elabora|"
    r"elab[oó]rame|monta|m[oó]ntame|exporta|exp[oó]rtame|convierte|convi[eé]rtelo|"
    r"conviertelo|p[aá]salo)\b"
    r"[^.?!\n]{0,60}?\b(word|docx|excel|xlsx|pdf|powerpoint|pptx|presentaci[oó]n|"
    r"hoja de c[aá]lculo|csv|informe|carta)\b"
    # “Pasa esta factura a Excel”; “¿qué pasa con el PDF?” is not.
    r"|(?<!qu[eé] )\bp[aá]sa(?:r|lo|la|los|las|me)?\b[^.?!\n]{0,60}?"
    r"\b(?:word|docx|excel|xlsx|pdf|powerpoint|pptx|csv)\b"
    # “Make me an Excel”, “write a report”, “turn this into a PDF”; “did you write the report?”
    # is not.
    r"|(?<!did you )(?<!have you )\b(?:make|create|prepare|generate|draft|write|build|"
    r"export|convert|turn|produce)\b[^.?!\n]{0,60}?\b(?:word|docx|excel|xlsx|pdf|powerpoint|"
    r"pptx|presentation|slide deck|slides|spreadsheet|csv|report|letter)\b",
    re.IGNORECASE,
)


def asks_file(text: str) -> bool:
    return bool(RE_FILE.search(text or ""))


def apply_rules(data: TriageData, recent: list[Task], text: str = "") -> Triage:
    """Deterministic rules on the model's classification."""
    # A new message only merges with a task that is still running. A finished, cancelled or
    # paused one is continued by replying to one of its messages; otherwise, it is a new task
    # .
    in_running = {t.id for t in recent if t.state_enum not in FINAL and t.state_enum not in PAUSED}
    related = data.related_task if data.related_task in in_running else None
    kind = data.kind
    if kind == "continuation" and related is None:
        kind = "new_task"
    if kind == "command":
        kind = "quick_question"  # real commands arrive with a slash and don't pass through here
    # A small task doesn't get a plan just for having an external action: that action already
    # asks for approval.
    needs_plan = (
        data.needs_plan or data.size == "large" or (data.external_actions and data.size != "small")
    )
    browser = data.needs_real_browser
    # Real work: leaves a file, takes several steps or has an action to approve, or needs real
    # Chrome (the quick session has no Chrome and can't ask for approvals).
    work = (
        data.deliverable
        or asks_file(text)
        or data.steps_several
        or data.external_actions
        or browser
    )
    offer_task = False
    if kind in ("conversation", "quick_question") and work:
        kind = "new_task"
    elif kind == "new_task" and not (work or data.size == "large") and data.kind != "continuation":
        # The model saw it as a task but it leaves nothing and has no steps: it is answered
        # in Telegram, with the button in case they want the task anyway. A continuation
        # that can no longer be continued is still a task: it asked to continue with a job.
        kind, offer_task = "quick_question", True
    if kind == "quick_question" and data.doubtful:
        offer_task = True
    return Triage(
        kind=kind,
        size=data.size,
        external_actions=data.external_actions,
        related_task=related,
        needs_plan=needs_plan,
        needs_real_browser=browser,
        title=(data.title or "").strip()[:80] or None,
        offer_task=offer_task,
    )


def prompt_triage(text: str, recent: list[Task]) -> str:
    """Builds the triage prompt from the user's recent tasks and the new message."""
    items = "\n".join(f"- {t.id}: {t.short_name} ({t.state}, {t.created[:10]})" for t in recent)
    return (
        f"The user's recent tasks:\n{items or '(none)'}\n\n"
        f"New message from the user:\n<message>\n{text}\n</message>"
    )


async def classify(core: Core, text: str, recent: list[Task], task_id: int | None = None) -> Triage:
    """Classifies a message with the triage model, using a safe default on failure."""
    config = core.config
    role = config.role("triage")
    options = triage_options(config, role, SCHEMA, config.settings.caps.triage.usd)
    ses = SessionContext(core=core, task_id=task_id, folder=None, role="triage", phase="triage")
    try:
        async with AgentSession(ses, options, "triage") as session:
            result = await session.turn(prompt_triage(text, recent))
    except Exception:
        log.exception("triage failed; using the safe default")
        return INSURANCE
    if result.kind != "ok" or not isinstance(result.structured, dict):
        log.warning("triage returned no valid output (%s); using the safe default", result.kind)
        return INSURANCE
    try:
        data = TriageData.model_validate(result.structured)
    except ValidationError as e:
        log.warning("invalid triage output: %s", e)
        return INSURANCE
    return apply_rules(data, recent, text)
