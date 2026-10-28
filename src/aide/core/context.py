"""Shared state containers.

`Core` gathers the process services (configuration, database, channel, managers).
`SessionContext` is the state of an SDK session that the hooks and can_use_tool consult.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aide.bot.channel import Channel
from aide.common.config import Config
from aide.core.approvals import ApprovalManager
from aide.core.db import Database
from aide.core.navigation import NavigationState
from aide.core.policy import CallContext, Decision, Policy
from aide.core.questions import QuestionManager
from aide.core.tasks import TaskRepository
from aide.core.usage import UsageLog

if TYPE_CHECKING:
    from aide.core.idempotency import ActionLog

log = logging.getLogger(__name__)


@dataclass
class Core:
    """The shared services of the agent: config, database, tasks, usage and approvals."""

    config: Config
    db: Database
    tasks: TaskRepository
    usage: UsageLog
    policy: Policy
    approvals: ApprovalManager
    questions: QuestionManager
    ref_channel: Callable[[], Channel | None]
    actions: ActionLog | None = None
    # Sends a warning to the user linked to a task (the engine implements it).
    notify: Callable[[int | None, str], Awaitable[None]] | None = None
    # Optional services registered in later phases (Google, browser, memory...).
    services: dict[str, Any] = field(default_factory=dict)

    @property
    def channel(self) -> Channel | None:
        return self.ref_channel()

    @property
    def paths(self) -> Any:
        return self.config.paths


@dataclass
class ExternalFragment:
    """A piece of external text with the source it came from."""

    source: str
    text: str


@dataclass
class SessionContext:
    """The state of one agent session: task, folder, role, phase and Chrome data."""

    core: Core
    task_id: int | None
    folder: Path | None
    role: str  # orchestrator | quick | review | triage
    phase: str = "run"  # plan | run | quick | review
    chrome: bool = False
    chrome_elements: dict[str, dict[str, str]] = field(default_factory=dict)
    password_focus: bool = False
    agents_by_id: dict[str, str] = field(default_factory=dict)
    decisions: dict[str, Decision] = field(default_factory=dict)
    external_content: list[ExternalFragment] = field(default_factory=list)
    suspicious_injection: str | None = None
    written_files: set[str] = field(default_factory=set)
    sent_notices: set[str] = field(default_factory=set)
    reports: int = 0
    # Per tab, the screenshot that still shows its page: nothing has changed it since.
    screenshots: dict[str, Path] = field(default_factory=dict)
    browser_errors: int = 0
    session_id: str | None = None
    # What the user has written in this task (to tell their data from external data).
    user_texts: list[str] = field(default_factory=list)
    # Google authorization expired during this session (the task is paused when the turn ends).
    expired_google: bool = False
    # Small task.
    small: bool = False
    # Last known address of each real Chrome tab.
    urls_chrome: dict[str, str] = field(default_factory=dict)
    # Navigation in the real Chrome: what the task carries (loaded from the database once
    # per session) and the usage for its budget: the task's before the turn and that of each
    # model message in the current turn.
    navigation: NavigationState = field(default_factory=NavigationState)
    loaded_navigation: bool = False
    previous_tokens: int = 0
    tokens_messages: dict[str, int] = field(default_factory=dict)
    cap_exceeded_tokens: bool = False
    # The task's own token budget, even if it does not use Chrome.
    cap_tokens_task: int | None = None
    # The agent has already delivered: the budget does not cut the closing turns (the missing
    # delivery, the review corrections, the CONTEXT.md); the action limit still applies.
    done_delivery: bool = False

    def call(self, agent: str | None = None) -> CallContext:
        """Builds a CallContext, using the reviewer agent for review and quick sessions."""
        # Review and quick reply sessions change nothing, like the reviewer.
        if agent is None and self.role in ("review", "quick"):
            agent = "reviewer"
        return CallContext(
            task_id=self.task_id,
            task_folder=self.folder,
            agent=agent,
            phase=self.phase,
            chrome_elements=self.chrome_elements,
            password_focus=self.password_focus,
            small=self.small,
            urls_chrome=self.urls_chrome,
            browser_actions=self.navigation.actions,
            tokens_task=self.spent_tokens(),
            token_cap=self.chrome_token_cap(),
        )

    def spent_tokens(self) -> int:
        """Token usage of the task: what is recorded and that of the current turn."""
        return self.previous_tokens + sum(self.tokens_messages.values())

    def chrome_token_cap(self) -> int:
        """Budget at which the policy stops browsing: that of tasks with Chrome or, if smaller, the
        task's own."""
        chrome = self.navigation.token_cap or self.core.config.settings.real_browser.max_tokens
        return min(chrome, self.cap_tokens_task) if self.cap_tokens_task else chrome

    def current_token_cap(self) -> tuple[int, str] | None:
        """Budget that cuts the turn and what it belongs to: the task's own and, if it has used the
        real Chrome, that of tasks with Chrome; the smaller of the two. None if it has none."""
        caps: list[tuple[int, str]] = []
        if self.cap_tokens_task:
            caps.append((self.cap_tokens_task, "task"))
        if self.navigation.actions:
            chrome = self.navigation.token_cap or self.core.config.settings.real_browser.max_tokens
            caps.append((chrome, "chrome"))
        return min(caps) if caps else None

    @property
    def policy(self) -> Policy:
        return self.core.policy
