"""Cost of navigation in real Chrome.

A task that reads many pages with real Chrome can burn tens of millions of tokens:
hundreds of actions and screenshots that are reread at every step. This holds what the policy
and the hooks share to bound it in each task: how many browser actions a call carries (in a
batch, each one) and the task's token budget.

What is counted per task is saved in the database (`navigation:<id>`), so that it holds
also after a pause or a restart.
"""

from __future__ import annotations

import logging
import re
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from aide.core.context import SessionContext
    from aide.core.db import Database

log = logging.getLogger(__name__)

PREFIX = "mcp__claude-in-chrome__"
# Calls that don't count as browser actions: they only arrange tabs.
NO_COST = {
    "tabs_context_mcp",
    "tabs_close_mcp",
    "list_connected_browsers",
    "select_browser",
    "switch_browser",
}
SCREENSHOTS = {"screenshot", "zoom"}


# --- actions of a call ----------------------------------------------------------------------


def steps(name: str, data: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """The actions of a call to real Chrome: the call itself or, in a batch, each of its own."""
    if name != "browser_batch":
        return [(name, data)]
    items = []
    for a in data.get("actions") or []:
        if isinstance(a, dict):
            internal = str(a.get("tool") or a.get("name") or "")
            entry = a.get("input") or a.get("args") or a
            items.append((internal.removeprefix(PREFIX), entry if isinstance(entry, dict) else {}))
    return items


def actions(name: str, data: dict[str, Any]) -> int:
    """Browser actions that a call counts (those of a batch, one by one)."""
    if name in NO_COST:
        return 0
    if name == "browser_batch":
        inside = steps(name, data)
        return sum(1 for p, _ in inside if p not in NO_COST) if inside else 1
    return 1


# --- addresses -------------------------------------------------------------------------------


def as_browser(url: str) -> str:
    """The address as the browser understands it: “https:///host”, “https:\\host” or “//host”
    lead to “host”. Without this, a domain rule didn't see the domain."""
    text = re.sub(r"^(https?:)[/\\]+", r"\1//", (url or "").strip(), flags=re.IGNORECASE)
    return "https:" + text if text.startswith("//") else text


# --- state of a task ---------------------------------------------------------------------------


@dataclass
class NavigationState:
    """What a task carries in real Chrome (saved in the database)."""

    actions: int = 0
    token_cap: int | None = None  # budget extended by the user (None: the one from settings)

    @classmethod
    def from_dict(cls, data: Any) -> NavigationState:
        """Builds a NavigationState from a dict, using defaults if it is not a dict."""
        if not isinstance(data, dict):
            return cls()
        return cls(
            actions=int(data.get("actions") or 0),
            token_cap=int(data["token_cap"]) if data.get("token_cap") else None,
        )


def kv_key(task_id: int) -> str:
    return f"navigation:{task_id}"


async def load(db: Database, task_id: int) -> NavigationState:
    return NavigationState.from_dict(await db.kv_read(kv_key(task_id)))


async def save(db: Database, task_id: int, state: NavigationState) -> None:
    await db.kv_put(kv_key(task_id), asdict(state))


async def prepare(ses: SessionContext) -> None:
    """Loads once per session what the task carries in real Chrome. If the database doesn't
    respond, it tries again on the next call and meanwhile nothing is saved, so as not to
    erase what was already there."""
    if ses.loaded_navigation or ses.task_id is None:
        return
    try:
        state = await load(ses.core.db, ses.task_id)
    except Exception:
        log.exception("couldn't read the browser use of task %s", ses.task_id)
        return
    if not ses.loaded_navigation:
        # What was counted before it could load (if the database failed) is added to what
        # was saved.
        state.actions += ses.navigation.actions
        ses.navigation = state
        ses.loaded_navigation = True


async def save_in_session(ses: SessionContext) -> None:
    """Saves what the task carries, only if it was already loaded (otherwise it would erase what is in the database)."""
    if ses.task_id is None or not ses.loaded_navigation:
        return
    try:
        await save(ses.core.db, ses.task_id, ses.navigation)
    except Exception:
        log.exception("couldn't save the browser use of task %s", ses.task_id)
