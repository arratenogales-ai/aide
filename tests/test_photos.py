"""Photos: with a question, a quick Sonnet reply that sees the image; with no text, it asks
what to do."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aide.application import configure_engine
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.core.states import TaskState
from aide.simulate import Script, Simulator
from tests.fake_sdk import End, FakeClient, Recorder
from tests.test_agents import _data, triage_returning

PNG = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00"
    b"\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00"
    b"\x00IEND\xaeB`\x82"
)


@pytest.fixture
async def environment(config_tmp, monkeypatch, tmp_path):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    engine = Engine(config_tmp)
    await engine.start()
    await configure_engine(engine, simulated=True)
    sim = Simulator(engine, Script(), print_out=False)
    photo = tmp_path / "dish.jpg"
    photo.write_bytes(PNG)
    yield engine, sim, photo
    await engine.stop()


def quick_reply(prompts: list[str]):
    def next_step(prompt, options, registry):
        prompts.append(prompt)
        return [End("About 650 kcal, roughly.", model=options.model)]

    return next_step


async def test_photo_with_a_question_is_a_quick_reply_that_sees_the_image(environment):
    engine, sim, photo = environment
    prompts: list[str] = []
    FakeClient.script = staticmethod(
        triage_returning({**_data(kind="quick_question").model_dump()}, quick_reply(prompts))
    )
    [task_id] = await sim.send("How many calories does this dish have?", photo=photo)
    task = await engine.tasks.require(task_id)
    assert task.kind == "quick" and task.state == TaskState.COMPLETED
    # The image is saved in the task folder and the quick reply receives it to look at.
    [entry] = await engine.tasks.files(task_id, "entry")
    assert entry["path"].endswith("dish.jpg")
    assert entry["path"] in prompts[-1] and "Look at it with Read" in prompts[-1]
    # Sonnet answers (the quick-reply model).
    assert FakeClient.registry.options[-1].model == "claude-sonnet-5-5"
    # The triage knows there is a photo.
    assert "[The user sends a photo" in FakeClient.registry.queries[0]
    assert "roughly" in sim.channel.texts()[-1]


async def test_photo_without_text_asks_and_uses_the_next_message(environment):
    engine, sim, photo = environment
    prompts: list[str] = []
    FakeClient.script = staticmethod(
        triage_returning({**_data(kind="quick_question").model_dump()}, quick_reply(prompts))
    )
    assert await sim.send("", photo=photo) == []  # does not create anything yet
    assert sim.channel.texts()[-1] == "I got the photo. What should I do with it?"
    [task_id] = await sim.send("How many calories does it have?")
    [entry] = await engine.tasks.files(task_id, "entry")
    assert entry["path"].endswith("dish.jpg") and entry["path"] in prompts[-1]
    # The next message no longer carries the photo.
    [other] = await sim.send("And how much protein?")
    assert await engine.tasks.files(other, "entry") == []


async def test_the_pending_photo_expires_after_30_minutes(environment):
    engine, sim, photo = environment
    prompts: list[str] = []
    FakeClient.script = staticmethod(
        triage_returning({**_data(kind="quick_question").model_dump()}, quick_reply(prompts))
    )
    await sim.send("", photo=photo)
    engine.pending_photo.hour = datetime.now(UTC) - timedelta(minutes=31)
    [task_id] = await sim.send("What time is it in Tokyo?")
    assert await engine.tasks.files(task_id, "entry") == []
    assert "Look at it with Read" not in prompts[-1]


async def test_photo_with_a_request_is_a_task_with_the_image(environment):
    engine, sim, photo = environment
    FakeClient.script = staticmethod(
        triage_returning(
            {**_data(kind="new_task").model_dump()},
            lambda p, o, r: [End("<delivery>Hecho.</delivery>", model=o.model)],
        )
    )
    [task_id] = await sim.send("Turn this invoice into an Excel table", photo=photo)
    task = await engine.tasks.require(task_id)
    assert task.kind == "task"
    [entry] = await engine.tasks.files(task_id, "entry")
    assert entry["path"].endswith("dish.jpg")
