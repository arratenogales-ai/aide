"""F5: Gmail, Calendar and Google authorization expiry, with a fake Google.

Google permissions: no scope or tool may delete or move emails. These tests fail on
purpose if someone adds a scope, a Gmail tool or an API call that deletes, moves to
Trash or changes email labels. If it were ever needed, talk to the user first; then
change these lists. Checked against Google on 19-10-2026 with the real token: the grant
has exactly the three scopes of ALLOWED, and trash, delete, modify, batchDelete and
batchModify of messages and threads return 403 (insufficient scope). gmail.compose does
allow deleting drafts; that is why the drafts().delete() call is also forbidden in the
code."""

from __future__ import annotations

import base64
import json
import re
from collections import deque
from datetime import UTC, datetime, time, timedelta
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

import pytest
from google.oauth2.credentials import Credentials

from aide.core.approvals import Request
from aide.core.engine import Engine
from aide.core.idempotency import action_key
from aide.core.orchestrator import AgentSession
from aide.core.states import TaskState
from aide.simulate import Script, Simulator
from aide.tools import google_auth
from aide.tools.google_auth import (
    SCOPES,
    ExcessivePermissions,
    GoogleAuthorization,
    check_permissions,
)
from aide.tools.google_calendar import calculate_slots, event_id, to_datetime
from aide.tools.google_gmail import (
    body_and_attachments,
    build_mime,
    describe_draft,
)
from tests.fake_google import FakeAuthorization, _payload_from_raw
from tests.fake_sdk import OWN, End, FakeClient, Recorder, install_own, own

# --- de test_google --------------------------------------------------------

BERLIN = ZoneInfo("Europe/Berlin")


USER = "user@example.com"


LINK = "http://127.0.0.1:8765/enter?t=x&go=/google/authorize"


def _b64(text: str) -> str:
    return base64.urlsafe_b64encode(text.encode()).decode()


def test_body_and_attachments_prefers_plain_text_and_lists_attachments():
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": _b64("Hi there")}},
                    {
                        "mimeType": "text/html",
                        "body": {"data": _b64("<p>Hi <b>there</b></p>")},
                    },
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "cv.pdf",
                "body": {"size": 2048, "attachmentId": "x"},
            },
        ],
    }
    text, attachments = body_and_attachments(payload)
    assert text == "Hi there"
    assert attachments == [{"name": "cv.pdf", "kind": "application/pdf", "size": 2048}]
    # HTML only: converted to text without tags or scripts.
    only_html = {
        "mimeType": "text/html",
        "body": {"data": _b64("<script>x()</script><p>One</p><p>Two &amp; three</p>")},
    }
    assert body_and_attachments(only_html)[0] == "One\nTwo & three"


def test_mime_draft_and_fingerprint(tmp_path):
    attachment = tmp_path / "letter.txt"
    attachment.write_text("letter")
    raw = build_mime(
        USER,
        [USER],
        "Test",
        "Email body",
        cc=["contact@example.com"],
        attachments=[attachment],
    )
    payload, header = _payload_from_raw(raw)
    assert (
        header["To"] == USER
        and header["Cc"] == "contact@example.com"
        and header["Subject"] == "Test"
    )
    text, fingerprint = describe_draft({"message": {"payload": payload}})
    assert "To: user@example.com" in text and "CC: contact@example.com" in text
    assert "Subject: Test" in text and "Attachments: letter.txt" in text and "Email body" in text
    # Changing the text, recipients or attachments changes the fingerprint.
    other = build_mime(
        USER, [USER], "Test", "Another body", cc=["contact@example.com"], attachments=[attachment]
    )
    assert describe_draft({"message": {"payload": _payload_from_raw(other)[0]}})[1] != fingerprint
    same = build_mime(
        USER,
        [USER],
        "Test",
        "Email body",
        cc=["contact@example.com"],
        attachments=[attachment],
    )
    assert describe_draft({"message": {"payload": _payload_from_raw(same)[0]}})[1] == fingerprint


def test_dates_and_gaps():
    assert to_datetime("2026-10-21T10:00", BERLIN) == datetime(2026, 10, 21, 10, 0, tzinfo=BERLIN)
    assert to_datetime("2026-10-21T08:00Z", BERLIN).hour == 10  # UTC+2 in October
    assert to_datetime("2026-10-21", BERLIN) == datetime(2026, 10, 21, tzinfo=BERLIN)
    since = datetime(2026, 10, 23, 0, 0, tzinfo=BERLIN)  # viernes
    until = since + timedelta(days=3)  # until Monday 00:00
    busy = [
        (since.replace(hour=10), since.replace(hour=11)),
        (since.replace(hour=10, minute=30), since.replace(hour=12)),  # solapado
        (since.replace(hour=16, minute=30), since.replace(hour=17, minute=15)),
    ]
    slots = calculate_slots(
        busy, since, until, timedelta(minutes=60), time(9), time(18), [0, 1, 2, 3, 4]
    )
    assert [(a.strftime("%a %H:%M"), b.strftime("%H:%M")) for a, b in slots] == [
        ("Fri 09:00", "10:00"),
        ("Fri 12:00", "16:30"),
    ]  # 17:15-18:00 is short and the weekend does not count


def test_valid_event_id_for_google():
    key = action_key(7, "create_event", "", {"title": "x"})
    eid = event_id(key)
    assert 5 <= len(eid) <= 1024 and set(eid) <= set("0123456789abcdefghijklmnopqrstuv")
    assert eid == event_id(key)


@pytest.fixture
async def environment(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    install_own(monkeypatch)
    engine = Engine(config_tmp)
    await engine.start()
    google = FakeAuthorization()
    engine.core.services["google"] = google
    engine.core.services["expired_google"] = engine.expired_google
    engine.core.services["google_link"] = lambda: LINK
    sim = Simulator(engine, Script(), print_out=False)
    yield engine, sim, google
    await engine.stop()


def results(name: str) -> list[str]:
    return [r for h, r in FakeClient.registry.tools_results if h == f"mcp__aide__{name}"]


async def test_email_and_calendar_end_to_end(environment):
    engine, sim, google = environment
    # Deterministic ids of the fake Gmail: m1 is the sample email and d2 the draft.
    FakeClient.script = lambda p, o, r: [
        own("gmail_search", {"query": "application"}),
        own(
            "gmail_create_draft",
            {"to": [USER], "subject": "Test", "body": "Hi, this is your agent."},
        ),
        own(
            "gmail_create_draft",
            {"to": [USER], "subject": "Test", "body": "Hi, this is your agent."},
        ),
        own("gmail_send_draft", {"draft_id": "d2"}),
        own("gmail_send_draft", {"draft_id": "d2"}),
        own(
            "calendar_create_event",
            {"title": "Review", "start": "2026-10-21T10:00", "end": "2026-10-21T11:00"},
        ),
        own(
            "calendar_create_event",
            {"title": "Review", "start": "2026-10-21T10:00", "end": "2026-10-21T11:00"},
        ),
        own(
            "calendar_slots",
            {"since": "2026-10-21T00:00", "until": "2026-10-22T00:00", "duration": 60},
        ),
        own(
            "calendar_create_event",
            {
                "title": "Coffee",
                "start": "2026-10-22T10:00",
                "end": "2026-10-22T10:30",
                "guests": ["contact@example.com"],
            },
        ),
        End("<delivery>Hecho.</delivery>"),
    ]
    sim.script.approve = deque(["yes", "no"])  # sending does; the event with guests does not
    [task_id] = await sim.send("Search my emails, prepare a test email for me and plan Wednesday")

    assert "Your application" in results("gmail_search")[0]
    created = results("gmail_create_draft")
    assert '"draft_id": "d2"' in created[0] and "it was not duplicated" in created[1]
    assert list(google.gmail.stored_drafts) == []  # the only draft was sent
    assert google.gmail.sent == [{"id": "s3", "to": USER, "subject": "Test"}]

    sends = results("gmail_send_draft")
    assert sends[0] == "d2: sent to user@example.com."
    denied = sends[1]
    assert denied.startswith("DENEGADA") and "does not exist" in denied

    approvals = await engine.db.everything(
        "SELECT * FROM approvals WHERE task_id=? ORDER BY id", (task_id,)
    )
    assert [(a["kind"], a["state"]) for a in approvals] == [
        ("email", "approved"),
        ("event", "rejected"),
    ]
    assert approvals[0]["fingerprint"] and "To: user@example.com" in approvals[0]["detail"]
    assert "Hi, this is your agent." in approvals[0]["detail"]
    assert "Guests: contact@example.com" in approvals[1]["detail"]
    assert "Date: Thursday, October 22, 2026" in approvals[1]["detail"]

    events = list(google.calendar.stored_events.values())
    assert [e["summary"] for e in events] == ["Review"]
    assert google.calendar.sends == [(events[0]["id"], "none")]  # no guests, no emails
    assert "already been created" in results("calendar_create_event")[1]
    assert results("calendar_create_event")[2].startswith("DENEGADA")
    slots = results("calendar_slots")[0]
    assert (
        '"start": "2026-10-21T09:00+02:00"' in slots
        and '"start": "2026-10-21T11:00+02:00"' in slots
    )
    assert any(
        "I created an event in your calendar" in t and "Review" in t for t in sim.channel.texts()
    )

    task = await engine.tasks.require(task_id)
    assert task.state == TaskState.COMPLETED
    done = await engine.core.actions.done_descriptions(task_id)
    assert {h.split(" to ")[0] for h in done} == {"draft", "send_email", "create_event"}


@pytest.mark.parametrize(
    "decisions,sent",
    [(["batch:everything"], 2), (["batch:one", "yes", "no"], 1), (["batch:cancel"], 0)],
)
async def test_email_batch(environment, decisions, sent):
    engine, sim, google = environment
    FakeClient.script = lambda p, o, r: [
        own("gmail_create_draft", {"to": [USER], "subject": "One", "body": "First"}),
        own("gmail_create_draft", {"to": [USER], "subject": "Two", "body": "Second"}),
        own("gmail_send_drafts", {"drafts_ids": ["d2", "d3"]}),
        End("<delivery>Hecho.</delivery>"),
    ]
    sim.script.approve = deque(decisions)
    [task_id] = await sim.send("Prepare and send two test emails")
    assert len(google.gmail.sent) == sent
    if sent == 1:
        assert google.gmail.sent[0]["subject"] == "One" and list(google.gmail.stored_drafts) == [
            "d3"
        ]
    request = next(m for m in sim.channel.messages if "batch:" in "".join(m.buttons_data))
    assert "Send 2 emails" in request.plain_text and "First" in request.plain_text
    assert (await engine.tasks.require(task_id)).state == TaskState.COMPLETED


async def _direct_context(engine: Engine):
    task = await engine.tasks.create("direct test", state=TaskState.RUNNING)
    ses = engine._context(task, "run")
    from aide.tools.server import tools_of

    tools_of(ses)
    return task.id, ses


async def _approve(engine: Engine, task_id: int, draft_id: str, fingerprint: str) -> None:
    s = Request(
        task_id,
        "mcp__aide__gmail_send_draft",
        {"draft_id": draft_id},
        "email",
        "x",
        fingerprint=fingerprint,
    )
    approval_id = await engine.approvals._create(s)
    await engine.db.run("UPDATE approvals SET state='approved' WHERE id=?", (approval_id,))


async def test_sending_requires_approval_with_the_exact_fingerprint(environment):
    engine, _sim, google = environment
    task_id, _ses = await _direct_context(engine)
    create, send = (
        OWN["gmail_create_draft"].handler,
        OWN["gmail_send_draft"].handler,
    )
    await create({"to": [USER], "subject": "Hi", "body": "Approved text"})
    draft = google.gmail.stored_drafts["d2"]

    # Without approval: it is not sent (even if someone skips the policy).
    r = await send({"draft_id": "d2"})
    assert r.get("is_error") and "there is no valid approval" in r["content"][0]["text"]
    # Approval for other content (the draft changed after approving it): neither.
    await _approve(engine, task_id, "d2", "fingerprint-of-another-text")
    r = await send({"draft_id": "d2"})
    assert r.get("is_error") and google.gmail.sent == []
    # With the fingerprint of the exact content, yes.
    await _approve(engine, task_id, "d2", describe_draft(draft)[1])
    r = await send({"draft_id": "d2"})
    assert not r.get("is_error") and len(google.gmail.sent) == 1

    # An identical draft (for example, created again after an interruption) is not sent twice
    # either.
    google.gmail.stored_drafts["d9"] = {**draft, "id": "d9"}
    await _approve(engine, task_id, "d9", describe_draft(draft)[1])
    r = await send({"draft_id": "d9"})
    assert "(not repeated)" in r["content"][0]["text"] and not r.get("is_error")
    assert len(google.gmail.sent) == 1
    # Repeating the creation of the same draft returns the one that already existed.
    r = await create({"to": [USER], "subject": "Hi", "body": "Approved text"})
    assert "it was not duplicated" in r["content"][0]["text"]


async def test_interrupted_send_is_checked_in_sent(environment):
    engine, _sim, google = environment
    task_id, _ses = await _direct_context(engine)
    create, send = (
        OWN["gmail_create_draft"].handler,
        OWN["gmail_send_draft"].handler,
    )
    await create({"to": [USER], "subject": "Cut off", "body": "It was cut off while sending"})
    fingerprint = describe_draft(google.gmail.stored_drafts["d2"])[1]
    await _approve(engine, task_id, "d2", fingerprint)
    # The send reached Gmail, but the program was cut off before recording it as done.
    key = action_key(task_id, "send_email", USER, fingerprint)
    await engine.core.actions.start(key, task_id, "send_email", USER, {})
    google.gmail.sent.append({"id": "s99", "to": USER, "subject": "Cut off"})
    r = await send({"draft_id": "d2"})
    assert "it was already in Sent" in r["content"][0]["text"]
    assert not r.get("is_error")
    assert len(google.gmail.sent) == 1 and "d2" in google.gmail.stored_drafts
    assert (await engine.core.actions.get(key)).state == "done"


async def test_repeated_event_after_an_interruption_is_not_duplicated(environment):
    engine, _sim, google = environment
    task_id, _ses = await _direct_context(engine)
    create = OWN["calendar_create_event"].handler
    data = {"title": "Interview", "start": "2026-10-26T14:00", "end": "2026-10-26T15:00"}
    # Interruption: the event was created in Google but the action stayed "in_progress".
    r = await create(data)
    eid = next(iter(google.calendar.stored_events))
    await engine.db.run(
        "UPDATE external_actions SET state='in_progress' WHERE task_id=?", (task_id,)
    )
    r = await create(data)
    assert not r.get("is_error") and eid in r["content"][0]["text"]
    assert list(google.calendar.stored_events) == [eid]  # the 409 is treated as "already exists"
    # With guests and without approval, the tool refuses even if called directly.
    r = await create({**data, "guests": ["contact@example.com"]})
    assert r.get("is_error") and "approval" in r["content"][0]["text"]
    # Modifying and cancelling also require approval.
    assert (await OWN["calendar_modify_event"].handler({"id": eid, "title": "x"})).get("is_error")
    assert (await OWN["calendar_cancel_event"].handler({"id": eid})).get("is_error")
    assert google.calendar.stored_events[eid]["summary"] == "Interview"


async def test_expiry_pauses_only_google_tasks_and_resumes_them(environment):
    engine, sim, google = environment
    google.expired = True

    def script(prompt, options, registry):
        if "the Google authorization had expired" in prompt:
            return [
                own("gmail_search", {"query": "application"}),
                End("<delivery>Resumed.</delivery>"),
            ]
        if "without google" in prompt:
            return [End("<delivery>Done without Google.</delivery>")]
        return [
            own("gmail_search", {"query": "application"}),
            End("<delivery>It should not get here.</delivery>"),
        ]

    FakeClient.script = script
    [t_1] = await sim.send("Check my emails about job applications")
    [t_2] = await sim.send("See whether TechNova has replied to me")
    [t_3] = await sim.send("Do a task without google")

    for t in (t_1, t_2):
        task = await engine.tasks.require(t)
        assert task.state == TaskState.PAUSED_LOGIN and task.pause_reason == "google"
    assert (await engine.tasks.require(t_3)).state == TaskState.COMPLETED
    texts = sim.channel.texts()
    notices = [t for t in texts if LINK in t]
    assert len(notices) == 1 and "has expired" in notices[0]  # the link is sent only once
    assert any(f"Task {t_2} is waiting for you to renew" in t for t in texts)
    assert "ERROR" in results("gmail_search")[0]

    # The user opens the link and authorizes: both tasks resume.
    google.expired = False
    resumed = await engine.reauthorized_google(USER)
    await sim.wait_rest()
    assert sorted(resumed) == [t_1, t_2]
    for t in (t_1, t_2):
        assert (await engine.tasks.require(t)).state == TaskState.COMPLETED
    assert any("Google authorization renewed" in t for t in sim.channel.texts())
    assert "Your application" in results("gmail_search")[-1]
    assert await engine.db.kv_read("google_notice_sent") is None


async def test_preventive_notice_the_day_before(environment):
    """The day before it expires, from 9:00, with the time and the reminder from /google; no
    link or model. Not before 9:00, and not once expired."""
    engine, sim, google = environment
    expires = datetime(2026, 10, 27, 12, 0, tzinfo=BERLIN)
    google.expires_in = lambda: expires

    def notices() -> list[str]:
        return [
            t
            for t in sim.channel.texts()
            if "the Google authorization (Gmail and Calendar) expires" in t
        ]

    await engine.check_google_expiry(datetime(2026, 10, 25, 16, 0, tzinfo=BERLIN))
    await engine.check_google_expiry(datetime(2026, 10, 26, 8, 0, tzinfo=BERLIN))
    assert notices() == []  # two days before, and the day before before 9:00
    await engine.check_google_expiry(datetime(2026, 10, 26, 9, 5, tzinfo=BERLIN))
    await engine.check_google_expiry(datetime(2026, 10, 26, 10, 5, tzinfo=BERLIN))
    assert notices() == [
        "Tomorrow at 12:00 the Google authorization (Gmail and Calendar) expires. Type /google "
        "to renew it and I'll send you the link to open on the Mac."
    ]
    assert LINK not in notices()[0]
    # If there was no connection the day before, it goes out the same day, as long as it has not
    # expired.
    await engine.db.kv_delete("google_preventive_notice")
    await engine.check_google_expiry(datetime(2026, 10, 27, 13, 0, tzinfo=BERLIN))
    assert len(notices()) == 1
    await engine.check_google_expiry(datetime(2026, 10, 27, 7, 30, tzinfo=BERLIN))
    assert notices()[-1].startswith("Today at 12:00 the Google authorization")


async def test_search_omits_drafts_unless_requested(environment):
    engine, _sim, google = environment
    await _direct_context(engine)
    google.gmail.add_message(USER, "Loose draft", "not sent", labels=("DRAFT",))
    google.gmail.add_message(USER, "Sent by me", "already sent", labels=("SENT",))
    search = OWN["gmail_search"].handler
    r = (await search({"query": "newer_than:7d"}))["content"][0]["text"]
    assert "Loose draft" not in r and "1 drafts were left out" in r
    assert '"kind": "received"' in r and '"kind": "sent"' in r
    r = (await search({"query": "in:drafts"}))["content"][0]["text"]
    assert "Loose draft" in r and '"kind": "draft"' in r


# --- from test_google_permissions ---------------------------------------------

SRC = Path(__file__).resolve().parents[1] / "src" / "aide"


# Closed list: any new scope makes the test fail.
ALLOWED = {
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
}


# Gmail scopes that allow deleting, moving to Trash, changing labels, inserting emails or creating
# filters and forwards (a filter can delete or archive the incoming email). They are defined here
# and not in the program so that a change in the program cannot lower the test.
DANGEROUS = {
    "https://mail.google.com/",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.labels",
    "https://www.googleapis.com/auth/gmail.insert",
    "https://www.googleapis.com/auth/gmail.settings.basic",
    "https://www.googleapis.com/auth/gmail.settings.sharing",
}


# Closed list of Gmail tools: a new one forces a review of this test.
GMAIL_TOOLS = {
    "gmail_search",
    "gmail_read",
    "gmail_thread",
    "gmail_list_drafts",
    "gmail_create_draft",
    "gmail_send_draft",
    "gmail_send_drafts",
}


# Gmail API calls that delete, move to Trash or modify emails, threads or drafts, and the label and
# settings resources (filters, forwards).
RE_FORBIDDEN_CALL = re.compile(
    r"\b(messages|threads|drafts)\(\)\s*\.\s*"
    r"(trash|untrash|delete|modify|batchDelete|batchModify|insert|import_)\s*\("
)


RE_FORBIDDEN_RESOURCE = re.compile(r"\.\s*(labels|settings)\(\)")


# The full-access scope is the root https://mail.google.com/; links to the Gmail web
# (https://mail.google.com/mail/...) are not scopes and are allowed.
RE_FORBIDDEN_SCOPE = re.compile(
    r"https://mail\.google\.com/(?!mail/)|auth/gmail\.(modify|labels|insert|settings)"
)


RE_DELETED_NAME = re.compile(
    r"borr(ar|a_)|elimin|papeler|trash|delet|suprim|archivar|archive|mover|move|etiquet|label|filtr|filter",
    re.IGNORECASE,
)


RE_TOPIC_EMAIL = re.compile(r"correo|gmail|mail|bandeja", re.IGNORECASE)


def test_exact_scopes():
    assert set(SCOPES) == ALLOWED
    assert len(SCOPES) == len(ALLOWED)


def test_no_scope_allows_deleting_or_moving_email():
    assert not set(SCOPES) & DANGEROUS


@pytest.mark.parametrize("extra", sorted(DANGEROUS))
def test_extra_permissions_are_rejected(extra):
    check_permissions(ALLOWED)  # just enough: correct
    with pytest.raises(ExcessivePermissions):
        check_permissions(ALLOWED | {extra})


def test_google_account_comes_from_env(config_tmp):
    from aide.common.config import Config

    assert config_tmp.settings.google.account is None  # settings.yaml holds no personal data
    config_tmp.paths.env.write_text("GOOGLE_ACCOUNT=user@example.com\n")
    config = Config.load(config_tmp.paths)
    assert config.google_account == "user@example.com"
    config.settings.google.account = "old@example.com"
    assert config.google_account == "user@example.com"  # .env manda
    config_tmp.paths.env.write_text("GOOGLE_ACCOUNT=\n")
    assert Config.load(config_tmp.paths).google_account is None


def test_an_account_different_from_the_env_one_is_not_saved(config_tmp, monkeypatch):
    from aide.common.config import Config

    config_tmp.paths.env.write_text("GOOGLE_ACCOUNT=user@example.com\n")
    config = Config.load(config_tmp.paths)
    saved = []
    monkeypatch.setattr(google_auth.keyring, "set_password", lambda *a: saved.append(a))
    monkeypatch.setattr(
        google_auth.keyring, "get_password", lambda *a: None
    )  # not the real keychain

    def authorize(email):
        auth = GoogleAuthorization(config)
        monkeypatch.setattr(auth, "authorized_email", lambda creds: email)
        creds = SimpleNamespace(
            granted_scopes=list(SCOPES), token="t", refresh_token="r", to_json=lambda: "{}"
        )
        auth._flows["state"] = SimpleNamespace(fetch_token=lambda code: None, credentials=creds)
        return auth.complete_web("state", "code")

    with pytest.raises(google_auth.WrongAccount):
        authorize("other@example.com")
    assert saved == []
    assert authorize("User@Example.com") == "User@Example.com"  # case-insensitive
    assert len(saved) == 1


def test_new_token_with_extra_permissions_is_not_saved(config_tmp, monkeypatch):
    saved = []
    monkeypatch.setattr(google_auth.keyring, "set_password", lambda *a: saved.append(a))
    auth = GoogleAuthorization(config_tmp)
    monkeypatch.setattr(auth, "authorized_email", lambda creds: "user@example.com")
    creds = SimpleNamespace(
        granted_scopes=[*SCOPES, "https://www.googleapis.com/auth/gmail.modify"],
        token="t",
        refresh_token="r",
    )
    auth._flows["state"] = SimpleNamespace(fetch_token=lambda code: None, credentials=creds)
    with pytest.raises(ExcessivePermissions):
        auth.complete_web("state", "code")
    assert saved == []


def test_renewal_with_extra_permissions_is_rejected(config_tmp, monkeypatch):
    info = {
        "refresh_token": "r",
        "client_id": "c",
        "client_secret": "s",
        "token_uri": "https://oauth2.googleapis.com/token",
        "scopes": SCOPES,
    }
    monkeypatch.setattr(google_auth.keyring, "get_password", lambda *a: json.dumps(info))
    saved = []
    monkeypatch.setattr(google_auth.keyring, "set_password", lambda *a: saved.append(a))

    def renew(self, request):
        self.token = "nuevo"
        self.expiry = datetime.now(UTC).replace(tzinfo=None) + timedelta(hours=1)
        self._granted_scopes = [*SCOPES, "https://mail.google.com/"]

    monkeypatch.setattr(Credentials, "refresh", renew)
    auth = GoogleAuthorization(config_tmp)
    with pytest.raises(ExcessivePermissions):
        auth.credentials()
    assert saved == [] and auth._creds is None


def test_reauthorization_link_asks_only_for_the_scopes_and_does_not_inherit(config_tmp):
    auth = GoogleAuthorization(config_tmp)
    auth.credentials_path.parent.mkdir(parents=True, exist_ok=True)
    auth.credentials_path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "x.apps.googleusercontent.com",
                    "client_secret": "s",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "redirect_uris": ["http://localhost"],
                }
            }
        )
    )
    url = auth.start_web("http://127.0.0.1:8765/google/callback")
    query = parse_qs(urlparse(url).query)
    assert set(query["scope"][0].split()) == ALLOWED
    assert "include_granted_scopes" not in query


async def test_email_tools_without_deletion(config_tmp):
    from aide.tools.server import tools_of

    engine = Engine(config_tmp)
    await engine.start()
    try:
        engine.core.services["google"] = FakeAuthorization()
        task = await engine.tasks.create("test", state=TaskState.RUNNING)
        tools = tools_of(engine._context(task, "run"))
    finally:
        await engine.stop()
    names = {h.name for h in tools}
    assert {n for n in names if n.startswith("gmail_")} == GMAIL_TOOLS
    for h in tools:
        if h.name.startswith("gmail_") or RE_TOPIC_EMAIL.search(h.name + " " + h.description):
            assert not RE_DELETED_NAME.search(h.name), f"herramienta de correo sospechosa: {h.name}"


def test_the_code_does_not_call_delete_move_or_label_email():
    violations = []
    for file in sorted(SRC.rglob("*.py")):
        text = file.read_text(encoding="utf-8")
        for regex in (RE_FORBIDDEN_CALL, RE_FORBIDDEN_RESOURCE, RE_FORBIDDEN_SCOPE):
            for m in regex.finditer(text):
                line = text.count("\n", 0, m.start()) + 1
                violations.append(f"{file.relative_to(SRC)}:{line}: {m.group(0)}")
    assert violations == []


def test_the_expressions_detect_what_they_should():
    # If the expressions stopped detecting these calls, the previous test would be useless.
    assert RE_FORBIDDEN_CALL.search('servicio.users().messages().trash(userId="me", id=x)')
    assert RE_FORBIDDEN_CALL.search(
        "servicio.users()\n    .drafts()\n    .delete(userId='me', id=x)"
    )
    assert RE_FORBIDDEN_CALL.search("g.threads().modify(userId='me', id=x, body={})")
    assert RE_FORBIDDEN_RESOURCE.search("servicio.users().settings().filters().create(...)")
    assert RE_FORBIDDEN_SCOPE.search('"https://www.googleapis.com/auth/gmail.modify"')
    assert RE_FORBIDDEN_SCOPE.search('SCOPES = ["https://mail.google.com/"]')
    assert RE_FORBIDDEN_SCOPE.search("'https://mail.google.com/'")
    assert not RE_FORBIDDEN_SCOPE.search('f"https://mail.google.com/mail/u/0/#all/{thread}"')
    assert not RE_FORBIDDEN_CALL.search("svc.events().delete(calendarId=c, eventId=e)")
    assert RE_DELETED_NAME.search("gmail_mover_a_papelera")
    assert RE_DELETED_NAME.search("correo_borrar")
    assert not RE_DELETED_NAME.search("gmail_create_draft")
