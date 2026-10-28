"""F3: triage, quick answers, prior plan, subagents and review (without a model)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from aide.application import configure_engine
from aide.core import session as session_module
from aide.core.context import SessionContext
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.core.states import TaskState
from aide.core.subagents import DEFINITIONS, subagents_definitions
from aide.core.triage import TriageData, apply_rules
from aide.simulate import Script, Simulator
from tests.fake_sdk import End, FakeClient, Recorder, Tool, write


def _data(**changes) -> TriageData:
    base = dict(
        kind="new_task",
        size="small",
        external_actions=False,
        related_task=None,
        needs_plan=False,
        needs_real_browser=False,
        title="Something",
    )
    base.update(changes)
    return TriageData(**base)


class MinimalTask:
    def __init__(self, i, state=TaskState.RUNNING):
        self.id = i
        self.state = state
        self.state_enum = TaskState(state)


def test_triage_rules():
    assert apply_rules(_data(size="large"), []).needs_plan
    # With an external action: plan if medium; a small one does not (the action already asks for
    # approval).
    t = apply_rules(_data(size="medium", external_actions=True), [])
    assert t.needs_plan and t.kind == "new_task"
    t = apply_rules(_data(size="small", external_actions=True), [])
    assert not t.needs_plan and t.kind == "new_task"
    assert apply_rules(_data(size="small", needs_plan=True), []).needs_plan
    t = apply_rules(_data(kind="quick_question", external_actions=True), [])
    assert t.kind == "new_task"  # anything with external actions is never a quick answer
    t = apply_rules(_data(kind="continuation", related_task=99), [MinimalTask(3)])
    assert t.kind == "new_task" and t.related_task is None
    t = apply_rules(_data(kind="continuation", related_task=3), [MinimalTask(3)])
    assert t.kind == "continuation" and t.related_task == 3
    # A finished task is not continued by triage: only by replying to its message.
    finished = [MinimalTask(3, TaskState.COMPLETED)]
    t = apply_rules(_data(kind="continuation", related_task=3), finished)
    assert t.kind == "new_task" and t.related_task is None
    assert apply_rules(_data(kind="command"), []).kind == "quick_question"
    # The quick session does not have the real Chrome: anything that needs it is a task.
    t = apply_rules(_data(kind="quick_question", needs_real_browser=True), [])
    assert t.kind == "new_task" and t.needs_real_browser


def test_subagents_follow_the_model_policy(config_tmp):
    agents = subagents_definitions(config_tmp, chrome=False)
    assert set(agents) == {
        "researcher",
        "reader",
        "builder",
        "writer",
        "reviewer",
        "quick_reviewer",
    }
    assert (
        agents["researcher"].model == "claude-sonnet-5-5"
        and agents["researcher"].effort == "medium"
    )
    assert agents["reader"].model == "claude-haiku-5-5" and agents["reader"].effort == "low"
    assert agents["builder"].effort == "high"
    assert agents["reviewer"].model == "claude-opus-5-5"
    assert agents["writer"].skills == ["natural-writing"]
    assert "WebSearch" not in agents["reader"].tools and "WebFetch" not in agents["reader"].tools
    assert "Write" not in agents["reviewer"].tools and "Edit" not in agents["reviewer"].tools
    for a in agents.values():
        assert "SUMMARY (300 words at most)" in a.prompt and a.maxTurns
    with_chrome = subagents_definitions(config_tmp, chrome=True)
    assert "navigator" in with_chrome
    assert all(
        h.startswith("mcp__claude-in-chrome__") or h in ("Read", "Write", "ToolSearch")
        for h in with_chrome["navigator"].tools
    )
    fallback = subagents_definitions(config_tmp, chrome=False, model_fallback_opus=True)
    assert fallback["reviewer"].model == "claude-sonnet-5-5"
    # No other subagent uses the real Chrome.
    for name, a in with_chrome.items():
        if name != "navigator":
            assert not any(h.startswith("mcp__claude-in-chrome__") for h in a.tools), name


@pytest.fixture
async def environment(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    engine = Engine(config_tmp)
    await engine.start()
    await configure_engine(engine)
    sim = Simulator(engine, Script(), print_out=False)
    yield engine, sim
    await engine.stop()


def triage_returning(data: dict, next_step):
    """Script: the triage session returns `data`; `next_step` decides the rest."""

    def script(prompt, options, registry):
        if options.output_format and "kind" in options.output_format["schema"]["properties"]:
            return [End("", structured=data, model=options.model)]
        return next_step(prompt, options, registry)

    return script


async def test_quick_question_does_not_use_opus(environment):
    engine, sim = environment
    FakeClient.script = staticmethod(
        triage_returning(
            {**_data(kind="quick_question").model_dump()},
            lambda p, o, r: [End("Bern is the capital of Switzerland.", model=o.model)],
        )
    )
    [task_id] = await sim.send("What is the capital of Switzerland?")
    task = await engine.tasks.require(task_id)
    assert task.kind == "quick" and task.state == TaskState.COMPLETED
    assert "Bern" in sim.channel.texts()[-1]
    models = [o.model for o in FakeClient.registry.options]
    assert models == ["claude-haiku-5-5", "claude-sonnet-5-5"]  # triage and quick answer
    quick = FakeClient.registry.options[-1]
    assert "Write" not in quick.tools and "Bash" not in quick.tools
    used = await engine.usage.by_task(task_id)
    assert not any("opus" in m for m in used)


async def test_quick_session_is_reused(environment):
    engine, sim = environment
    FakeClient.script = staticmethod(
        triage_returning(
            {**_data(kind="quick_question").model_dump()},
            lambda p, o, r: [End("Answer.", model=o.model)],
        )
    )
    await sim.send("What do I have tomorrow?")
    await sim.send("And the day after?")
    quick = [o for o in FakeClient.registry.options if o.model == "claude-sonnet-5-5"]
    assert quick[0].resume is None and quick[1].resume is not None


async def test_big_task_asks_for_a_plan(environment):
    engine, sim = environment
    sim.script.default_plan = "nothing"

    def rest(prompt, options, registry):
        if "PLAN PHASE" in prompt:
            return [End("<plan>Goal: market study.</plan>", model=options.model)]
        return [End("<delivery>Done.</delivery>")]

    FakeClient.script = staticmethod(triage_returning({**_data(size="large").model_dump()}, rest))
    [task_id] = await sim.send("Research the e-bike market in Europe")
    task = await engine.tasks.require(task_id)
    assert task.state == TaskState.WAITING_PLAN and task.needs_plan == 1
    assert task.turn_cap == 150 and task.usd_cap == 12.0  # caps for a large task
    plan = sim.channel.last_with_buttons("plan:")
    assert plan is not None and "market study" in plan.plain_text


async def test_triage_continuation_does_not_reopen_a_finished_task(environment):
    """If triage takes a loose message for the continuation of a finished task, it is a new
    task. Before, the finished one was reopened."""
    engine, sim = environment
    FakeClient.script = staticmethod(
        triage_returning(
            # Leaves a file: it is a task (without a deliverable it would be a quick
            # answer).
            {**_data(deliverable=True).model_dump()},
            lambda p, o, r: [End("<delivery>First.</delivery>")],
        )
    )
    [first] = await sim.send("Write a poem in a Word file")
    FakeClient.script = staticmethod(
        triage_returning(
            {**_data(kind="continuation", related_task=first).model_dump()},
            lambda p, o, r: [End("<delivery>Longer now.</delivery>")],
        )
    )
    affected = await sim.send("Make it longer")
    new = await engine.tasks.require(affected[0])
    assert affected == [new.id] and new.kind == "task" and new.parent_task is None
    assert new.result == "Longer now."
    assert (await engine.tasks.require(first)).result == "First."


async def test_review_with_one_correction_round(environment):
    engine, sim = environment
    verdicts = [
        {
            "approved": False,
            "failures": ["The source for figure 2 is missing"],
            "checks": ["opened the report"],
        },
        {"approved": True, "failures": [], "checks": ["all sources present"]},
    ]
    prompts_reviewer = []

    def rest(prompt, options, registry):
        if options.output_format and "approved" in options.output_format["schema"]["properties"]:
            prompts_reviewer.append((prompt, options.model))
            return [End("", structured=verdicts.pop(0), model=options.model)]
        folder = (
            prompt.split("Task folder: ")[1].split("\n")[0] if "Task folder" in prompt else None
        )
        steps = []
        if folder:
            destination = f"{folder}/results/report.md"
            steps.append(
                Tool("Write", {"file_path": destination, "content": "Report"}, write(destination))
            )
        return [*steps, End("<delivery>Report ready.</delivery>")]

    FakeClient.script = staticmethod(triage_returning({**_data(size="medium").model_dump()}, rest))
    [task_id] = await sim.send("Write a report about X")
    task = await engine.tasks.require(task_id)
    assert task.state == TaskState.COMPLETED and task.reviews == 2
    # The reviewer only sees the request, the plan and the deliverables.
    text, model = prompts_reviewer[0]
    assert "Write a report about X" in text and "results/report.md" in text
    assert "Report ready" not in text
    assert model == "claude-sonnet-5-5"  # medium task: quick reviewer
    assert any("The source for figure 2 is missing" in c for c in FakeClient.registry.queries)
    transitions = [t["until"] for t in await engine.tasks.transitions(task_id)]
    assert transitions.count("reviewing") == 2


async def test_review_that_keeps_failing_is_delivered_with_the_failures(environment):
    engine, sim = environment

    def rest(prompt, options, registry):
        if options.output_format and "approved" in options.output_format["schema"]["properties"]:
            return [
                End(
                    "",
                    structured={
                        "approved": False,
                        "failures": ["The chart is empty"],
                        "checks": [],
                    },
                    model=options.model,
                )
            ]
        return [End("<delivery>Done.</delivery>")]

    FakeClient.script = staticmethod(
        triage_returning(
            {**_data(size="large", external_actions=False, needs_plan=False).model_dump()},
            rest,
        )
    )
    sim.script.plan = __import__("collections").deque(["approve"])
    [task_id] = await sim.send("Do a big analysis")
    task = await engine.tasks.require(task_id)
    assert task.state == TaskState.COMPLETED and task.reviews == 2
    delivery = next(t for t in sim.channel.texts() if f"Task {task_id} done" in t)
    assert "The chart is empty" in delivery
    events = await engine.tasks.events(task_id, ["start_review"])
    assert events[0]["data"]["model"] == "claude-opus-5-5"  # large task: final reviewer with Opus


async def test_reviewer_delivers_its_verdict_with_the_structured_tool(environment):
    """Claude Code returns structured output by calling StructuredOutput; the policy must not
    ask for approval for that (before, the review always ended without a verdict)."""
    engine, sim = environment
    verdict = {"approved": True, "failures": [], "checks": ["opened the report"]}

    def rest(prompt, options, registry):
        if options.output_format and "approved" in options.output_format["schema"]["properties"]:
            return [
                Tool("StructuredOutput", dict(verdict)),
                End("", structured=dict(verdict), model=options.model),
            ]
        return [End("<delivery>Done.</delivery>")]

    FakeClient.script = staticmethod(triage_returning({**_data(size="medium").model_dump()}, rest))
    [task_id] = await sim.send("Write a report about Y")
    results = dict(FakeClient.registry.tools_results)
    assert not str(results.get("StructuredOutput", "")).startswith("DENEGADA")
    assert await engine.tasks.events(task_id, ["review"])
    assert not await engine.tasks.events(task_id, ["review_no_completed"])


async def test_the_own_tools_server_has_the_name_the_policy_expects(environment):
    """The policy recognizes the agent's own tools by `mcp__aide__`: with any other server name
    they would be treated as external tools and would all ask for approval."""
    engine, _sim = environment
    ses = SessionContext(core=engine.core, task_id=None, folder=None, role="orchestrator")
    assert "aide" in session_module._mcp_servers(ses)


def test_the_instructions_delegate_to_subagents_that_exist(config_tmp):
    text = (Path(__file__).resolve().parents[1] / "agent" / "CLAUDE.md").read_text(encoding="utf-8")
    named = {
        n for line in re.findall(r"^- (`.+?):", text, re.M) for n in re.findall(r"`(\w+)`", line)
    }
    assert named == set(DEFINITIONS)
