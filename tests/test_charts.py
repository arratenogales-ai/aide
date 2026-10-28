"""PNG charts: the generate_chart tool and the usage ones, which the system makes
without a model for /usage and the Sunday report."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from aide.bot.channel import Entry
from aide.bot.commands import run_command
from aide.core import charts
from aide.core.context import SessionContext
from aide.core.engine import Engine
from aide.core.states import TaskState
from aide.simulate import Script, Simulator


@pytest.fixture
async def engine(config_tmp):
    m = Engine(config_tmp)
    await m.start(workers=False)
    yield m
    await m.stop()


def test_numbers_in_english():
    assert charts.is_number(1234567.8) == "1,234,568"
    assert charts.is_number(1234.5, 1) == "1,234.5"
    assert charts._abbreviated(1_500_000) == "1.5M" and charts._abbreviated(2_000_000) == "2M"
    assert charts._abbreviated(12_000) == "12K" and charts._abbreviated(950) == "950"
    assert charts._abbreviated(2.5) == "2.5" and charts._abbreviated(0.25) == "0.25"
    assert charts.WEEK_DAYS[0] == "Mon"


def test_bar_and_line_chart(tmp_path):
    for kind in charts.KINDS:
        output = charts.generate(
            tmp_path / f"{kind}.png",
            kind,
            "Average price by design",
            ["Cat", "Dog", "Halo"],
            [
                {"name": "USD", "values": [24.5, 27, 19.99]},
                {"name": "Other", "values": [1, 2, 3]},
            ],
            axis_and="dollars",
        )
        assert output.read_bytes().startswith(b"\x89PNG") and output.stat().st_size > 5000
    with pytest.raises(charts.ChartError, match="one value per label"):
        charts.generate(tmp_path / "x.png", "bars", "x", ["a", "b"], [{"values": [1]}])
    with pytest.raises(charts.ChartError, match="not numbers"):
        charts.generate(tmp_path / "x.png", "bars", "x", ["a"], [{"values": ["lots"]}])
    with pytest.raises(charts.ChartError, match="invalid kind"):
        charts.generate(tmp_path / "x.png", "tarta", "x", ["a"], [{"values": [1]}])


async def test_generate_chart_tool(engine):
    from aide.tools.documents import documents_tools

    task = await engine.tasks.create("Trends", state=TaskState.RUNNING)
    ses = SessionContext(
        core=engine.core,
        task_id=task.id,
        folder=engine.config.paths.create_task_folder(task.id),
        role="orchestrator",
    )
    tool = next(h for h in documents_tools(ses) if h.name == "generate_chart")
    r = await tool.handler(
        {
            "kind": "bars",
            "title": "Sales by product",
            "labels": ["Mug", "Ornament"],
            "series": [{"name": "Orders", "values": [12, 7]}],
            "name": "../../sales",
        }
    )
    assert not r.get("is_error")
    output = ses.folder / "results" / "sales.png"  # the name does not come from the task
    assert output.is_file() and str(output) in r["content"][0]["text"]
    bad = await tool.handler({"kind": "bars", "title": "x", "labels": ["a"], "series": []})
    assert bad.get("is_error") and "series" in bad["content"][0]["text"]


async def test_usage_per_day_and_its_chart(engine):
    now = datetime.now(UTC)
    for days, model, tokens in (
        (0, "claude-opus-5-5", 1_000_000),
        (0, "claude-haiku-5-5", 5000),
        (2, "claude-sonnet-5-5", 300_000),
        (20, "claude-opus-5-5", 9),
    ):
        await engine.db.run(
            "INSERT INTO usage(task_id, hour, origin, model, input_tokens) VALUES (NULL, ?, 'test', ?, ?)",
            ((now - timedelta(days=days)).isoformat(timespec="seconds"), model, tokens),
        )
    by_day = await engine.usage.consumption_by_day(7, engine.config.time_zone)
    assert len(by_day) == 7
    assert by_day[-1][1] == {"Opus": 1_000_000, "Haiku": 5000}
    assert by_day[-3][1] == {"Sonnet": 300_000} and by_day[0][1] == {}
    path = await charts.consumption_chart(engine.core, 7)
    assert (
        path is not None and path.is_file() and path.parent == engine.config.paths.data / "charts"
    )
    # /usage sends the text and then the chart as an image.
    sim = Simulator(engine, Script(), print_out=False)
    await run_command(engine, Entry("command", 0, text="/usage", command="usage"))
    assert sim.channel.messages[-1].kind == "image" and "14 days" in sim.channel.messages[-1].text


async def test_no_usage_means_no_chart(engine):
    assert await charts.consumption_chart(engine.core, 7) is None
