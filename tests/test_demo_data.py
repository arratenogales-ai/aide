"""scripts/demo_data.py: the sample data, with the name asked for."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("demo_data", ROOT / "scripts/demo_data.py")
assert _spec is not None and _spec.loader is not None
demo = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(demo)


class Terminal:
    """Standard input, as a terminal or not."""

    def __init__(self, terminal: bool) -> None:
        self.terminal = terminal

    def isatty(self) -> bool:
        return self.terminal


def nobody_asks(*_: object) -> str:
    raise AssertionError("it should not ask")


def test_the_option_is_used_without_asking(monkeypatch):
    monkeypatch.setattr(demo.sys, "stdin", Terminal(True))
    monkeypatch.setattr("builtins.input", nobody_asks)
    assert demo.chosen_name("  Sam   Lee ") == "Sam Lee"
    assert demo.chosen_name("") == "Alex"


@pytest.mark.parametrize(("answer", "name"), [("Sam", "Sam"), ("", "Alex"), ("   ", "Alex")])
def test_in_a_terminal_it_asks(monkeypatch, answer, name):
    monkeypatch.setattr(demo.sys, "stdin", Terminal(True))
    monkeypatch.setattr("builtins.input", lambda _: answer)
    assert demo.chosen_name(None) == name


def test_without_an_answer_or_a_terminal_it_is_alex(monkeypatch):
    monkeypatch.setattr(demo.sys, "stdin", Terminal(False))
    monkeypatch.setattr("builtins.input", nobody_asks)
    assert demo.chosen_name(None) == "Alex"

    def closed(_: str) -> str:
        raise EOFError

    monkeypatch.setattr(demo.sys, "stdin", Terminal(True))
    monkeypatch.setattr("builtins.input", closed)
    assert demo.chosen_name(None) == "Alex"


async def test_the_name_reaches_all_the_sample_data(tmp_path, monkeypatch):
    for variable in ("AIDE_ROOT", "AIDE_DATA", "AIDE_FOLDERS"):
        monkeypatch.setenv(variable, str(tmp_path / "unused"))
    engine = await demo.create(tmp_path / "demo", "Sam")
    try:
        paths = engine.config.paths
        assert engine.config.user == "Sam"
        assert "- Name: Sam" in paths.profile.read_text(encoding="utf-8")
        memory = paths.memory.read_text(encoding="utf-8")
        assert "Sam is vegetarian" in memory and "Alex" not in memory
        approvals = await engine.db.everything("SELECT detail FROM approvals")
        assert any("Thanks, Sam" in a["detail"] for a in approvals)
        assert not any("Alex" in a["detail"] or "{name}" in a["detail"] for a in approvals)
    finally:
        await engine.stop()
