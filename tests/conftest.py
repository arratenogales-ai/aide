"""Common test utilities."""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from aide.common.config import Config
from aide.common.paths import Paths
from aide.core.authentication import AuthenticationResult
from aide.core.engine import Engine
from tests.fake_sdk import FakeClient, Recorder

REAL_ROOT = Path(__file__).resolve().parents[1]
MINIMAL_PROFILE = "# User profile\n\n## How I want you to work\n- Keep it short and friendly.\n"
MINIMAL_MEMORY = "# Aide memory\n\nLasting facts approved by the user.\n"
DEFAULT_SCRIPT = vars(FakeClient)["script"]
TESTS_ZONE = "Europe/Berlin"


@pytest.fixture(autouse=True)
def never_the_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """No test calls the model (10-27-2026): if something tried to open a real session with the
    SDK, the test fails instead of spending subscription."""
    from claude_agent_sdk import ClaudeSDKClient

    async def blocked(self: Any, *args: Any, **kwargs: Any) -> None:
        raise RuntimeError("una prueba ha intentado llamar al modelo de verdad")

    monkeypatch.setattr(ClaudeSDKClient, "connect", blocked)


@pytest.fixture(autouse=True)
def fake_keychain(monkeypatch: pytest.MonkeyPatch) -> dict[tuple[str, str], str]:
    """No test reads or writes the real keychain: an in-memory one is used."""
    import keyring

    saved: dict[tuple[str, str], str] = {}

    def delete(service: str, account: str) -> None:
        saved.pop((service, account), None)

    monkeypatch.setattr(keyring, "get_password", lambda s, c: saved.get((s, c)))
    monkeypatch.setattr(keyring, "set_password", lambda s, c, v: saved.__setitem__((s, c), v))
    monkeypatch.setattr(keyring, "delete_password", delete)
    return saved


@pytest.fixture(autouse=True)
def fake_trash(monkeypatch: pytest.MonkeyPatch) -> Any:
    """No test touches the real Trash: what the inbox sends to the Trash is deleted and
    recorded in `discarded`; `real` is the real function, to test it separately."""
    from types import SimpleNamespace

    from aide.paperwork import inbox

    trash = SimpleNamespace(discarded=[], real=inbox.to_trash, fails=False)

    def fake(path: Path) -> bool:
        if trash.fails:
            return False
        trash.discarded.append(path)
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return True

    monkeypatch.setattr(inbox, "to_trash", fake)
    return trash


@pytest.fixture(autouse=True)
def no_minimal_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """The engine does not make the minimal request to the model (the one that checks the login
    and brings the plan limits): it would spend subscription and take time."""

    async def fake(self: Engine, browsers: bool = False) -> AuthenticationResult:
        return AuthenticationResult(ok=True)

    monkeypatch.setattr(Engine, "_minimal_request", fake)


@pytest.fixture(autouse=True)
def no_real_model(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """No SDK session calls the real model: the fake client runs with its default script (the
    previous test's is not carried over; those that need one set their own). If a script
    raises an exception, the test fails."""
    from aide.core.orchestrator import AgentSession

    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    monkeypatch.setattr(FakeClient, "script", DEFAULT_SCRIPT)
    monkeypatch.setattr(FakeClient, "registry", Recorder())
    yield
    if FakeClient.registry.script_errors:
        pytest.fail(
            "falló el guion del modelo simulado: " + "; ".join(FakeClient.registry.script_errors)
        )


@pytest.fixture(autouse=True)
def connection_without_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """The connection check does not go out to the internet in tests: there is a
    connection, unless a test says otherwise."""
    from aide.core.connection import Connection

    async def with_network(self: Connection) -> bool:
        return True

    monkeypatch.setattr(Connection, "_test", with_network)


@pytest.fixture
def tmp_paths(tmp_path: Path) -> Paths:
    """Temporary project with the real configuration copied and a minimal agent/ folder."""
    root = tmp_path / "project"
    (root / "config").mkdir(parents=True)
    for name in ("models.yaml", "settings.yaml", "permissions.yaml"):
        shutil.copy(REAL_ROOT / "config" / name, root / "config" / name)
    agent = root / "agent"
    (agent / ".claude" / "skills" / "natural-writing").mkdir(parents=True)
    shutil.copy(
        REAL_ROOT / "agent" / ".claude" / "skills" / "natural-writing" / "SKILL.md",
        agent / ".claude" / "skills" / "natural-writing" / "SKILL.md",
    )
    shutil.copy(REAL_ROOT / "agent" / "CLAUDE.md", agent / "CLAUDE.md")
    # profile.md and memory.md hold personal data and are not in the repository: tests always
    # use minimal ones, so as not to depend on anything that is not in git.
    (agent / "profile.md").write_text(MINIMAL_PROFILE, encoding="utf-8")
    (agent / "memory.md").write_text(MINIMAL_MEMORY, encoding="utf-8")
    paths = Paths(root=root)
    paths.ensure()
    return paths


@pytest.fixture
def config_tmp(tmp_paths: Paths) -> Config:
    config = Config.load(tmp_paths)
    # Date and time tests run in a zone with a clock change (the example settings.yaml uses
    # UTC).
    config.settings.time_zone = TESTS_ZONE
    # Gmail reading is tested separately (test_email.py): here, off even if the real one is on,
    # so that no test looks at a Gmail unintentionally.
    config.settings.email.active = False
    # And in normal mode, even if the real configuration is in the no-wifi days: the
    # tests for that mode turn it on.
    config.settings.no_wifi.active = False
    # Same for the morning summary: its tests turn it on.
    config.settings.morning_summary.active = False
    # The usual tests run with /login and the real Chrome; those for the API key (what
    # settings.yaml brings) change it themselves.
    config.settings.claude.credential = "login"
    config.settings.real_browser.enabled = True
    return config
