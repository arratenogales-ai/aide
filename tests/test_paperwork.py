"""Your documents: they are saved in data/documents when sent with “documento” in the
caption, the model reads them once, the code checks the dates and calculates the notice
period, the notices are done by code and the agent only searches what is saved. No network
or model.

Documents: captions with punctuation, several files with a single
caption, files without a caption with buttons, the retry if the model does not respond,
Word and the documents inbox. No network or model.

Documents: scans with huge pages are read, the real reason for a failed read (the
image, the plan limit or the model) and duplicates, which are neither read nor proposed
again. No network or model."""

from __future__ import annotations

import asyncio
import fnmatch
import re
import shutil
import unicodedata
import zipfile
import zlib
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from aide.bot.channel import Entry, FakeChannel
from aide.bot.commands import run_command
from aide.core import call
from aide.core.context import SessionContext
from aide.core.engine import Engine
from aide.core.policy import FORBIDDEN, CallContext, Policy
from aide.paperwork import duplicates, flow, inbox, reading, registry, reminders

# --- from test_paperwork_2115 ------------------------------------------------

READ = {
    "is_document": True,
    "kind": "insurance",
    "title": "Segurix home insurance",
    "entity": "Segurix",
    "number": "12.345.678",
    "summary": "Your home insurance: it covers your belongings in the apartment.",
    "dates": [
        {
            "kind": "expiry",
            "date": "2027-12-31",
            "description": "the insurance expires",
            "phrase": "Contract term until 31.12.2027",
            "months_advance_notice": 3,
            "days_advance_notice": 0,
        },
        {
            "kind": "payment",
            "date": "not a date",
            "description": "a payment is due",
            "phrase": "",
            "months_advance_notice": 0,
            "days_advance_notice": 0,
        },
    ],
    "text": "",
    "doubts": "",
}


def pdf_insurance_policy(path: Path, number: str = "12.345.678") -> None:
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    for i, line in enumerate(
        [
            "Segurix Insurance",
            f"Policy No. {number} - Home contents insurance",
            "Contract term until 31.12.2027",
            "Notice period: 3 months before expiry",
            "Annual premium USD 312.00",
        ]
    ):
        c.drawString(72, 760 - 18 * i, line)
    c.save()


@pytest.fixture
async def engine(config_tmp):
    m = Engine(config_tmp, channel=FakeChannel(print_out=False))
    await m.start(workers=False)
    m.commands = run_command
    yield m
    await m.stop()


def model(monkeypatch, output: dict[str, Any] | None) -> list[dict[str, Any]]:
    from aide.core import call

    seen: list[dict[str, Any]] = []

    async def structured(core, **kw):
        seen.append(kw)
        return output

    monkeypatch.setattr(call, "structured", structured)
    return seen


def download(m: Engine, name: str) -> Path:
    """As the bot leaves it: in the agent's Telegram cache, in the message folder."""
    folder = m.config.paths.agent_cache / "telegram" / "77"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / name


async def proposed(m: Engine, monkeypatch) -> int:
    pdf = download(m, "Policy.pdf")
    pdf_insurance_policy(pdf)
    model(monkeypatch, READ)
    assert await flow.since_telegram(m, "document apartment insurance", [pdf])
    return await m.db.value("SELECT MAX(id) FROM documents")


def test_the_caption():
    assert flow.caption("document") == "" and flow.caption("document") == ""
    assert flow.caption("Document: car insurance policy") == "car insurance policy"
    assert flow.caption("Documento: póliza del coche") == "póliza del coche"
    assert flow.caption("documents") == "" and flow.caption("documents") == ""
    assert flow.caption("software expense") is None and flow.caption("my document") is None


def test_the_notice_period_is_calculated_by_the_code():
    assert flow.subtract_months(date(2027, 12, 31), 3) == date(2027, 9, 30)
    assert flow.subtract_months(date(2027, 3, 31), 1) == date(2027, 2, 28)
    assert flow.subtract_months(date(2028, 3, 31), 1) == date(2028, 2, 29)
    assert flow.subtract_months(date(2027, 1, 15), 2) == date(2026, 11, 15)
    p = flow.advance_notice_date(date(2027, 12, 31), 3, 0)
    assert p["date"] == "2027-09-30" and ", 3 months before Dec 31, 2027" in p["description"]
    assert p["rule"] == {"months": 3, "days": 0, "base": "2027-12-31"}
    assert flow.advance_notice_date(date(2027, 12, 31), 0, 14)["date"] == "2027-12-17"


def test_the_dates_that_are_valid():
    dates = flow.valid_dates(READ["dates"], "Contract term until 31.12.2027 ...")
    assert [f["kind"] for f in dates] == ["expiry", "advance_notice"]
    # The notice period comes from the same sentence: if the sentence is there, it is valid for
    # it too.
    assert dates[0]["checked"] and dates[1]["checked"]
    no_text = flow.valid_dates(READ["dates"], "")
    assert not no_text[0]["checked"]
    rare = [{**READ["dates"][0], "months_advance_notice": 99}]
    assert len(flow.valid_dates(rare, "")) == 1  # a 99-month notice period is not believed


async def test_a_pdf_comes_from_the_cache_and_is_proposed(engine, monkeypatch):
    pdf = download(engine, "Policy.pdf")
    pdf_insurance_policy(pdf)
    seen = model(monkeypatch, READ)
    assert await flow.since_telegram(engine, "document apartment insurance", [pdf])
    assert not pdf.exists() and not pdf.parent.exists()  # is no longer in the cache
    [d] = await engine.db.everything("SELECT * FROM documents")
    saved = engine.config.paths.documents / d["file"]
    assert saved.is_file() and oct(saved.stat().st_mode & 0o777) == "0o600"
    assert (
        d["state"] == "proposed"
        and d["origin_text"] == "pdf"
        and d["note"] == "apartment insurance"
    )
    [call] = seen
    assert "Contract term until 31.12.2027" in call["prompt"] and not call.get("images")
    assert call["origin"] == "documents" and call["role"] == "documents"
    proposal = engine.channel.messages[-1]
    text = proposal.plain_text
    assert text.startswith("Segurix home insurance\nInsurance from Segurix, no. 12.345.678.")
    assert "On Dec 31, 2027 the insurance expires" in text
    assert "On Sep 30, 2027 it's the last day to give notice if you want to cancel it" in text
    assert "I can't find that sentence" not in text  # the sentence is in the PDF
    assert proposal.buttons_data == [
        f"doc:sav:{d['id']}",
        f"doc:cor:{d['id']}",
        f"doc:no:{d['id']}",
    ]


async def test_saving_it_leaves_it_searchable_and_sendable(engine, monkeypatch):
    document_id = await proposed(engine, monkeypatch)
    await engine.button(f"doc:sav:{document_id}")
    d = await registry.get(engine.db, document_id)
    assert d["state"] == "saved" and "Saved. I'll remind you of each date 7 days before" in (
        engine.channel.messages[-1].plain_text
    )
    [found] = await registry.search(engine.db, "When does my home insurance expire?")
    assert found["id"] == document_id and len(found["dates"]) == 2
    assert "«insurance»" in found["chunk"].lower()  # the matching piece
    [text] = await registry.search(engine.db, "contents")
    assert "«contents»" in text["chunk"]
    assert not await registry.search(engine.db, "x")
    # The agent's tools: search (with the source) and send the original.
    from aide.tools.server import tools_of

    ses = SessionContext(core=engine.core, task_id=None, folder=None, role="orchestrator")
    tools = {h.name: h for h in tools_of(ses)}
    output = await tools["documents_search"].handler({"query": "insurance"})
    popover = output["content"][0]["text"]
    assert f'Document {document_id}: "Segurix home insurance" (insurance)' in popover
    assert "Date 2027-09-30" in popover and 'sentence in the document: "Contract term' in popover
    output = await tools["documents_send"].handler({"document": document_id})
    assert "Done" in output["content"][0]["text"]
    file = engine.channel.messages[-1]
    assert file.kind == "file" and file.path.name.endswith("Policy.pdf")
    bad = await tools["documents_send"].handler({"document": 999})
    assert bad.get("is_error")
    # Pressing again does nothing.
    await engine.button(f"doc:no:{document_id}")
    assert engine.channel.texts()[-1] == "That document is no longer pending."


async def test_discarding_deletes_the_copy(engine, monkeypatch):
    document_id = await proposed(engine, monkeypatch)
    d = await registry.get(engine.db, document_id)
    file = engine.config.paths.documents / d["file"]
    await engine.button(f"doc:no:{document_id}")
    assert (await registry.get(engine.db, document_id))["state"] == "discarded"
    assert not file.exists() and await registry.search(engine.db, "insurance") == []


async def test_correcting_is_applied_by_the_code(engine, monkeypatch):
    document_id = await proposed(engine, monkeypatch)
    await engine.button(f"doc:cor:{document_id}")
    assert engine.text_wait.kind == "document_correction"
    assert '"expires 2027-12-31"' in engine.channel.texts()[-1]
    await flow.correct(
        engine,
        document_id,
        "Title: Apartment insurance\nno. 99-1\nexpires 2028-03-31\nnotice 2 months",
    )
    d = await registry.get(engine.db, document_id)
    assert d["title"] == "Apartment insurance" and d["number"] == "99-1"
    dates = await registry.dates_of(engine.db, document_id)
    assert [(f["kind"], f["date"]) for f in dates] == [
        ("advance_notice", "2028-01-31"),
        ("expiry", "2028-03-31"),
    ]
    assert "Apartment insurance" in engine.channel.messages[-1].plain_text  # it is shown again
    await flow.correct(engine, document_id, "remove date 1\nnothing to do with it")
    dates = await registry.dates_of(engine.db, document_id)
    assert [f["kind"] for f in dates] == ["expiry"]
    assert any(
        'I didn\'t understand this: "nothing to do with it"' in t for t in engine.channel.texts()
    )
    assert engine.text_wait.kind == "document_correction"  # it can write it again


async def test_a_photo_is_read_by_the_model_and_its_text_is_searchable(engine, monkeypatch):
    photo = download(engine, "foto-1.jpg")
    photo.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)
    read = {**READ, "text": "Home contents insurance Contract term until 31.12.2027"}
    seen = model(monkeypatch, read)
    assert await flow.since_telegram(engine, "document", [photo])
    [call] = seen
    assert call["images"] and "The document is in the images." in call["prompt"]
    [d] = await engine.db.everything("SELECT * FROM documents")
    assert d["origin_text"] == "image" and d["text"].startswith("Home contents")
    assert "I can't find that sentence" not in engine.channel.messages[-1].plain_text


async def test_what_cannot_be_read_is_not_saved(engine, monkeypatch):
    rare = download(engine, "contract.pages")
    rare.write_bytes(b"PK")
    model(monkeypatch, READ)
    assert await flow.since_telegram(engine, "document", [rare])
    assert "I can't read .pages files" in engine.channel.texts()[-1]
    assert await engine.db.everything("SELECT * FROM documents") == []
    assert not any(engine.config.paths.documents.rglob("*.pages"))
    pdf = download(engine, "Policy.pdf")
    pdf_insurance_policy(pdf)
    model(monkeypatch, None)  # the model does not respond
    assert await flow.since_telegram(engine, "document", [pdf])
    assert "the model didn't respond" in engine.channel.texts()[-1]
    assert not any(engine.config.paths.documents.rglob("*.pdf"))


ZONE = ZoneInfo("Europe/Berlin")


def at_time(day: date, hour: int = 10) -> datetime:
    return datetime.combine(day, time(hour), ZONE)


async def saved_with_dates(m: Engine, dates: list[tuple[int, str]], today: date) -> int:
    document_id = await registry.create(
        m.db,
        {"title": "Residence permit", "kind": "permission", "file": "2026/x.pdf"},
        [
            {
                "kind": "expiry",
                "date": (today + timedelta(days=n)).isoformat(),
                "description": d,
            }
            for n, d in dates
        ],
    )
    await registry.save(m.db, document_id)
    return document_id


async def test_notices_60_30_7_and_the_same_day(engine):
    today = date(2026, 11, 2)
    await saved_with_dates(
        engine,
        [
            (45, "the permit expires"),
            (20, "it must be renewed"),
            (3, "the installment is due"),
            (0, "the card expires"),
            (90, "far away"),
            (-1, "in the past"),
        ],
        today,
    )
    assert await reminders.review(engine, at_time(today)) == 4
    text = engine.channel.texts()[-1]
    assert text.startswith("A reminder of these dates in your documents.")
    assert "On Dec 17, 2026 (in 45 days) the permit expires (Residence permit)." in text
    assert "Today the card expires (Residence permit)." in text and "far away" not in text
    assert await reminders.review(engine, at_time(today)) == 0  # once a day
    # The next day what was already notified is not repeated...
    assert await reminders.review(engine, at_time(today + timedelta(days=1))) == 0
    # ...and 7 days before the permit the 7-day notice is due (and 60 days before the distant
    # date, the 60-day one).
    assert await reminders.review(engine, at_time(today + timedelta(days=38))) == 2
    assert "(in 7 days) the permit expires" in engine.channel.texts()[-1]
    assert "(in 52 days) far away" in engine.channel.texts()[-1]


async def test_without_connection_the_notice_waits(engine, monkeypatch):
    today = date(2026, 11, 2)
    await saved_with_dates(engine, [(7, "the permit expires")], today)
    original = engine.notify

    async def no_network(*args: Any, **kwargs: Any) -> None:
        return None

    monkeypatch.setattr(engine, "notify", no_network)
    assert await reminders.review(engine, at_time(today)) == 0
    monkeypatch.setattr(engine, "notify", original)
    assert await reminders.review(engine, at_time(today)) == 1


def test_the_stage_of_each_notice():
    assert [reminders.stage(d) for d in (61, 60, 31, 30, 8, 7, 1, 0, -1)] == [
        None,
        60,
        60,
        30,
        30,
        7,
        7,
        0,
        None,
    ]


def test_the_agent_cannot_read_the_documents_folder(config_tmp):
    pol = Policy(config_tmp)
    path = config_tmp.paths.documents / "2026" / "Policy.pdf"
    assert pol.classify_read(path).level == FORBIDDEN
    assert "!**/[dD][aA][tT][aA]/**" in pol.search_exclusions(str(config_tmp.paths.root))


def test_the_weekly_backup_includes_the_documents(config_tmp):
    from aide.maintenance.backups import create_backup

    paths = config_tmp.paths
    paths.db.parent.mkdir(parents=True, exist_ok=True)
    import sqlite3

    sqlite3.connect(paths.db).close()
    doc = paths.documents / "2026" / "20261025-Policy.pdf"
    doc.parent.mkdir(parents=True, exist_ok=True)
    doc.write_bytes(b"%PDF-1.4")
    destination, _size, _deleted = create_backup(paths, datetime.now(UTC), 4)
    with zipfile.ZipFile(destination) as z:
        assert "data/documents/2026/20261025-Policy.pdf" in z.namelist()


def test_the_role_of_the_reading(config_tmp):
    class N:
        config = config_tmp

    assert reading.role_of(N()) == "documents"


def test_search_goes_in_the_quick_replies_and_its_text_is_external(config_tmp):
    from aide.core.hooks import EXTERNAL_SOURCES

    assert EXTERNAL_SOURCES["mcp__aide__documents_search"] == "your saved documents"
    assert "mcp__aide__documents_send" not in EXTERNAL_SOURCES


async def test_before_nine_no_notice_is_sent(engine):
    today = date(2026, 11, 2)
    await saved_with_dates(engine, [(7, "the permit expires")], today)
    assert await reminders.review(engine, at_time(today, 7)) == 0
    assert await engine.db.kv_read(reminders.DAY_KEY) is None  # does not count as done
    assert await reminders.review(engine, at_time(today, 9)) == 1


async def test_when_saving_the_notice_of_that_day_is_already_given(engine, monkeypatch):
    """If a document expiring in 20 days is saved, the next notice is the 7-day one: the 30-day
    one was already seen when saving it."""
    document_id = await proposed(engine, monkeypatch)
    today = flow.today_of(engine)
    expires = today + timedelta(days=20)
    await registry.put_dates(
        engine.db,
        document_id,
        [
            {
                "kind": "expiry",
                "date": expires.isoformat(),
                "description": "the insurance expires",
            }
        ],
    )
    await engine.button(f"doc:sav:{document_id}")
    assert await reminders.review(engine, at_time(today + timedelta(days=1))) == 0
    assert await reminders.review(engine, at_time(today + timedelta(days=13))) == 1
    assert "(in 7 days) the insurance expires" in engine.channel.texts()[-1]


async def test_the_pending_items_serve_the_morning_summary(engine):
    today = date(2026, 11, 2)
    await saved_with_dates(engine, [(7, "the permit expires"), (0, "the card expires")], today)
    lines, marks = await reminders.pending(engine.db, today)
    assert len(lines) == 2 and len(marks) == 2
    await reminders.mark(engine.db, marks)
    assert await reminders.pending(engine.db, today) == ([], [])
    assert await reminders.review(engine, at_time(today)) == 0


async def test_the_engine_sends_the_documento_caption_to_your_documents(engine, monkeypatch):
    pdf = download(engine, "Policy.pdf")
    pdf_insurance_policy(pdf)
    model(monkeypatch, READ)
    await engine.receive(
        Entry("document", 0, text="Document: apartment insurance", files=[pdf], message_id=5)
    )
    [d] = await engine.db.everything("SELECT * FROM documents")
    assert d["note"] == "apartment insurance"
    assert engine.channel.messages[-1].buttons_data[0] == f"doc:sav:{d['id']}"
    assert await engine.tasks.list_items(limit=5, kind="task") == []  # no triage or task


async def test_a_photo_without_text_and_then_documento(engine, monkeypatch):
    photo = download(engine, "foto-1.jpg")
    photo.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)
    model(monkeypatch, {**READ, "text": "Home contents insurance"})
    await engine.receive(Entry("image", 0, files=[photo], message_id=6))
    assert "I got the photo. What should I do with it?" in engine.channel.texts()[-1]
    await engine.receive(Entry("text", 0, text="document", message_id=7))
    [d] = await engine.db.everything("SELECT * FROM documents")
    assert d["origin_text"] == "image"


async def test_the_correction_arrives_in_a_message(engine, monkeypatch):
    document_id = await proposed(engine, monkeypatch)
    await engine.receive(Entry("button", 0, button_datum=f"doc:cor:{document_id}"))
    await engine.receive(Entry("text", 0, text="expires 2028-03-31", message_id=8))
    dates = await registry.dates_of(engine.db, document_id)
    assert ("expiry", "2028-03-31") in [(f["kind"], f["date"]) for f in dates]
    assert engine.text_wait is None


async def test_the_hourly_checks_look_at_the_notices(engine, monkeypatch):
    calls = []

    async def review(m, moment=None):
        calls.append(m)
        return 0

    monkeypatch.setattr(reminders, "review", review)
    await engine.check_all()
    assert calls == [engine]


async def test_the_documents_page_and_its_file(engine, monkeypatch):
    import httpx

    from aide.panel.app import Panel

    document_id = await proposed(engine, monkeypatch)
    p = Panel(engine, token="token-de-prueba")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=p.app), base_url=f"http://127.0.0.1:{p.port}"
    ) as client:
        await client.get("/enter?t=token-de-prueba&go=/")
        html = (await client.get("/documents")).text
        assert "1 document is waiting for your “Save” in Telegram." in html
        assert "You haven't saved any documents yet." in html
        await engine.button(f"doc:sav:{document_id}")
        html = (await client.get("/documents")).text
        # In its folder: a home insurance goes to Insurance, with its date in view.
        insurance = html.split('id="folder-insurance"')[1].split('id="folder-')[0]
        assert "Segurix home insurance" in insurance and "Dec 31, 2027" in insurance
        # The six folders in their order, collapsed; “Other” only if it has something. No
        # “Upcoming dates”, which are already in “Today”.
        assert re.findall(r'id="folder-(\w+)"', html) == [
            "housing",
            "work",
            "education",
            "insurance",
            "car",
            "health",
        ]
        assert ' open id="folder-' not in html and "Empty" in html and "1 document<" in html
        assert "Upcoming dates" not in html
        assert "the last day to give notice if you want to cancel it" in html
        assert f'href="/documents/{document_id}/file"' in html
        r = await client.get(f"/documents/{document_id}/file")
        assert r.status_code == 200 and r.headers["content-type"] == "application/pdf"
        assert r.headers["content-disposition"].startswith("inline")
        assert r.content.startswith(b"%PDF")
        # One that does not exist and paths that leave the folder: nothing.
        assert (await client.get("/documents/999/file")).status_code == 404
        await registry.update(engine.db, document_id, file="../../agent.db")
        assert (await client.get(f"/documents/{document_id}/file")).status_code == 404
        await registry.update(engine.db, document_id, file="/etc/hosts")
        assert (await client.get(f"/documents/{document_id}/file")).status_code == 404
        # The “Today” card leads to the page.
        assert 'href="/documents"' in (await client.get("/")).text
        # Without the panel cookie it is not visible.
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=p.app), base_url=f"http://127.0.0.1:{p.port}"
    ) as anonymous:
        assert (await anonymous.get(f"/documents/{document_id}/file")).status_code == 401


def test_each_document_goes_to_its_folder():
    """The Documents folders are decided by the code: the type and, if that is not
    enough, whole words or their beginning, without confusing “authorization” with a car or
    “Mastercard” with a master's degree. An insurance goes to Car or to Health if it is about
    that. English first and other languages too."""
    from aide.paperwork.categories import CATEGORIES, category

    cases = [
        ({"kind": "housing", "title": "Apartment lease"}, "housing"),
        (
            {"kind": "municipality", "title": "Confirmation", "entity": "Town of Exampleton"},
            "housing",
        ),
        ({"kind": "work", "title": "Employment contract"}, "work"),
        ({"kind": "education", "title": "Bachelor's degree"}, "education"),
        ({"kind": "car", "title": "Vehicle tax 2026"}, "car"),
        ({"kind": "health", "title": "Health insurance policy"}, "health"),
        ({"kind": "insurance", "title": "Vehicle policy", "entity": "Insurer"}, "car"),
        ({"kind": "insurance", "title": "Car insurance"}, "car"),
        ({"kind": "insurance", "title": "Autoversicherung"}, "car"),
        ({"kind": "insurance", "entity": "Example Health Insurance Inc."}, "health"),
        ({"kind": "insurance", "entity": "Krankenkasse Beispiel AG"}, "health"),
        ({"kind": "insurance", "title": "Home insurance", "entity": "Segurix"}, "insurance"),
        ({"kind": "other", "title": "Weekly work timesheet"}, "work"),
        ({"kind": "other", "title": "Calendario y horario de la semana"}, "work"),
        ({"kind": "contract", "title": "Sublease agreement"}, "housing"),
        ({"kind": "contract", "title": "Untermietvertrag"}, "housing"),
        ({"kind": "contract", "title": "Contrat de location", "summary": "Le loyer"}, "housing"),
        ({"kind": "contract", "original_name": "Arbeitsvertrag.pdf"}, "work"),
        ({"kind": "other", "title": "Medical transcript"}, "health"),
        ({"kind": "other", "title": "Expediente médico"}, "health"),
        ({"kind": "other", "title": "Course diploma"}, "education"),
        ({"kind": "bank", "title": "Mastercard statement"}, "other"),
        ({"kind": "permission", "title": "Authorization", "summary": "Pending process"}, "other"),
        ({"kind": "permission", "title": "Autorización", "summary": "Trámite en curso"}, "other"),
        ({"kind": None, "title": None}, "other"),
    ]
    for document, folder in cases:
        assert category(document) == folder, document
    assert [c[0] for c in CATEGORIES] == [
        "housing",
        "work",
        "education",
        "insurance",
        "car",
        "health",
        "other",
    ]
    assert [c[1] for c in CATEGORIES] == [
        "Housing",
        "Work",
        "Education",
        "Insurance",
        "Car",
        "Health",
        "Other",
    ]


async def test_correcting_and_saving_does_not_break_the_notices(engine, monkeypatch):
    """A correction re-encoded the date notices («"[]"») and, when saving, the notices of all
    documents stopped going out."""
    other = await saved_with_dates(engine, [(7, "the permit expires")], flow.today_of(engine))
    document_id = await proposed(engine, monkeypatch)
    today = flow.today_of(engine)
    expires = today + timedelta(days=20)
    await flow.correct(
        engine, document_id, f"title Apartment insurance\nexpires {expires:%Y-%m-%d}"
    )
    await engine.button(f"doc:sav:{document_id}")
    assert (await registry.get(engine.db, document_id))["state"] == "saved"
    assert "Saved. I'll remind you" in engine.channel.messages[-1].plain_text
    # The 7-day notice of the other document goes out today, and this one's, 7 days before its
    # date.
    assert await reminders.review(engine, at_time(today)) == 1
    assert "the permit expires" in engine.channel.texts()[-1]
    assert await reminders.review(engine, at_time(today + timedelta(days=13))) == 1
    assert "(in 7 days) it expires (Apartment insurance)" in engine.channel.texts()[-1]
    assert other != document_id


def test_the_notices_given_are_only_numbers():
    assert registry.given_notices('"[]"') == [] and registry.given_notices("[60, 30]") == [60, 30]
    assert registry.given_notices('[60, "x", true, 7]') == [60, 7]
    assert registry.given_notices(None) == [] and registry.given_notices("roto") == []


async def test_an_odd_date_does_not_leave_the_others_without_notices(engine):
    today = date(2026, 11, 2)
    document_id = await saved_with_dates(engine, [(7, "the permit expires")], today)
    await engine.db.run(
        "INSERT INTO document_dates(document_id, kind, date, description, notices) "
        "VALUES (?, 'other', ?, 'something', '\"[]\"')",
        (document_id, (today + timedelta(days=3)).isoformat()),
    )
    await engine.db.run(
        "INSERT INTO document_dates(document_id, kind, date, description) "
        "VALUES (?, 'other', '2026-11-03x', 'broken date')",
        (document_id,),
    )
    assert await reminders.review(engine, at_time(today)) == 2
    assert "the permit expires" in engine.channel.texts()[-1]


async def test_many_notices_go_in_several_messages(engine):
    today = date(2026, 11, 2)
    await saved_with_dates(engine, [(7, f"thing {n} expires") for n in range(25)], today)
    assert await reminders.review(engine, at_time(today)) == 25
    texts = engine.channel.texts()
    assert len(texts) == 2 and texts[-2].count("\n- ") == 20 and texts[-1].count("\n- ") == 5


async def test_expiry_redoes_the_notice_deadline(engine, monkeypatch):
    document_id = await proposed(engine, monkeypatch)
    await flow.correct(engine, document_id, "expires on 2028-12-31")
    dates = await registry.dates_of(engine.db, document_id)
    assert [(f["kind"], f["date"]) for f in dates] == [
        ("advance_notice", "2028-09-30"),
        ("expiry", "2028-12-31"),
    ]
    assert "3 months before Dec 31, 2028" in dates[0]["description"]


def test_a_correction_has_to_be_a_whole_one():
    assert flow.is_correction("expires on 2027-12-31\nnotice 1 month and 15 days")
    assert flow.line("notice 1 month and 15 days") == ("advance_notice", (1, 15))
    assert flow.line("notice of 2 weeks before") == ("advance_notice", (0, 14))
    assert flow.line("Title:Car insurance") == ("title", "Car insurance")
    assert flow.line("type insurance") == ("kind", "insurance")
    assert flow.line("type local government") == ("kind", "municipality")
    assert flow.line("issuer Segurix") == ("entity", "Segurix")
    assert flow.line("No. 12.345") == ("number", "12.345")
    assert flow.line("remove date 2") == ("quita", 2)
    assert flow.line("date 2027-03-15 the second installment is due") == (
        "date",
        (date(2027, 3, 15), "the second installment is due"),
    )
    # And in Spanish, as before.
    assert flow.is_correction("vence el 31-12-2027\npreaviso 1 mes y 15 días")
    assert flow.line("preaviso 1 mes y 15 días") == ("advance_notice", (1, 15))
    assert flow.line("Título:Seguro del coche") == ("title", "Seguro del coche")
    assert flow.line("tipo seguro") == ("kind", "insurance")
    assert flow.line("quita la fecha 1") == ("quita", 1)
    for message in (
        "Number of the Segurix hotline?",
        "Summary of today's meeting, please",
        "Delete yesterday's email",
        "expires soon",
        "notice 3 months, I think",
        "expires 2027-12-31\nAnd another thing",
        "No, thanks",
        "Número de teléfono de Segurix?",
        "Resumen de la reunión de hoy, porfa",
        "Borra el correo de ayer",
        "vence pronto",
        "preaviso 3 meses, creo",
        "vence 31-12-2027\nY otra cosa",
    ):
        assert not flow.is_correction(message), message


def test_the_date_has_to_be_in_its_sentence():
    day = date(2027, 12, 31)
    for phrase in (
        "Contract term until 31.12.2027",
        "until 31/12/27",
        "ends 2027-12-31",
        "expires December 31, 2027",
        "expires on the 31st of December 2027",
        "endet am 31. Dezember 2027",
        "vence el 31 de diciembre de 2027",
        "expire le 31 décembre 2027",
        "termina em 31 de dezembro de 2027",
    ):
        assert flow.date_in_phrase(day, phrase), phrase
    assert not flow.date_in_phrase(date(2028, 12, 31), "Contract term until 31.12.2027")
    read = [{**READ["dates"][0], "date": "2028-12-31"}]
    [date_, advance_notice] = flow.valid_dates(read, "Contract term until 31.12.2027")
    assert not date_["checked"] and not advance_notice["checked"]


async def test_a_deduced_date_is_marked_for_review(engine, monkeypatch):
    pdf = download(engine, "Policy.pdf")
    pdf_insurance_policy(pdf)
    model(monkeypatch, {**READ, "dates": [{**READ["dates"][0], "date": "2028-12-31"}]})
    assert await flow.since_telegram(engine, "document", [pdf])
    text = engine.channel.messages[-1].plain_text
    assert (
        "That date isn't written like that in the document, I inferred it: please check it." in text
    )


async def test_discarding_forgets_what_was_read(engine, monkeypatch):
    document_id = await proposed(engine, monkeypatch)
    await engine.button(f"doc:no:{document_id}")
    d = await registry.get(engine.db, document_id)
    assert d["state"] == "discarded" and d["text"] is None and d["summary"] is None
    assert d["number"] is None and await registry.dates_of(engine.db, document_id) == []
    closing = engine.channel.messages[-1].plain_text
    assert "I've deleted my copy and what I read" in closing and "12.345.678" not in closing


async def test_a_failure_after_moving_it_leaves_no_files(engine, monkeypatch):
    pdf = download(engine, "Policy.pdf")
    pdf_insurance_policy(pdf)
    # An impossible year with its notice period no longer breaks anything: that date is not
    # valid.
    model(monkeypatch, {**READ, "dates": [{**READ["dates"][0], "date": "0001-01-15"}]})
    assert await flow.since_telegram(engine, "document", [pdf])
    [d] = await engine.db.everything("SELECT * FROM documents")
    assert await registry.dates_of(engine.db, d["id"]) == []

    async def broken(*a: Any, **k: Any) -> int:
        raise RuntimeError("the database fails")

    other = download(engine, "Other.pdf")
    pdf_insurance_policy(other)
    monkeypatch.setattr(registry, "create", broken)
    assert await flow.since_telegram(engine, "document", [other])
    assert 'I couldn\'t read "Other.pdf"' in engine.channel.texts()[-1]
    files = {p.name for p in engine.config.paths.documents.rglob("*.pdf")}
    assert len(files) == 1  # only the first, which does have its row


async def test_german_ss_is_found_both_ways(engine, monkeypatch):
    photo = download(engine, "foto-2.jpg")
    photo.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)
    model(monkeypatch, {**READ, "text": "Wohnung an der Bahnhofstraße 12"})
    assert await flow.since_telegram(engine, "document", [photo])
    [d] = await engine.db.everything("SELECT id FROM documents")
    await engine.button(f"doc:sav:{d['id']}")
    for query in ("Bahnhofstraße", "Bahnhofstrasse"):
        assert [x["id"] for x in await registry.search(engine.db, query)] == [d["id"]]


async def test_the_texts_on_closing_and_of_the_gemeinde(engine, monkeypatch):
    document_id = await proposed(engine, monkeypatch)
    await engine.button(f"doc:sav:{document_id}")
    closing = engine.channel.messages[-1].plain_text
    assert "If you save it" not in closing and closing.count("I'll remind you of each date") == 1
    d = await registry.get(engine.db, document_id)
    municipality = {**d, "kind": "municipality", "entity": "Town of Exampleton"}
    text = await flow.proposal_text(engine.db, municipality, date(2026, 10, 25))
    assert "Letter from Town of Exampleton" in text and "letter from Town" not in text
    # The notice for the cancellation deadline does not carry two consecutive parentheses.
    [advance_notice] = [
        f for f in await registry.dates_of(engine.db, document_id) if f["kind"] == "advance_notice"
    ]
    notice = reminders.notice_text({**advance_notice, "title": "Insurance"}, date(2027, 9, 23))
    assert ") (" not in notice and notice.endswith("Dec 31, 2027 (Insurance).")


def test_the_reading_does_not_save_the_conversation(config_tmp):
    from aide.core import call

    class N:
        config = config_tmp

    options = call.options(N(), role="documents", system="x", schema={}, save_session=False)
    assert options.extra_args == {"no-session-persistence": None}
    assert call.options(N(), role="documents", system="x", schema={}).extra_args == {}


async def test_the_reading_is_requested_without_saving_the_session(engine, monkeypatch):
    pdf = download(engine, "Policy.pdf")
    pdf_insurance_policy(pdf)
    seen = model(monkeypatch, READ)
    assert await flow.since_telegram(engine, "document", [pdf])
    assert seen[0]["save_session"] is False


def test_the_documents_folders_are_seen_only_by_the_user(tmp_path):
    origin = tmp_path / "cache" / "77" / "a.pdf"
    origin.parent.mkdir(parents=True)
    origin.write_bytes(b"%PDF")
    [moved] = flow.move([origin], tmp_path / "documents")
    assert oct(moved.parent.stat().st_mode & 0o777) == "0o700"
    assert oct((tmp_path / "documents").stat().st_mode & 0o777) == "0o700"


async def test_dates_are_saved_with_their_notices_properly_written(engine):
    document_id = await registry.create(
        engine.db, {"title": "X", "kind": "other", "file": "2026/x.pdf"}, []
    )
    await registry.put_dates(
        engine.db,
        document_id,
        [
            {
                "kind": "other",
                "date": "2027-01-01",
                "description": "something",
                "notices": "[60, 30]",
            }
        ],
    )
    [f] = await registry.dates_of(engine.db, document_id)
    assert f["notices"] == "[60, 30]"


# --- from test_documents_2121 ---------------------------------------------


@pytest.fixture(autouse=True)
def no_waits(monkeypatch):
    monkeypatch.setattr(flow, "RETRY_SECONDS", 0)


def texts(m: Engine) -> list[str]:
    return [x.plain_text for x in m.channel.messages]


def test_any_caption_that_starts_with_documento():
    for caption in (
        "Documents",
        "document:",
        "DOCUMENT",
        "📄 Documents: the insurance",
        "Document!",
        "Documentos",
        "document:",
        "DOCUMENTO",
        "📄 Documentos: el seguro",
        "¡Documento!",
    ):
        assert flow.caption(caption) is not None, caption
    assert flow.caption("Document to save in the documentation section") == (
        "to save in the documentation section"
    )
    assert flow.caption("Documento a guardar en la sección documentación") == (
        "a guardar en la sección documentación"
    )
    for caption in (
        "documentation for the apartment",
        "my document",
        "software expense",
        "documentación del piso",
        "mi documento",
    ):
        assert flow.caption(caption) is None, caption


async def test_several_files_with_one_caption_each_its_own_document(engine, monkeypatch):
    pdfs = []
    for name in ("One.pdf", "Two.pdf"):
        pdf = download(engine, name)
        pdf_insurance_policy(pdf)
        pdfs.append(pdf)
    seen = model(monkeypatch, READ)
    await engine.receive(Entry("document", 0, text="Documents", files=pdfs, message_id=4))
    rows = await engine.db.everything("SELECT original_name FROM documents ORDER BY id")
    assert [f["original_name"] for f in rows] == ["One.pdf", "Two.pdf"] and len(seen) == 2
    proposals = [
        m for m in engine.channel.messages if m.buttons_data and "doc:sav" in m.buttons_data[0]
    ]
    assert len(proposals) == 2
    assert await engine.tasks.list_items(limit=5, kind="task") == []


async def test_a_file_without_a_caption_asks_with_buttons(engine, monkeypatch):
    pdf = download(engine, "Contract.pdf")
    pdf_insurance_policy(pdf)
    await engine.receive(Entry("document", 0, files=[pdf], message_id=5))
    question = engine.channel.messages[-1]
    assert question.plain_text == 'I got "Contract.pdf". What should I do with it?'
    key = question.buttons_data[0].split(":")[2]
    assert question.buttons_data == [f"pending:doc:{key}", f"pending:other:{key}"]
    assert await engine.tasks.list_items(limit=5, kind="task") == []  # neither triage nor task
    model(monkeypatch, READ)
    await engine.receive(Entry("button", 0, button_datum=f"pending:doc:{key}"))
    assert await engine.db.value("SELECT COUNT(*) FROM documents") == 1
    # The same button again, or that of other files, no longer does anything.
    await engine.receive(Entry("button", 0, button_datum=f"pending:doc:{key}"))
    assert "no longer waiting" in texts(engine)[-1]


async def test_without_a_caption_it_is_a_document_or_something_else(engine, monkeypatch):
    photos = [download(engine, f"ticket-{i}.jpg") for i in (1, 2)]
    for f in photos:
        f.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)
    await engine.receive(Entry("image", 0, files=photos, message_id=6))
    assert texts(engine)[-1] == "I got 2 files. What should I do with them?"
    # The button of an expense no longer exists: it does nothing.
    key = engine.channel.messages[-1].buttons_data[0].split(":")[2]
    before = len(engine.channel.messages)
    await engine.receive(Entry("button", 0, button_datum=f"pending:gas:{key}"))
    assert len(engine.channel.messages) == before
    # “Something else”: waits for the user to say it, and the message goes with the files.
    photo = download(engine, "whiteboard.jpg")
    photo.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)
    await engine.receive(Entry("image", 0, files=[photo], message_id=7))
    key = engine.channel.messages[-1].buttons_data[0].split(":")[2]
    await engine.receive(Entry("button", 0, button_datum=f"pending:other:{key}"))
    assert texts(engine)[-1] == "Tell me what you'd like me to do with it and I'll take a look."
    model(monkeypatch, READ)
    await engine.receive(Entry("text", 0, text="document", message_id=8))
    assert await engine.db.value("SELECT COUNT(*) FROM documents") == 1


async def test_a_file_without_a_caption_replying_to_a_task_goes_to_that_task(engine, monkeypatch):
    consecutive: list[int] = []

    async def task_from_message(message_id):
        return 7

    async def continue_task(task_id, text, entry):
        consecutive.append(task_id)

    monkeypatch.setattr(engine.tasks, "task_from_message", task_from_message)
    monkeypatch.setattr(engine, "continue_task", continue_task)
    pdf = download(engine, "Order.pdf")
    pdf_insurance_policy(pdf)
    await engine.receive(Entry("document", 0, files=[pdf], message_id=9, replies_to=99))
    assert consecutive == [7] and not any("What should I do" in t for t in texts(engine))


async def test_if_the_model_does_not_respond_it_retries_once(engine, monkeypatch):
    from aide.core import call

    replies = [None, READ]

    async def structured(core, **kw):
        return replies.pop(0)

    monkeypatch.setattr(call, "structured", structured)
    pdf = download(engine, "Policy.pdf")
    pdf_insurance_policy(pdf)
    assert await flow.since_telegram(engine, "document", [pdf])
    assert await engine.db.value("SELECT COUNT(*) FROM documents") == 1 and replies == []
    # If it does not respond on retry either, the user is told.
    model(monkeypatch, None)
    other = download(engine, "Otro.pdf")
    pdf_insurance_policy(other)
    assert await flow.since_telegram(engine, "document", [other])
    assert "even after retrying" in texts(engine)[-1]


async def test_a_word_file_is_read_as_a_pdf(engine, monkeypatch):
    from aide.core import documents

    def to_pdf(path: Path, folder: Path) -> Path:
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / f"{path.stem}.pdf"
        pdf_insurance_policy(destination)
        return destination

    monkeypatch.setattr(documents, "to_pdf", to_pdf)
    seen = model(monkeypatch, READ)
    word = download(engine, "Contract.docx")
    word.write_bytes(b"PK")
    assert await flow.since_telegram(engine, "document", [word])
    [d] = await engine.db.everything("SELECT * FROM documents")
    assert d["origin_text"] == "pdf" and "Contract term" in seen[0]["prompt"]


async def test_an_album_arrives_as_a_single_entry():
    from aide.bot.telegram import Albums

    delivered: list[Entry] = []

    async def deliver(entry: Entry) -> None:
        delivered.append(entry)

    albums = Albums(deliver, wait=0.05)
    for _part in (1, 2, 3):
        albums.starts("g1")
    for i, caption in ((2, ""), (1, "Documents"), (3, "")):
        albums.ends(
            "g1", Entry("document", 0, text=caption, message_id=i, files=[Path(f"{i}.pdf")])
        )
    await asyncio.sleep(0.3)
    [unique] = delivered
    assert unique.text == "Documents" and unique.kind == "document"
    assert unique.files == [Path("1.pdf"), Path("2.pdf"), Path("3.pdf")]
    # If one part could not be downloaded, the album is still closed with the others.
    albums.starts("g2")
    albums.starts("g2")
    albums.ends("g2", Entry("image", 0, message_id=5, files=[Path("a.jpg")]))
    albums.ends("g2", None)
    await asyncio.sleep(0.3)
    assert delivered[-1].files == [Path("a.jpg")] and delivered[-1].kind == "image"


def to_inbox(m: Engine, relative: str, pdf: bool = True) -> Path:
    destination = m.config.paths.inbox / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if pdf:
        # Each with its own policy number: two PDFs with the same text would be duplicates
        # .
        pdf_insurance_policy(destination, number=f"{zlib.crc32(relative.encode()) % 10**8:08d}")
    else:
        destination.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)
    return destination


async def test_the_inbox_says_how_many_there_are_and_how_much_it_will_cost(engine):
    await inbox.ask(engine)
    assert texts(engine)[-1].startswith("There are no new files in the inbox.")
    to_inbox(engine, "insurance.pdf")
    to_inbox(engine, "apartment/lease.pdf")
    to_inbox(engine, "permit.jpg", pdf=False)
    (engine.config.paths.inbox / "notes.xyz").write_text("x")
    (engine.config.paths.inbox / ".DS_Store").write_text("x")
    await inbox.ask(engine)
    question = engine.channel.messages[-1]
    assert "There are 3 new documents in the inbox (2 PDFs and 1 photo)." in question.plain_text
    assert "Reading them will use about " in question.plain_text
    assert "tokens" in question.plain_text
    assert 'I can\'t read "notes.xyz": it stays in the inbox.' in question.plain_text
    assert question.buttons_data == ["inbox:read:0", "inbox:nothing:0"]
    assert inbox.tokens_of(engine.config.paths.inbox / "insurance.pdf") > inbox.TOKENS_BASE


async def test_read_save_all_and_the_inbox_is_left_empty(engine, monkeypatch):
    model(monkeypatch, READ)
    to_inbox(engine, "insurance.pdf")
    to_inbox(engine, "apartment/lease.pdf")
    await inbox.read_all(engine)
    summary = engine.channel.messages[-1]
    assert summary.plain_text.startswith("I read 2 documents from the inbox")
    assert "Segurix home insurance" in summary.plain_text and "Dec 31, 2027" in summary.plain_text
    assert summary.buttons_data == ["inbox:sav:1", "inbox:rev:1", "inbox:des:1"]
    rows = await engine.db.everything("SELECT * FROM documents ORDER BY id")
    assert [f["inbox"] for f in rows] == ["apartment/lease.pdf", "insurance.pdf"]
    assert all(f["state"] == "proposed" and f["fingerprint"] for f in rows)
    # Nothing leaves the inbox until it is saved.
    assert (engine.config.paths.inbox / "insurance.pdf").is_file()
    await engine.receive(Entry("button", 0, button_datum="inbox:sav:1"))
    assert texts(engine)[-1] == "Saved 2 documents. The inbox is now empty."
    rows = await engine.db.everything("SELECT * FROM documents ORDER BY id")
    assert all(f["state"] == "saved" and not f["inbox"] for f in rows)
    for f in rows:
        assert (engine.config.paths.documents / f["file"]).is_file()
    assert list(engine.config.paths.inbox.iterdir()) == []  # without the empty subfolder
    await engine.receive(Entry("button", 0, button_datum="inbox:sav:1"))
    assert "already been handled" in texts(engine)[-1]


async def test_what_cannot_be_read_stays_in_the_inbox(engine, monkeypatch):
    from aide.core import call

    async def structured(core, **kw):
        return None if "Broken" in kw["prompt"] else READ

    monkeypatch.setattr(call, "structured", structured)
    to_inbox(engine, "Good.pdf")
    bad = to_inbox(engine, "Broken.pdf")
    bad.write_bytes(bad.read_bytes().replace(b"Segurix", b"Broken"))
    await inbox.read_all(engine)
    summary = engine.channel.messages[-1].plain_text
    assert "I read 1 document from the inbox" in summary
    assert 'I couldn\'t read these, which are still in the inbox: "Broken.pdf"' in summary
    await engine.receive(Entry("button", 0, button_datum="inbox:sav:1"))
    assert 'Still in the inbox: "Broken.pdf".' in texts(engine)[-1]
    assert bad.is_file()


async def test_discard_and_review_one_by_one(engine, monkeypatch):
    model(monkeypatch, READ)
    one = to_inbox(engine, "one.pdf")
    await inbox.read_all(engine)
    await engine.receive(Entry("button", 0, button_datum="inbox:des:1"))
    # Discarded: its file goes to Trash and the inbox is left empty.
    assert texts(engine)[-1] == "Discarded 1 document. Its file is in the Trash."
    assert not one.exists() and await inbox.new(engine) == []
    # Review one by one: each with its buttons, and when saved it leaves the inbox.
    two = to_inbox(engine, "two.jpg", pdf=False)
    await inbox.read_all(engine)
    await engine.receive(Entry("button", 0, button_datum="inbox:rev:2"))
    [proposal] = [
        m
        for m in engine.channel.messages
        if m.buttons_data and m.buttons_data[0].startswith("doc:sav")
    ]
    await engine.receive(Entry("button", 0, button_datum=proposal.buttons_data[0]))
    assert not two.exists()
    d = await registry.get(engine.db, int(proposal.buttons_data[0].split(":")[2]))
    assert d["state"] == "saved" and (engine.config.paths.documents / d["file"]).is_file()


async def test_the_command_and_the_panel(engine):
    import httpx

    from aide.bot.telegram import COMMANDS
    from aide.panel.app import Panel

    assert ("documents", "Read the new documents in the inbox") in COMMANDS
    to_inbox(engine, "insurance.pdf")
    await engine.receive(Entry("command", 0, command="documents"))
    assert "There is 1 new document in the inbox (1 PDF)." in texts(engine)[-1]
    p = Panel(engine, token="t")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=p.app), base_url=f"http://127.0.0.1:{p.port}"
    ) as client:
        await client.get("/enter?t=t&go=/")
        html = (await client.get("/documents")).text
        assert 'action="/documents/inbox"' in html and "Read the inbox" in html
        r = await client.post("/documents/inbox", data={"csrf": p.csrf})
        assert r.status_code == 303


def test_the_agent_cannot_read_the_inbox(config_tmp):
    from aide.core.session import sandbox_settings

    pol = Policy(config_tmp)
    ctx = CallContext(task_id=7, task_folder=config_tmp.paths.create_task_folder(7))
    real = config_tmp.paths.root / "Document Inbox" / "insurance.pdf"
    nfd = Path(unicodedata.normalize("NFD", str(real)))
    for path in (real, nfd):
        assert pol.evaluate("Read", {"file_path": str(path)}, ctx).level == FORBIDDEN, path
        assert pol.evaluate("Bash", {"command": f"cat '{path}'"}, ctx).level == FORBIDDEN, path
    assert str(config_tmp.paths.inbox) in sandbox_settings(config_tmp)["filesystem"]["denyRead"]


def test_the_reading_supports_word():
    assert {".docx", ".doc"} <= reading.OFFICE and ".docx" in inbox.READABLE


# --- from test_paperwork_inbox -----------------------------------------------

ROOT = Path(__file__).resolve().parents[1]


def pdf_scan(path: Path, width: float = 1738, height: float = 2584) -> None:
    """A PDF without text with the page the size of the image in points, like those from
    its scanner."""
    from PIL import Image
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=(width, height))
    noise = Image.effect_noise((600, 900), 90).convert("RGB")
    c.drawImage(ImageReader(noise), 0, 0, width, height)
    c.save()


async def saved(m: Engine, monkeypatch, name: str = "Policy.pdf") -> dict:
    """A document read through Telegram and saved with its button."""
    pdf = download(m, name)
    pdf_insurance_policy(pdf)
    model(monkeypatch, READ)
    assert await flow.since_telegram(m, "document", [pdf])
    document_id = await m.db.value("SELECT MAX(id) FROM documents")
    await m.button(f"doc:sav:{document_id}")
    d = await registry.get(m.db, document_id)
    assert d["state"] == "saved"
    return d


def same_policy_other_bytes(path: Path) -> None:
    """The policy from `pdf_insurance_policy` with another layout: the same text in a different
    file."""
    from reportlab.lib.pagesizes import A4
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path), pagesize=A4)
    c.setFont("Helvetica", 11)
    for i, line in enumerate(
        [
            "Segurix Insurance",
            "Policy No. 12.345.678 - Home contents insurance",
            "Contract term until 31.12.2027",
            "Notice period: 3 months before expiry",
            "Annual premium USD 312.00",
        ]
    ):
        c.drawString(90, 700 - 20 * i, line)
    c.save()


def path_in_inbox(m: Engine, relative: str) -> Path:
    destination = m.config.paths.inbox / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    return destination


@pytest.mark.skipif(
    not (shutil.which("pdftoppm") or Path("/opt/homebrew/bin/pdftoppm").exists()),
    reason="sin pdftoppm",
)
def test_a_scan_with_huge_pages_comes_out_as_jpeg_the_model_accepts(tmp_path):
    pdf = tmp_path / "Registration.pdf"
    pdf_scan(pdf)
    pages = reading.pdf_pages(pdf, tmp_path)
    assert len(pages) == 1 and pages[0].suffix == ".jpg"
    from PIL import Image

    with Image.open(pages[0]) as image:
        # What the model makes use of; at 110 dpi, as before, 2,655 × 3,947 pixels came out.
        assert max(image.size) == 1600
    assert pages[0].stat().st_size <= call.MAX_IMAGE


async def test_if_the_image_is_not_valid_it_says_so_and_does_not_retry(
    engine, monkeypatch, tmp_path
):
    image = tmp_path / "pagina-1.jpg"
    image.write_bytes(b"\xff\xd8\xff\xe0" + b"0" * 100)
    monkeypatch.setattr(reading, "pdf_pages", lambda path, folder: [image])
    attempts = []

    def image_block(path):
        attempts.append(path)
        raise ValueError("the image is over 5 MB")

    monkeypatch.setattr(call, "image_block", image_block)
    pdf = download(engine, "Scan.pdf")
    pdf_scan(pdf, 595, 842)
    assert await flow.since_telegram(engine, "document", [pdf])
    assert len(attempts) == 1  # no retry: it would fail the same way again
    assert "its pages can't be turned into images the model accepts" in texts(engine)[-1]
    assert "send me a photo of the document" in texts(engine)[-1]
    assert await engine.db.value("SELECT COUNT(*) FROM documents") == 0


async def test_the_plan_limit_is_stated_with_its_time_and_not_retried(engine, monkeypatch):
    reset = datetime.now(UTC) + timedelta(hours=3)
    calls = []

    async def structured(core, **kw):
        calls.append(kw)
        raise call.NoReply("limit", "seven_day", "seven_day", reset)

    monkeypatch.setattr(call, "structured", structured)
    pdf = download(engine, "Policy.pdf")
    pdf_insurance_policy(pdf)
    assert await flow.since_telegram(engine, "document", [pdf])
    assert len(calls) == 1
    last = texts(engine)[-1]
    assert "because you've reached your plan's weekly limit, which resets " in last
    assert "send it again when it resets" in last
    # In the inbox it stops as soon as the limit is reached: the rest could not be read either.
    calls.clear()
    pdf_insurance_policy(path_in_inbox(engine, "one.pdf"), number="11")
    pdf_insurance_policy(path_in_inbox(engine, "two.pdf"), number="22")
    await inbox.read_all(engine)
    assert len(calls) == 1
    summary = texts(engine)[-1]
    assert "you've reached your plan's weekly limit" in summary
    assert (
        '"one.pdf" (I didn\'t get to read it)' in summary
        or '"two.pdf" (I didn\'t get to read it)' in summary
    )


async def test_if_the_session_drops_the_saved_limits_are_checked(engine):
    usage = engine.core.usage
    assert (await call._no_reply(engine.core, "x")).reason == "model"
    future = datetime.now(UTC) + timedelta(hours=2)
    await usage.log_limit(
        SimpleNamespace(
            rate_limit_type="five_hour",
            status="rejected",
            utilization=1.0,
            resets_at=future.timestamp(),
            raw={},
        )
    )
    e = await call._no_reply(engine.core, "x")
    assert e.reason == "limit" and e.kind == "five_hour"
    assert "you've reached your plan's 5-hour limit" in flow.why(engine, e)
    # A rejection whose reset has already passed does not count.
    past = datetime.now(UTC) - timedelta(minutes=5)
    await usage.log_limit(
        SimpleNamespace(
            rate_limit_type="five_hour",
            status="rejected",
            utilization=1.0,
            resets_at=past.timestamp(),
            raw={},
        )
    )
    assert (await call._no_reply(engine.core, "x")).reason == "model"


def test_the_same_text():
    a = "Policy No. 12.345.678\nContract term until 31.12.2027\nAnnual premium USD 312.00"
    assert duplicates.same_text(a, a.upper().replace("\n", "   "))
    # The same template with other numbers (another year, another premium) is another document.
    assert not duplicates.same_text(a, a.replace("2027", "2028"))
    assert not duplicates.same_text(a, "")


async def test_the_inbox_does_not_read_what_is_already_saved(engine, monkeypatch):
    d = await saved(engine, monkeypatch)
    file = engine.config.paths.documents / d["file"]
    shutil.copy(file, path_in_inbox(engine, "exact copy.pdf"))
    same_policy_other_bytes(path_in_inbox(engine, "other download.pdf"))
    pdf_insurance_policy(path_in_inbox(engine, "new.pdf"), number="98.765.432")
    shutil.copy(engine.config.paths.inbox / "new.pdf", path_in_inbox(engine, "sub/new 2.pdf"))
    await inbox.ask(engine)
    question = texts(engine)[-1]
    assert "There is 1 new document in the inbox (1 PDF)." in question
    assert '"exact copy.pdf" (it\'s the same file as "Segurix home insurance"' in question
    assert '"other download.pdf" (it has the same text as "Segurix home insurance"' in question
    assert '"sub/new 2.pdf" (it\'s the same file as "new.pdf")' in question
    # The duplicates are already in Trash: they are not read and the inbox only has the
    # new one.
    assert "I moved 3 duplicate files to the Trash:" in question
    assert [p.name for p in inbox._visible(engine.config.paths.inbox)] == ["new.pdf"]
    seen = model(monkeypatch, {**READ, "number": "98.765.432"})
    await inbox.read_all(engine)
    assert len(seen) == 1  # only the new one


async def test_a_saved_one_without_fingerprint_also_counts(engine, monkeypatch):
    d = await saved(engine, monkeypatch)
    await engine.db.run("UPDATE documents SET fingerprint=NULL WHERE id=?", (d["id"],))
    shutil.copy(engine.config.paths.documents / d["file"], path_in_inbox(engine, "again.pdf"))
    seen = model(monkeypatch, READ)
    await inbox.read_all(engine)
    assert seen == []
    assert texts(engine)[-1].startswith(
        "There are no new documents to read in the inbox. I moved a duplicate file to the "
        'Trash: "again.pdf" (it\'s the same file as "Segurix home insurance"'
    )


async def test_by_telegram_what_is_already_saved_is_not_read_again(engine, monkeypatch):
    d = await saved(engine, monkeypatch)
    seen = model(monkeypatch, READ)
    other = download(engine, "Policy (1).pdf")
    shutil.copy(engine.config.paths.documents / d["file"], other)
    assert await flow.since_telegram(engine, "document", [other])
    assert seen == [] and not other.exists()
    assert texts(engine)[-1].startswith(
        'You already have "Policy (1).pdf" saved as "Segurix home insurance"'
    )
    assert await engine.db.value("SELECT COUNT(*) FROM documents") == 1


async def test_a_scan_that_looks_the_same_is_not_in_save_all(engine, monkeypatch):
    await saved(engine, monkeypatch)
    photo = path_in_inbox(engine, "insurance photo.jpg")
    photo.write_bytes(b"\xff\xd8\xff\xe0" + b"1" * 100)
    pdf_insurance_policy(path_in_inbox(engine, "other.pdf"), number="55.555.555")
    outputs = {"photo": READ, "other": {**READ, "number": "55.555.555", "entity": "Northshield"}}

    async def structured(core, **kw):
        if kw.get("images"):
            return outputs["photo"]
        other = {**outputs["other"]}
        other["dates"] = [{**f, "date": "2028-06-30"} for f in READ["dates"]]
        return other

    monkeypatch.setattr(call, "structured", structured)
    await inbox.read_all(engine)
    summary = texts(engine)[-1]
    assert 'It looks like the same one as "Segurix home insurance", already saved' in summary
    assert summary.count("It looks like the same one") == 1
    batch = int(engine.channel.messages[-1].buttons_data[0].split(":")[2])
    await engine.receive(Entry("button", 0, button_datum=f"inbox:sav:{batch}"))
    final = texts(engine)[-1]
    assert final.startswith("Saved 1 document.")
    assert "which looks like a duplicate of what you already have and is in the Trash." in final
    assert final.endswith("The inbox is now empty.")
    assert not photo.exists() and await inbox.new(engine) == []


def test_the_inbox_is_not_in_git():
    patterns = [
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.startswith("#")
    ]
    for shape in ("NFC", "NFD"):
        name = unicodedata.normalize(shape, "Document Inbox/")
        assert any(fnmatch.fnmatchcase(name, p) for p in patterns), shape


# --- the inbox empties itself --------------------------------------------------------------


async def test_what_was_already_discarded_and_empty_folders_go_to_trash(engine, fake_trash):
    """What was discarded earlier (its fingerprint is in the database) and subfolders with
    nothing visible go to Trash when the inbox is checked; a folder with something does not."""
    old = path_in_inbox(engine, "apartment/old lease.pdf")
    pdf_insurance_policy(old, number="77")
    await engine.db.run(
        "INSERT INTO documents(created, state, file, inbox, fingerprint) "
        "VALUES ('2026-10-27T10:00:00+00:00', 'discarded', '', ?, ?)",
        ("apartment/old lease.pdf", flow.fingerprint(old)),
    )
    empty = engine.config.paths.inbox / "Insurance" / "2026"
    empty.mkdir(parents=True)
    (empty / ".DS_Store").write_bytes(b"x")
    (engine.config.paths.inbox / "Insurance" / ".DS_Store").write_bytes(b"x")
    new = path_in_inbox(engine, "Work/new.pdf")
    pdf_insurance_policy(new, number="88")
    await inbox.ask(engine)
    text = texts(engine)[-1]
    assert (
        'I moved a duplicate file to the Trash: "apartment/old lease.pdf" '
        "(you already discarded it)." in text
    )
    assert not old.exists() and not (engine.config.paths.inbox / "apartment").exists()
    assert not (engine.config.paths.inbox / "Insurance").exists()
    assert new.is_file() and engine.config.paths.inbox.is_dir()  # the new one and the inbox stay


async def test_if_it_cannot_be_moved_to_trash_it_stays_and_says_so(engine, monkeypatch, fake_trash):
    model(monkeypatch, READ)
    one = path_in_inbox(engine, "one.pdf")
    pdf_insurance_policy(one, number="1")
    await inbox.read_all(engine)
    fake_trash.fails = True
    await engine.receive(Entry("button", 0, button_datum="inbox:des:1"))
    assert texts(engine)[-1] == (
        'Discarded 1 document. I couldn\'t move "one.pdf" to the Trash: please delete it from '
        "the inbox yourself."
    )
    assert one.is_file()


async def test_discarding_one_by_one_sends_it_to_trash(engine, monkeypatch):
    model(monkeypatch, READ)
    two = path_in_inbox(engine, "two.pdf")
    pdf_insurance_policy(two, number="2")
    await inbox.read_all(engine)
    await engine.receive(Entry("button", 0, button_datum="inbox:rev:1"))
    [proposal] = [
        m
        for m in engine.channel.messages
        if m.buttons_data and m.buttons_data[0].startswith("doc:sav")
    ]
    document_id = int(proposal.buttons_data[0].split(":")[2])
    await engine.button(f"doc:no:{document_id}")
    assert not two.exists()
    assert any("Its file is in the Trash." in x for x in texts(engine))


def test_the_real_trash_without_the_command(tmp_path, monkeypatch, fake_trash):
    """Without the trash command nothing is deleted: the file stays and it says it could not be
    done."""
    file = tmp_path / "a.pdf"
    file.write_bytes(b"x")
    monkeypatch.setattr(inbox, "TRASH", str(tmp_path / "no-existe"))
    monkeypatch.setattr(inbox.shutil, "which", lambda name: None)
    assert fake_trash.real(file) is False and file.is_file()
