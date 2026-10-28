"""Delivery formats: the final file is generated and verified with code, no delivered .md
files.

The Python in the agent's sessions generates and reopens the four formats."""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest

from aide.core import documents as d
from aide.core.base_session import sdk_environment
from aide.core.context import SessionContext
from aide.core.delivery import reserve_drafts
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.core.review import prompt_review, review_role, sends_to_third_parties
from aide.core.states import TaskState
from aide.simulate import Script, Simulator
from tests.fake_sdk import End, FakeClient, Recorder, Tool, write

# --- from test_formats ----------------------------------------------------

with_libreoffice = pytest.mark.skipif(d.soffice() is None, reason="LibreOffice is not installed")


DRAFT = """# Internship report

Summary with **bold text** and a [link](https://example.com).

## Offers

| Company | Role | Link |
|---|---|---|
| Northwind Labs | Data Scientist Intern | https://careers.northwind.example |
| Bluepeak AI | LLM Intern | https://bluepeak.example |

## Conclusions

- Point one
- Point two
  - Sub-point
1. First
2. Second
"""


@pytest.fixture
async def environment(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    engine = Engine(config_tmp)
    await engine.start()
    sim = Simulator(engine, Script(), print_out=False)
    yield engine, sim
    await engine.stop()


def test_markdown_separates_lists_and_tables():
    blocks = d.read_markdown(DRAFT)
    kinds = [b.kind for b in blocks]
    assert kinds == ["title", "paragraph", "title", "table", "title", "items", "items"]
    assert blocks[5].elements == [(0, "Point one"), (0, "Point two"), (1, "Sub-point")]
    assert blocks[6].sorted_items and blocks[6].elements == [(0, "First"), (0, "Second")]
    assert d.spans("hello **world** and *more*") == [
        ("hello ", False, False),
        ("world", True, False),
        (" and ", False, False),
        ("more", False, True),
    ]


def test_speaker_notes_in_english_and_spanish():
    blocks = d.read_markdown("# Slide\n\nNotes: say hello\nnotas: legacy note\n")
    assert [(b.kind, b.text) for b in blocks] == [
        ("title", "Slide"),
        ("notes", "say hello"),
        ("notes", "legacy note"),
    ]


def test_excel_numbers():
    assert d._number("4500") == 4500 and d._number("2.25") == 2.25
    assert d._number("1,234") == 1234 and d._number("1,234.5") == 1234.5
    assert d._number("1,5") == 1.5  # decimal comma from old drafts
    assert d._number("Q3") == "Q3"


@pytest.mark.parametrize(
    ("format_", "unit", "how_many"),
    [
        ("docx", "pages", None),
        ("xlsx", "sheets", 1),
        ("pptx", "slides", 3),
        ("pdf", "pages", 1),
    ],
)
def test_generates_and_verifies_each_format(tmp_path, format_, unit, how_many):
    output = tmp_path / f"report.{format_}"
    d.generate(format_, DRAFT, output)
    v = d.verify(output)
    assert v.ok, v.problems
    assert v.unit == unit
    if how_many is not None:
        assert v.count == how_many


def test_excel_from_json_with_several_sheets(tmp_path):
    from openpyxl import load_workbook

    data = {
        "sheets": [
            {"name": "Offers", "columns": ["Company", "Salary"], "rows": [["ACME", "4500"]]},
            {"name": "Sources", "columns": ["URL"], "rows": [["https://acme.example"]]},
        ]
    }
    output = tmp_path / "table.xlsx"
    d.generate("xlsx", json.dumps(data), output)
    book = load_workbook(output)
    assert book.sheetnames == ["Offers", "Sources"]
    assert book["Offers"]["B2"].value == 4500  # numbers stay as numbers
    assert book["Offers"].freeze_panes == "A2"
    assert book["Sources"]["A2"].hyperlink is not None
    assert d.verify(output, expected=2).ok
    v = d.verify(output, expected=3)
    assert not v.ok and "3 were expected" in v.problems[0]


def test_excel_headers_fit(tmp_path):
    from openpyxl import load_workbook

    data = {
        "sheets": [
            {
                "name": "Margin",
                "columns": ["Platform fees (USD)", "Expected profit (USD)"],
                "rows": [[1.5, 2.25]],
            }
        ]
    }
    output = tmp_path / "headers.xlsx"
    d.generate("xlsx", json.dumps(data), output)
    ws = load_workbook(output)["Margin"]
    # Bold and filter button: the column has more than the title length.
    for letter, title in (
        ("A", "Platform fees (USD)"),
        ("B", "Expected profit (USD)"),
    ):
        assert ws.column_dimensions[letter].width >= len(title) + 5


def test_slides_use_the_full_width(tmp_path):
    from pptx import Presentation

    output = tmp_path / "presentation.pptx"
    d.generate("pptx", DRAFT, output)
    prs = Presentation(str(output))
    assert round(prs.slide_width / prs.slide_height, 2) == 1.78  # 16:9
    for slide in prs.slides:
        for shape in slide.placeholders:
            assert shape.height > 0 and shape.top >= 0
            # Centered: the right margin equals the left (the 4:3 template left a third
            # of the right empty).
            right = prs.slide_width - shape.left - shape.width
            assert shape.left > 0 and abs(right - shape.left) < 0.02 * prs.slide_width
    title = prs.slides[1].shapes.title
    assert title.width > 0.85 * prs.slide_width


def test_excel_prints_at_the_width_of_one_page(tmp_path):
    from openpyxl import load_workbook

    columns = [f"Column {n}" for n in range(8)]
    data = {"sheets": [{"name": "Wide", "columns": columns, "rows": [["x" * 20] * 8]}]}
    output = tmp_path / "wide.xlsx"
    d.generate("xlsx", json.dumps(data), output)
    ws = load_workbook(output)["Wide"]
    assert ws.sheet_properties.pageSetUpPr.fitToPage
    assert (ws.page_setup.fitToWidth, ws.page_setup.fitToHeight) == (1, 0)
    assert ws.page_setup.orientation == "landscape"


@with_libreoffice
def test_wide_excel_comes_out_on_a_single_page(tmp_path):
    columns = ["City", "District", "Population", "Source", "Notes"]
    rows = [["Riverton", "North", 440000, "Estimate without a search (2023)", "x" * 40]] * 5
    output = tmp_path / "cities.xlsx"
    d.generate("xlsx", json.dumps({"sheets": [{"columns": columns, "rows": rows}]}), output)
    v = d.verify(output, views=tmp_path / "views")
    assert v.ok and len(v.views) == 1


def test_detects_empty_or_broken_files(tmp_path):
    from docx import Document

    empty = tmp_path / "empty.docx"
    Document().save(str(empty))
    assert not d.verify(empty).ok
    broken = tmp_path / "broken.pptx"
    broken.write_bytes(b"this is not a pptx")
    v = d.verify(broken)
    assert not v.ok and "does not open" in v.problems[0]
    assert not d.verify(tmp_path / "missing.pdf").ok


@with_libreoffice
def test_images_of_the_first_pages(tmp_path):
    output = tmp_path / "presentation.pptx"
    d.generate("pptx", DRAFT, output)
    v = d.verify(output, expected=3, views=tmp_path / "views")
    assert v.ok and len(v.views) == 3
    assert all(p.suffix == ".png" and p.stat().st_size > 0 for p in v.views)
    assert v.views[0].name.startswith("presentation-pptx-")
    word = tmp_path / "report.docx"
    d.generate("docx", DRAFT, word)
    v = d.verify(word, views=tmp_path / "views")
    assert v.count == 1 and [p.name for p in v.views] == ["report-docx-1.png"]


async def test_generate_document_tool(environment):
    engine, _ = environment
    from aide.tools.documents import documents_tools

    task = await engine.tasks.create("Report", state=TaskState.RUNNING)
    folder = engine.config.paths.create_task_folder(task.id)
    ses = SessionContext(core=engine.core, task_id=task.id, folder=folder, role="orchestrator")
    generate, verify_doc, _chart = documents_tools(ses)
    draft = folder / "data" / "drafts" / "report.md"
    draft.parent.mkdir(parents=True)
    draft.write_text(DRAFT)

    r = await generate.handler(
        {
            "format_": "docx",
            "draft": "data/drafts/report.md",
            "name": "Offers report",
            "expected": 0,
        }
    )
    text = r["content"][0]["text"]
    assert not r.get("is_error") and "OK" in text
    final = folder / "results" / "Offers report.docx"
    assert final.is_file()
    assert any(a["path"] == str(final) for a in await engine.tasks.files(task.id, "result"))
    assert await engine.tasks.events(task.id, ["document"])
    # The draft cannot be outside the task or in results/.
    outside = engine.config.paths.root / "outside.md"
    outside.write_text("x")
    assert (await generate.handler({"format_": "pdf", "draft": str(outside), "name": "a"}))[
        "is_error"
    ]
    (folder / "results" / "b.md").write_text("x")
    r = await generate.handler({"format_": "pdf", "draft": "results/b.md", "name": "b"})
    assert r["is_error"] and "data/" in r["content"][0]["text"]
    assert (
        await generate.handler({"format_": "exe", "draft": "data/drafts/report.md", "name": "x"})
    )["is_error"]
    # Check an already finished file.
    r = await verify_doc.handler({"path": str(final), "expected": 0})
    assert "OK" in r["content"][0]["text"]
    assert (await verify_doc.handler({"path": "/etc/hosts", "expected": 0}))["is_error"]


def test_md_drafts_are_not_delivered(tmp_path):
    folder = tmp_path / "task"
    (folder / "results" / "sub").mkdir(parents=True)
    (folder / "results" / "report.md").write_text("draft")
    (folder / "results" / "sub" / "notes.md").write_text("notes")
    (folder / "results" / "report.docx").write_text("final")
    moved = reserve_drafts(folder, "Make me a report in Word")
    assert sorted(p.name for p in moved) == ["notes.md", "report.md"]
    assert not list((folder / "results").rglob("*.md"))
    assert (folder / "data" / "drafts" / "report.md").read_text() == "draft"
    assert (folder / "results" / "report.docx").exists()
    # If the user asks for Markdown, it is delivered.
    (folder / "results" / "note.md").write_text("x")
    assert reserve_drafts(folder, "Send it to me in markdown") == []
    assert reserve_drafts(folder, "save it as summary.md") == []
    assert (folder / "results" / "note.md").exists()


async def test_delivery_has_no_md_and_verifies_the_documents(environment):
    engine, sim = environment

    def script(prompt, options, registry):
        folder = Path(re.search(r"Task folder: (\S.*?)\n", prompt).group(1).strip())
        md = folder / "results" / "report.md"
        broken = folder / "results" / "table.xlsx"
        return [
            Tool("Write", {"file_path": str(md), "content": "# Hello"}, write(md)),
            Tool(
                "Write", {"file_path": str(broken), "content": "not an excel file"}, write(broken)
            ),
            End("<delivery>Here is the report.</delivery>"),
        ]

    FakeClient.script = script
    [task_id] = await sim.send("Make me a report and a table")
    delivery = next(t for t in sim.channel.texts() if f"Task {task_id} done" in t)
    sent = [m.path.name for m in sim.channel.messages if m.kind == "file"]
    assert "report.md" not in sent and "report.md" not in delivery
    assert "table.xlsx: has problems" in delivery  # the broken Excel fails the check
    assert await engine.tasks.events(task_id, ["draft_reserved"])
    final = Path((await engine.tasks.require(task_id)).final_folder)
    assert not list(final.rglob("*.md"))


async def test_long_delivery_goes_in_word(environment, monkeypatch):
    engine, sim = environment
    long = "Long paragraph. " * 400
    FakeClient.script = lambda p, o, r: [End(f"<delivery>{long}</delivery>")]
    [task_id] = await sim.send("Make me something long")
    sent = [m.path.name for m in sim.channel.messages if m.kind == "file"]
    assert "delivery.docx" in sent and "delivery.md" not in sent
    assert any("as a Word file" in t for t in sim.channel.texts())
    assert d.verify(engine.config.paths.task(task_id) / "results" / "delivery.docx").ok


async def test_review_uses_opus_only_for_large_or_third_party_sends(environment):
    engine, _ = environment
    core = engine.core
    medium = await engine.tasks.create("Research this", state=TaskState.RUNNING)
    await engine.tasks.update(medium.id, size="medium", external_actions=1)
    medium = await engine.tasks.require(medium.id)
    # Medium with an external action that sends nothing to third parties (for example,
    # scheduling).
    assert not await sends_to_third_parties(core, medium)
    assert review_role(core, medium, False, False).alias == "sonnet"
    # Medium that sent an approved email: Opus.
    await engine.db.run(
        "INSERT INTO approvals(task_id, kind, tool, entry, detail, level, state, created) "
        "VALUES (?, 'email', 'mcp__aide__gmail_send_draft', '{}', 'x', 2, 'approved', '2026-10-20')",
        (medium.id,),
    )
    assert await sends_to_third_parties(core, medium)
    assert review_role(core, medium, False, True).alias == "opus"
    large = await engine.tasks.create("Big one", state=TaskState.RUNNING)
    await engine.tasks.update(large.id, size="large")
    large = await engine.tasks.require(large.id)
    assert review_role(core, large, False, False).alias == "opus"
    # With the Opus limit or temporary Sonnet, Sonnet.
    assert review_role(core, large, True, False).alias == "sonnet"


def test_the_reviewer_receives_the_images():
    class T:
        request = "Make a presentation"
        plan = None

    text = prompt_review(T(), [Path("/x/results/p.pptx")], [Path("/x/data/vistas/p-pptx-1.png")])
    assert "/x/data/vistas/p-pptx-1.png" in text and "Do not approve" in text
    assert "Images" not in prompt_review(T(), [Path("/x/results/a.txt")], [])


# --- from test_documents --------------------------------------------------

GENERATE = r"""
import sys
from pathlib import Path
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from reportlab.lib.pagesizes import A4
from reportlab.pdfgen import canvas

d = Path(sys.argv[1])
doc = Document(); doc.add_heading("Report", 1); doc.add_paragraph("Text with accents: café, naïve, señor."); doc.save(d / "a.docx")
wb = Workbook(); ws = wb.active; ws.append(["City", "Rent"]); ws.append(["Zoë Town", 2450]); wb.save(d / "a.xlsx")
prs = Presentation(); s = prs.slides.add_slide(prs.slide_layouts[1]); s.shapes.title.text = "Summary"; prs.save(d / "a.pptx")
c = canvas.Canvas(str(d / "a.pdf"), pagesize=A4); c.drawString(72, 770, "Test report"); c.showPage(); c.save()
"""


OPEN_ = r"""
import sys
from pathlib import Path
from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pypdf import PdfReader

d = Path(sys.argv[1])
assert "café" in Document(d / "a.docx").paragraphs[1].text
assert load_workbook(d / "a.xlsx").active["A2"].value == "Zoë Town"
assert Presentation(d / "a.pptx").slides[0].shapes.title.text == "Summary"
assert "Test report" in PdfReader(d / "a.pdf").pages[0].extract_text()
print("ok")
"""


def test_the_four_formats_are_generated_and_open(config_tmp, tmp_path):
    env = sdk_environment(config_tmp)
    assert env["PATH"].split(":")[0] == str(Path(sys.executable).parent)
    python = Path(env["PATH"].split(":")[0]) / "python"
    for script in (GENERATE, OPEN_):
        r = subprocess.run(
            [str(python), "-c", script, str(tmp_path)], capture_output=True, text=True, env=env
        )
        assert r.returncode == 0, r.stderr
    assert r.stdout.strip() == "ok"
    for extension in ("docx", "xlsx", "pptx", "pdf"):
        assert (tmp_path / f"a.{extension}").stat().st_size > 0
