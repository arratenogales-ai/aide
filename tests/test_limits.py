"""Plan limits: where the data comes from, when new data is requested and how it is shown."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from claude_agent_sdk import RateLimitEvent, RateLimitInfo

from aide.bot.channel import Entry
from aide.bot.commands import run_command
from aide.core import authentication, engine_checks
from aide.core.authentication import AuthenticationResult, check_authentication
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.panel.views import Views
from aide.simulate import Script, Simulator
from tests.fake_sdk import FakeClient, Recorder

# The real minimal request (conftest replaces it in each test; here the original is tested).
ORIGINAL_REQUEST = Engine._minimal_request
ZONE = "Europe/Berlin"
USER = 1


@pytest.fixture
async def environment(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    engine = Engine(config_tmp)
    await engine.start()
    engine.commands = run_command
    sim = Simulator(engine, Script(), print_out=False)
    yield engine, sim
    await engine.stop()


def info(
    state: str = "allowed",
    kind: str | None = "five_hour",
    resets_at: float | None = None,
    windows: dict[str, Any] | None = None,
) -> RateLimitInfo:
    raw: dict[str, Any] = {"status": state}
    if windows is not None:
        raw["unifiedWindows"] = windows
    return RateLimitInfo(status=state, resets_at=resets_at, rate_limit_type=kind, raw=raw)


def window(utilization: float, reset: datetime) -> dict[str, Any]:
    return {"utilization": utilization, "resetsAt": int(reset.timestamp())}


async def save(engine: Engine, datum: RateLimitInfo, collected: datetime | None = None) -> None:
    await engine.usage.log_limit(datum)
    if collected is not None:
        await engine.db.run(
            "UPDATE limits SET hour=? WHERE id=(SELECT MAX(id) FROM limits)",
            (collected.isoformat(timespec="seconds"),),
        )


async def make_older(engine: Engine, age: timedelta) -> None:
    await engine.db.run(
        "UPDATE limits SET hour=?",
        ((datetime.now(UTC) - age).isoformat(timespec="seconds"),),
    )


# --- limit text ----------------------------------------------------------------------------


async def test_no_data(environment):
    engine, _ = environment
    assert await engine.usage.limits_text(ZONE) == "There's no data on the plan limits yet."


async def test_recent_data_is_shown_without_notice(environment):
    engine, _ = environment
    now = datetime.now(UTC)
    windows = {
        "five_hour": window(0.1, now + timedelta(hours=3)),
        "seven_day": window(0.61, now + timedelta(days=3)),
    }
    await save(engine, info(windows=windows))
    text = await engine.usage.limits_text(ZONE)
    assert "Limit data from" not in text
    assert "5-hour window ▰▱▱▱▱▱▱▱▱▱ 10% used, 90% free, resets" in text
    assert "Weekly limit ▰▰▰▰▰▰▱▱▱▱ 61% used, 39% free, resets" in text
    assert "Limit reached" not in text


async def test_real_case_yesterdays_data_with_the_window_already_reset(environment):
    """On 20/10 at 12:12, the data was from 19/10 and the 5-hour window had reset on 19/10 at
    20:50: it cannot be shown as if it were the current state."""
    engine, _ = environment
    collected = datetime(2026, 10, 19, 14, 5, tzinfo=UTC)  # 16:05 in Munich
    windows = {
        "five_hour": window(0.43, datetime(2026, 10, 19, 18, 50, tzinfo=UTC)),  # 20:50
        "seven_day": window(0.61, datetime(2026, 10, 23, 7, 0, tzinfo=UTC)),  # 09:00
    }
    await save(engine, info(windows=windows), collected)
    moment = datetime(2026, 10, 20, 10, 12, tzinfo=UTC)  # 12:12 in Munich
    assert await engine.usage.limits_text(ZONE, moment) == (
        "Limit data from 20 h ago (yesterday at 16:05).\n"
        "The 5-hour window reset yesterday at 20:50; there's no usage data since then.\n"
        "Weekly limit ▰▰▰▰▰▰▱▱▱▱ 61% used, 39% free, resets on Oct 23 at 09:00."
    )


async def test_valid_rejection_and_already_reset_rejection(environment):
    engine, _ = environment
    now = datetime.now(UTC)
    # Rejection with data per window: valid as long as the window has not reset.
    windows = {"five_hour": window(1.0, now + timedelta(hours=1))}
    await save(engine, info("rejected", windows=windows))
    assert "Limit reached (5-hour window)." in await engine.usage.limits_text(ZONE)
    after = now + timedelta(hours=2)
    text = await engine.usage.limits_text(ZONE, after)
    assert "Limit reached" not in text and "reset" in text

    # Rejection without data per window: the event's own reset time applies.
    reset = now + timedelta(minutes=30)
    await save(engine, info("rejected", resets_at=reset.timestamp()))
    assert "Limit reached (5-hour window)." in await engine.usage.limits_text(ZONE)
    text = await engine.usage.limits_text(ZONE, now + timedelta(hours=1))
    assert "Limit reached" not in text and text.startswith("Limit data from")
    assert "The limit reset " in text


async def test_state_without_windows_uses_no_labels(environment):
    engine, _ = environment
    await save(engine, info("allowed_warning", windows={}))
    assert await engine.usage.limits_text(ZONE) == "Close to a plan limit."


# --- request for new data ------------------------------------------------------------------


def saving_request(engine: Engine, calls: list[int], wait: float = 0) -> Any:
    async def request() -> AuthenticationResult:
        calls.append(1)
        await asyncio.sleep(wait)
        now = datetime.now(UTC)
        await engine.usage.log_limit(
            info(windows={"five_hour": window(0.1, now + timedelta(hours=4))})
        )
        return AuthenticationResult(ok=True)

    return request


async def test_data_is_only_requested_if_the_stored_data_is_old(environment, monkeypatch):
    engine, _ = environment
    calls: list[int] = []
    engine.request_limits = saving_request(engine, calls)

    assert await engine.refresh_limits() is True  # no data: they are requested
    assert await engine.refresh_limits() is False  # just arrived: no
    assert len(calls) == 1

    await make_older(engine, timedelta(minutes=30))
    # Old, but just requested: the request is not repeated on every query.
    assert await engine.refresh_limits() is False
    assert len(calls) == 1

    monkeypatch.setattr(engine_checks, "LIMIT_ATTEMPTS_WAIT", 0)
    assert await engine.refresh_limits() is True
    assert len(calls) == 2
    assert "Limit data from" not in await engine.usage.limits_text(ZONE)


async def test_simultaneous_requests_make_a_single_call(environment):
    engine, _ = environment
    calls: list[int] = []
    engine.request_limits = saving_request(engine, calls, wait=0.1)
    results = await asyncio.gather(*(engine.refresh_limits() for _ in range(3)))
    assert sorted(results) == [False, False, True]
    assert len(calls) == 1


async def test_if_the_request_fails_nothing_breaks(environment):
    engine, _ = environment

    async def fails() -> None:
        raise RuntimeError("no network")

    engine.request_limits = fails
    assert await engine.refresh_limits() is False
    assert await engine.usage.limits_text(ZONE) == "There's no data on the plan limits yet."


async def test_minimal_request_stores_the_limits(environment, monkeypatch):
    engine, _ = environment
    received: dict[str, Any] = {}

    async def check(config, at_limit=None, browsers=True):
        received["browsers"] = browsers
        now = datetime.now(UTC)
        await at_limit(info(windows={"five_hour": window(0.2, now + timedelta(hours=1))}))
        return AuthenticationResult(ok=True)

    monkeypatch.setattr(authentication, "check_authentication", check)
    result = await ORIGINAL_REQUEST(engine)
    assert result.ok and received["browsers"] is False
    assert engine.core.services["authentication"] is result
    assert "5-hour window ▰▰▱▱▱▱▱▱▱▱ 20% used" in await engine.usage.limits_text(ZONE)


async def test_checking_authentication_delivers_the_limits_event(config_tmp, monkeypatch):
    class MinimalClient:
        def __init__(self, options: Any) -> None:
            pass

        async def __aenter__(self) -> MinimalClient:
            return self

        async def __aexit__(self, *_: Any) -> None:
            return None

        async def get_server_info(self) -> dict[str, Any]:
            return {"account": {}}

        async def query(self, _: str) -> None:
            return None

        async def receive_response(self):
            yield RateLimitEvent(info(), "u1", "s1")
            yield RateLimitEvent(info("allowed_warning"), "u2", "s1")

    monkeypatch.setattr(authentication, "ClaudeSDKClient", MinimalClient)
    received: list[str] = []

    async def at_limit(datum: RateLimitInfo) -> None:
        received.append(datum.status)
        if len(received) == 1:
            raise RuntimeError("database busy")  # does not interrupt the check

    await check_authentication(config_tmp, at_limit=at_limit, browsers=False)
    assert received == ["allowed", "allowed_warning"]


# --- where they are used: Telegram and panel -----------------------------------------------


async def command(engine: Engine, text: str) -> None:
    name, _, rest = text.lstrip("/").partition(" ")
    await engine.receive(Entry("command", USER, text=text, command=name, arguments=rest))


async def test_status_and_usage_request_new_data_if_old(environment, monkeypatch):
    engine, sim = environment
    yesterday = datetime.now(UTC) - timedelta(days=1)
    old = {"five_hour": window(0.43, yesterday + timedelta(hours=4))}
    await save(engine, info(windows=old), yesterday)
    calls: list[int] = []
    engine.request_limits = saving_request(engine, calls)

    await command(engine, "/status")
    text = sim.channel.texts()[-1]
    assert "10% used, 90% free" in text and "Limit data from" not in text

    await make_older(engine, timedelta(hours=2))
    monkeypatch.setattr(engine_checks, "LIMIT_ATTEMPTS_WAIT", 0)
    await command(engine, "/usage")
    assert "10% used, 90% free" in sim.channel.texts()[-1]
    assert len(calls) == 2


async def test_status_warns_if_there_is_no_new_data(environment):
    engine, sim = environment
    yesterday = datetime.now(UTC) - timedelta(days=1)
    await save(
        engine, info(windows={"five_hour": window(0.43, yesterday + timedelta(hours=4))}), yesterday
    )

    async def fails() -> None:
        raise RuntimeError("no network")

    engine.request_limits = fails
    await command(engine, "/status")
    text = sim.channel.texts()[-1]
    assert "Limit data from 24 h ago" in text
    assert "The 5-hour window reset" in text and "43%" not in text


async def test_opening_the_panel_does_not_request_the_limits(environment):
    """The panel does not show usage or limits: opening “Today” does not make the
    minimal request to the model (the limits remain in /usage and /status)."""
    engine, _ = environment
    calls: list[int] = []
    engine.request_limits = saving_request(engine, calls)
    data = await Views(engine).start()
    await asyncio.sleep(0.1)
    assert "limits" not in data and calls == []
