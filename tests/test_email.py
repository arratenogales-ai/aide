"""Gmail reading and important-email filter. Gmail is the in-memory fake and the
model is the simulated SDK: no network or real account.

Gmail draft format. A text/plain-only draft opens in the Gmail editor inside
<pre>: typewriter font and fixed breaks. Every draft must be multipart/alternative, with
text/plain in UTF-8 without forced breaks and text/html with normal paragraphs and
lists, no <pre>, no monospaced font or font styles and no visible markdown."""

from __future__ import annotations

import asyncio
import base64
import email
import re
import time
from email import policy
from email.message import EmailMessage
from typing import Any

import pytest
import yaml

from aide.bot.channel import Entry, FakeChannel
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.email import review, rules
from aide.email.rules import Email
from aide.tools.google_gmail import build_mime, compose_body
from tests.fake_google import FakeAuthorization
from tests.fake_sdk import End, FakeClient, Recorder

# --- from test_email ------------------------------------------------------

NOW = int(time.time())


@pytest.fixture
async def engine(config_tmp, monkeypatch):
    from aide.bot.commands import run_command

    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    from aide.common.config import EmailDomains

    config_tmp.settings.email.active = True
    config_tmp.settings.email.domains = EmailDomains(
        bank=["northbank.example"], insurance=["shieldsure.example"], official=["agency.example"]
    )
    m = Engine(config_tmp, channel=FakeChannel(print_out=False))
    await m.start(workers=False)
    m.commands = run_command
    m.core.services["google"] = FakeAuthorization()
    yield m
    await m.stop()


def gmail(engine: Engine) -> Any:
    return engine.core.services["google"].gmail


async def button(engine: Engine, datum: str) -> None:
    await engine.receive(Entry("button", 0, button_datum=datum))


async def since(engine: Engine, moment: int) -> None:
    """As if reading had been turned on at that moment and reviewed up to it."""
    await engine.db.kv_put(review.SINCE_KEY, moment)
    await engine.db.kv_put(review.START_KEY, moment)


def make_email(of: str, subject: str, start: str = "", bulk: bool = False) -> Email:
    name, _, address = of.rpartition(" <")
    return Email(
        id="x",
        from_name=name,
        from_address=(address or of).rstrip(">").lower(),
        subject=subject,
        start=start,
        bulk=bulk,
    )


DOMAINS = {
    "bank": ("northbank.example",),
    "insurance": ("shieldsure.example",),
    "official": ("agency.example",),
}


def test_email_rules():
    silence = {"news@shop.example": "silence"}
    # “Don't alert me about this” applies to the exact address, never to the domain.
    d = rules.decide(make_email("news@shop.example", "Deals"), silence, DOMAINS)
    assert d.action == "silence" and d.reason == "sender you muted"
    d = rules.decide(make_email("orders@shop.example", "Your appointment on Tuesday"), silence)
    assert d.action == "model" and d.category == "appointment"
    # Important domains: direct alert, except bulk mailings, which the model reviews.
    d = rules.decide(make_email("alerts@northbank.example", "Account statement"), {}, DOMAINS)
    assert (d.action, d.category, d.reason) == ("notice", "bank", "bank sender")
    d = rules.decide(make_email("news@northbank.example", "Newsletter", bulk=True), {}, DOMAINS)
    assert (d.action, d.category) == ("model", "bank")
    d = rules.decide(make_email("claims@shieldsure.example", "Your claim"), {}, DOMAINS)
    assert (d.action, d.category) == ("notice", "insurance")
    d = rules.decide(make_email("x@sub.agency.example", "Hi"), {}, DOMAINS)
    assert d.category == "official"
    # Without the domain list, none is important because of its domain.
    assert rules.decide(make_email("alerts@northbank.example", "Hi"), {}).action == "silence"
    # Government domains from any country are recognized automatically.
    for address in (
        "notice@irs.gov",
        "x@tax.service.gov.uk",
        "info@agency.gob.es",
        "contact@impots.gouv.fr",
        "a@ird.govt.nz",
    ):
        assert rules.decide(make_email(address, "Hi"), {}).category == "official", address
    for address in ("a@gov.example", "a@govexample.com", "a@mygov.uk"):
        assert rules.domain_category(address.split("@")[1]) is None, address
    # “Important” takes precedence, also by exact address, and travels with the decision.
    d = rules.decide(make_email("friend@mail.example", "Hi"), {"friend@mail.example": "important"})
    assert d.action == "notice" and d.preference == "important"
    assert rules.decide(make_email("other@mail.example", "Hi"), {}).action == "silence"
    # Invoices and payments go to the model, in several languages; everything else is silent.
    for subject in ("Invoice No. 12", "Your bill is ready", "Rechnung Nr. 12", "Factura 7"):
        assert rules.decide(make_email("a@b.example", subject), {}).category == "invoice", subject
    for subject in ("Meeting invitation", "Einladung", "Cita médica", "Rendez-vous lundi"):
        assert rules.decide(make_email("a@b.example", subject), {}).category == "appointment", (
            subject
        )
    assert rules.decide(make_email("a@b.example", "Hi"), {}).action == "silence"


def test_domains_match_exactly_or_with_their_subdomains():
    domains = {"bank": ("northbank.example",), "insurance": (), "official": ()}
    assert rules.domain_category("northbank.example", domains) == "bank"
    assert rules.domain_category("alerts.northbank.example", domains) == "bank"
    assert rules.domain_category("Alerts.NorthBank.example.", domains) == "bank"
    assert rules.domain_category("fakenorthbank.example", domains) is None
    assert rules.domain_category("northbank.example.evil.example", domains) is None
    # The category order is kept: bank before insurance.
    both = {"bank": ("both.example",), "insurance": ("both.example",), "official": ()}
    assert rules.domain_category("both.example", both) == "bank"


def test_domains_come_from_settings(config_tmp):
    from aide.common.config import Config

    # The settings.yaml example is loaded and validated.
    example = config_tmp.settings.email.domains.by_category()
    assert list(example) == ["bank", "insurance", "official"]
    assert all(example.values())
    assert all(
        d == d.lower() and "@" not in d and "/" not in d for v in example.values() for d in v
    )
    # A custom one, with capitals and a trailing period, ends up lowercase and without repeats.
    path = config_tmp.paths.config / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["email"]["domains"] = {"bank": ["NorthBank.example.", "northbank.example"]}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    config = Config.load(config_tmp.paths)
    assert config.settings.email.domains.by_category() == {
        "bank": ("northbank.example",),
        "insurance": (),
        "official": (),
    }
    # Without the section, the lists are empty.
    del data["email"]["domains"]
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    assert not any(Config.load(config_tmp.paths).settings.email.domains.by_category().values())


@pytest.mark.parametrize(
    "value,message",
    [
        (["alerts@northbank.example"], "looks like an email address"),
        (["https://northbank.example"], "looks like a web address"),
        (["northbank.example/login"], "looks like a web address"),
        (["north bank.example"], "is not a valid domain"),
        (["localhost"], "is not a valid domain"),
        ([""], "empty domain"),
        ([12], "is not a domain"),
        ("northbank.example", "write a list of domains"),
    ],
)
def test_a_misspelled_domain_prevents_startup(config_tmp, value, message):
    from aide.common.config import Config, ConfigError

    path = config_tmp.paths.config / "settings.yaml"
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data["email"]["domains"] = {"bank": value}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        Config.load(config_tmp.paths)
    # A category that does not exist, neither.
    data["email"]["domains"] = {"bancos": ["northbank.example"]}
    path.write_text(yaml.safe_dump(data, allow_unicode=True), encoding="utf-8")
    with pytest.raises(ConfigError):
        Config.load(config_tmp.paths)


def test_email_headers():
    c = rules.from_headers(
        "m1",
        "t1",
        {
            "from": '"Technova HR" <Jobs@Technova.example>',
            "subject": " Your application ",
            "list-unsubscribe": "<mailto:x>",
        },
        "Good  morning\n\nMs Example",
    )
    assert (c.from_name, c.from_address, c.domain) == (
        "Technova HR",
        "jobs@technova.example",
        "technova.example",
    )
    assert c.subject == "Your application" and c.start == "Good morning Ms Example" and c.bulk


def model_replies(by_subject: dict[str, dict[str, Any]]):
    """SDK script: what the model says about each email, by its subject."""

    def script(prompt, options, registry_):
        assert "You classify the user's new emails" in options.system_prompt
        assert options.tools == []
        emails = []
        for n, subject in re.findall(r"#(\d+) · .*?\nSubject: (.*)", prompt):
            r = by_subject.get(subject)
            if r is not None:
                emails.append({"n": int(n), **r})
        return [End(structured={"emails": emails})]

    return script


def model(category: str, important: bool = False, summary: str = "") -> dict[str, Any]:
    return {"category": category, "important": important, "summary": summary}


async def test_gmail_review(engine):
    g = gmail(engine)
    channel = engine.channel
    db = engine.db
    g.stored_messages.clear()
    # The first time it only records the moment: nothing from the history.
    g.add_message("alerts@northbank.example", "Old", "Old one", date=NOW - 86400)
    assert await review.review(engine) == {"seen": 0}
    assert channel.messages == [] and FakeClient.registry.queries == []
    await since(engine, NOW - 3600)
    bank = g.add_message(
        "Northbank <alerts@northbank.example>", "Your invoice", "Amount", date=NOW - 3000
    )
    g.add_message(
        "Example Clinic <appointments@clinic.example>",
        "Your appointment on Monday",
        "We confirm your appointment at 10 a.m.",
        date=NOW - 2500,
    )
    g.add_message(
        "Fashion <news@clothes-shop.example>",
        "Black Friday invitation",
        "50%",
        date=NOW - 2000,
        headers={"List-Unsubscribe": "<mailto:unsubscribe@clothes-shop.example>"},
    )
    g.add_message("x@unknown.example", "Hi", "How are you", date=NOW - 1500)
    g.add_message("me@mail.example", "Sent", "x", ("SENT",), date=NOW - 900)
    labels = {i: list(m["labelIds"]) for i, m in g.stored_messages.items()}
    FakeClient.script = staticmethod(
        model_replies(
            {
                "Your appointment on Monday": model(
                    "appointment", True, "Appointment on Monday at 10"
                ),
                "Black Friday invitation": model("other"),
            }
        )
    )
    account = await review.review(engine)
    assert account == {"seen": 4, "notices": 2}
    # The model only sees the doubtful ones, with the sender, subject and beginning, in one
    # batch.
    classification = [c for c in FakeClient.registry.queries if "Emails:" in c]
    assert len(classification) == 1
    assert await db.value("SELECT COUNT(*) FROM usage WHERE origin='email'") == 1
    assert "Black Friday" in classification[0] and "Your appointment" in classification[0]
    assert not any("Your invoice" in c or "How are you" in c for c in classification)
    texts = [m.plain_text for m in channel.messages]
    # The bank: direct alert with its per-address buttons.
    notice = next(m for m in channel.messages if "Your invoice" in m.plain_text)
    assert notice.plain_text.startswith("Important email (bank) from Northbank")
    assert notice.buttons_data == [f"email:imp:{bank}", f"email:sil:{bank}"]
    assert "mail.google.com/mail/u/0/#all/" in notice.text
    appointment = next(m for m in channel.messages if "Your appointment" in m.plain_text)
    assert "Important email (appointment or invitation)" in appointment.plain_text
    assert "Appointment on Monday at 10." in appointment.plain_text
    assert "Open in Gmail" in appointment.plain_text
    assert not any("Black Friday" in t or "How are you" in t or "Sent" in t for t in texts)
    decisions = {
        f["subject"]: f["decision"]
        for f in await db.everything("SELECT subject, decision FROM emails")
    }
    assert decisions == {
        "Your invoice": "notice",
        "Your appointment on Monday": "notice",
        "Black Friday invitation": "silence",
        "Hi": "silence",
    }
    # The mark advances and what was already seen is not repeated.
    assert await db.kv_read(review.SINCE_KEY) == NOW - 1500
    total = len(channel.messages)
    assert await review.review(engine) == {"seen": 0}
    assert len(channel.messages) == total
    # It never deletes, archives or flags: the emails stay as they were.
    assert {i: m["labelIds"] for i, m in g.stored_messages.items()} == labels


async def test_dont_alert_me_by_exact_address(engine):
    g = gmail(engine)
    channel = engine.channel
    db = engine.db
    g.stored_messages.clear()
    await since(engine, NOW - 3600)
    bank = g.add_message("alerts@northbank.example", "Account statement", "x", date=NOW - 3000)
    FakeClient.script = staticmethod(model_replies({}))
    await review.review(engine)
    await button(engine, f"email:sil:{bank}")
    assert await db.everything("SELECT address, preference FROM senders") == [
        {"address": "alerts@northbank.example", "preference": "silence"}
    ]
    edited = channel.messages[-1]
    assert edited.kind == "edit"
    assert "I won't alert you about alerts@northbank.example" in edited.plain_text
    # Another from that address: silent; from another address on the same domain: alert.
    g.add_message("alerts@northbank.example", "Account statement 2", "x", date=NOW - 2000)
    g.add_message("cards@northbank.example", "Card statement", "x", date=NOW - 1900)
    before = len(channel.messages)
    assert await review.review(engine) == {"seen": 2, "notices": 1}
    new = [m.plain_text for m in channel.messages[before:]]
    assert not any("Account statement 2" in t for t in new)
    assert any("Card statement" in t for t in new)
    # “Important” changes the preference for that address.
    await button(engine, f"email:imp:{bank}")
    assert await db.value(
        "SELECT preference FROM senders WHERE address='alerts@northbank.example'"
    ) == ("important")


async def test_if_the_model_does_not_respond(engine):
    g = gmail(engine)
    channel = engine.channel
    db = engine.db
    g.stored_messages.clear()
    await since(engine, NOW - 30 * 3600)
    recent = g.add_message("a@b.example", "Appointment on Monday", "x", date=NOW - 600)
    old = g.add_message("c@d.example", "Invoice 7", "x", date=NOW - 7 * 3600)

    def fails(prompt, options, registry_):
        return [End("I can't.")]  # no structured output, as when the call fails

    FakeClient.script = staticmethod(fails)
    account = await review.review(engine)
    # The recent one waits for later; the one unclassified for hours is alerted now.
    assert account == {"seen": 2, "notices": 1}
    row = await db.one("SELECT * FROM emails WHERE id=?", (recent,))
    assert (row["decision"], row["attempts"]) == ("model_pending", 1)
    assert row["next_attempt"] > time.time() + 200
    assert await db.value("SELECT decision FROM emails WHERE id=?", (old,)) == "notice"
    assert await db.kv_read(review.SINCE_KEY) == NOW - 600
    # Before its wait is over it is not retried: no spending in a loop.
    calls = len(FakeClient.registry.queries)
    assert await review.review(engine) == {"seen": 0}
    assert len(FakeClient.registry.queries) == calls
    # When its turn comes and the model is back, it is classified (alone, in its own call).
    await db.run("UPDATE emails SET next_attempt=0 WHERE id=?", (recent,))
    FakeClient.script = staticmethod(
        model_replies(
            {"Appointment on Monday": model("appointment", True, summary="Appointment on Monday")}
        )
    )
    assert await review.review(engine) == {"seen": 1, "notices": 1}
    assert "Appointment on Monday." in channel.messages[-1].plain_text
    assert await db.value("SELECT decision FROM emails WHERE id=?", (recent,)) == "notice"


async def test_a_down_model_has_an_attempt_cap(engine):
    g = gmail(engine)
    db = engine.db
    g.stored_messages.clear()
    await since(engine, NOW - 3600)
    other = g.add_message("a@b.example", "Invoice 5", "x", date=NOW - 600)
    FakeClient.script = staticmethod(lambda p, o, r: [End("I can't.")])
    results = []
    for _ in range(6):
        results.append(await review.review(engine))
        await db.run("UPDATE emails SET next_attempt=0 WHERE id=?", (other,))
    # At most four attempts; on the fourth it is alerted anyway, unclassified.
    assert len(FakeClient.registry.queries) == review.MAX_ATTEMPTS
    assert await db.value("SELECT decision FROM emails WHERE id=?", (other,)) == "notice"
    assert "Important email (invoice or payment)" in engine.channel.messages[-1].plain_text
    assert results[-1] == {"seen": 0}


async def test_alerts_telegram_does_not_deliver_are_retried(engine, monkeypatch):
    g = gmail(engine)
    channel = engine.channel
    db = engine.db
    g.stored_messages.clear()
    await since(engine, NOW - 3600)
    bank = g.add_message("alerts@northbank.example", "Account statement", "x", date=NOW - 600)
    original = channel.send

    async def down(text, **kwargs):
        return None

    monkeypatch.setattr(channel, "send", down)
    assert await review.review(engine) == {"seen": 1, "notices": 1}
    # It did not arrive: it is not counted as alerted and stays for retry.
    assert await db.value("SELECT COUNT(*) FROM pending_notices") == 1
    assert await db.value("SELECT message_id FROM emails WHERE id=?", (bank,)) is None
    assert await review.review(engine) == {"seen": 0}
    assert await db.value("SELECT attempts FROM pending_notices") == 1
    # Telegram is back: the alert arrives, with its buttons, and the buttons find their message.
    monkeypatch.setattr(channel, "send", original)
    await review.review(engine)
    assert await db.value("SELECT COUNT(*) FROM pending_notices") == 0
    notice = channel.messages[-1]
    assert "Account statement" in notice.plain_text
    assert notice.buttons_data == [f"email:imp:{bank}", f"email:sil:{bank}"]
    assert await db.value("SELECT message_id FROM emails WHERE id=?", (bank,)) == notice.id


async def test_what_is_flagged_overrides_the_model(engine):
    g = gmail(engine)
    channel = engine.channel
    db = engine.db
    g.stored_messages.clear()
    await since(engine, NOW - 3600)
    await db.run(
        "INSERT INTO senders(address, preference, since) VALUES "
        "('news@mercadix.example', 'silence', '2026-10-23T00:00:00+00:00'), "
        "('info@tiendix.example', 'important', '2026-10-23T00:00:00+00:00')"
    )
    g.add_message("Mercadix <news@mercadix.example>", "Deals of the week", "x", date=NOW - 600)
    g.add_message("Tiendix <info@tiendix.example>", "News", "x", date=NOW - 500)
    FakeClient.script = staticmethod(
        model_replies(
            {
                "Deals of the week": model("other", important=True),
                "News": model("other", important=False),
            }
        )
    )
    assert await review.review(engine) == {"seen": 2, "notices": 1}
    texts = [m.plain_text for m in channel.messages]
    assert not any("Deals" in x for x in texts)
    assert any("News" in x for x in texts)


async def test_review_off_or_without_google(engine):
    engine.config.settings.email.active = False
    assert await review.review(engine) == {}
    engine.config.settings.email.active = True
    del engine.core.services["google"]
    assert await review.review(engine) == {}


async def test_many_new_emails_are_not_lost(engine, monkeypatch):
    g = gmail(engine)
    db = engine.db
    g.stored_messages.clear()
    monkeypatch.setattr(review, "MAX_PER_PAGE", 50)
    monkeypatch.setattr(review, "MAX_PER_REVIEW", 50)
    await since(engine, NOW - 3600)
    for i in range(120):
        g.add_message(f"n{i}@unknown.example", f"Hi {i}", "x", date=NOW - 3000 + i)
    # They are read in pages and the oldest are reviewed first; the rest, in the following ones.
    assert (await review.review(engine))["seen"] == 50
    assert len(g.listed) == 3 and all("after:" in q and "-in:spam" in q for q in g.listed)
    subjects = {f["subject"] for f in await db.everything("SELECT subject FROM emails")}
    assert subjects == {f"Hi {i}" for i in range(50)}
    assert (await review.review(engine))["seen"] == 50
    assert (await review.review(engine))["seen"] == 20
    assert await db.value("SELECT COUNT(*) FROM emails") == 120
    # An email that shows up late in the search (with an arrival before the mark) is seen thanks
    # to the day of margin.
    g.add_message("late@unknown.example", "Late", "x", date=NOW - 3500)
    assert (await review.review(engine))["seen"] == 1


async def test_engine_launches_the_review_only_if_active(engine, monkeypatch):
    calls: list[int] = []

    async def fake_review(m):
        calls.append(1)
        return {}

    monkeypatch.setattr(review, "review", fake_review)
    engine.config.settings.email.active = False
    await engine.check_all()
    await asyncio.sleep(0)
    assert calls == []
    engine.config.settings.email.active = True
    await engine.check_all()
    await asyncio.sleep(0)
    assert calls == [1]
    # It is not repeated until the minutes from the settings have passed.
    await engine.check_all()
    await asyncio.sleep(0.01)
    assert calls == [1]


async def test_the_first_checks_do_not_depend_on_how_long_the_mac_has_been_on(engine, monkeypatch):
    # The loop's clock counts from the boot: just after it, it is below every interval.
    loop = asyncio.get_running_loop()
    clock, boot = loop.time, loop.time()
    monkeypatch.setattr(loop, "time", lambda: clock() - boot + 1)
    calls: list[str] = []

    def fake(name):
        async def record(*args, **kwargs):
            calls.append(name)

        return record

    monkeypatch.setattr(review, "review", fake("email"))
    monkeypatch.setattr(engine, "check_pauses", fake("pauses"))
    monkeypatch.setattr(engine, "check_google_expiry", fake("expiry"))
    await engine.check_all()
    await asyncio.sleep(0)
    assert sorted(calls) == ["email", "expiry", "pauses"]


async def test_email_page(config_tmp, monkeypatch):
    import httpx

    from aide.panel.app import ADVANCED_MENU, COOKIE, Panel

    config_tmp.settings.email.active = False
    m = Engine(config_tmp, channel=FakeChannel(print_out=False))
    await m.start(workers=False)
    try:
        await m.db.run(
            "INSERT INTO senders(address, preference, since) VALUES "
            "('alerts@northbank.example', 'silence', '2026-10-23T08:00:00+00:00')"
        )
        await m.db.run(
            "INSERT INTO emails(id, thread, from_name, from_address, subject, category, decision, "
            "reason, reviewed) VALUES ('m1', 't_1', 'Northbank', 'alerts@northbank.example', "
            "'Account statement', 'bank', 'notice', 'bank sender', '2026-10-23T08:00:00+00:00')"
        )
        p = Panel(m, token="t")
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=p.app),
            base_url=f"http://127.0.0.1:{p.port}",
            follow_redirects=False,
        ) as client:
            r = await client.get("/enter?t=t&go=/")
            assert COOKIE in r.cookies
            assert (await client.get("/empleo")).status_code == 404
            # Email: in Advanced, with what it has learned and the latest alerts.
            assert ("/email", "Email") in ADVANCED_MENU
            html = (await client.get("/email")).text
            assert "Gmail reading is off" in html
            assert "alerts@northbank.example" in html and "Account statement" in html
            r = await client.post(
                "/email/senders/remove",
                data={"csrf": p.csrf, "address": "Alerts@NorthBank.example"},
            )
            assert r.status_code == 303
            assert await m.db.value("SELECT COUNT(*) FROM senders") == 0
    finally:
        await m.stop()


# --- from test_email_format ----------------------------------------------

USER = "user@example.com"


# A body like those the model writes when copying a ready-made text: lines broken at about 80
# characters, quote marks, bold text, a numbered list with split items, asterisk bullets, a heading,
# a link, a code fence and a two-line signature.
HARD_BODY = """> Hello,
>
> I organise a small community garden and we would like to rent two raised beds
> for the spring season, including the tools and the watering system that the
> municipal guidelines (sections 12 and following) require for shared plots in
> the city.
>
> **Are the raised beds still available in March, and can you send us the rental
> conditions?** Please confirm whether the **watering system and the
> tools** are included.

## Two questions

1. In that arrangement, who is the *holder* of the rental contract for the purposes
   of the insurance?
2. If I am the holder, when should a new member register?

* Renée, Jürgen and Niño are fine names.
* See [the FAQ](https://example.com/faq).

```
Tracking: 1Z999AA10123456784
```

Thank you,
Alex
"""


def _parts(raw: str) -> EmailMessage:
    return email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)


def format_problems(raw: str) -> list[str]:
    """What would make the draft look wrong in Gmail. Empty if it is fine."""
    message = _parts(raw)
    problems: list[str] = []
    alternatives = [p for p in message.walk() if p.get_content_type() == "multipart/alternative"]
    if not alternatives:
        return ["no es multipart/alternative"]
    children = {p.get_content_type(): p for p in alternatives[0].iter_parts()}
    if set(children) != {"text/plain", "text/html"}:
        return [f"multipart/alternative con {sorted(children)}"]
    for kind, part in children.items():
        if (part.get_content_charset() or "").lower() != "utf-8":
            problems.append(f"{kind} no va en UTF-8")
    plain, html_body = children["text/plain"].get_content(), children["text/html"].get_content()
    for pattern, that in (
        (r"<\s*(pre|code|tt|kbd|samp)\b", "etiqueta de texto preformateado o de código"),
        (r"monospace|courier|font-family|<\s*font\b", "fuente fijada o monoespaciada"),
        (r"white-space\s*:\s*pre", "white-space: pre"),
    ):
        if re.search(pattern, html_body, re.IGNORECASE):
            problems.append(f"html con {that}")
    html_text = re.sub(r"<[^>]+>", "", html_body)
    for name, text in (("text/plain", plain), ("text/html", html_text)):
        if "**" in text or "__" in text or "```" in text or "`" in text:
            problems.append(f"{name} con markdown sin convertir")
        if re.search(r"(?m)^\s*#{1,6}\s", text) or re.search(r"(?m)^\s*>\s?", text):
            problems.append(f"{name} con títulos o citas de markdown")
        if re.search(r"(?m)^\s*\*\s", text):
            problems.append(f"{name} con viñetas de asterisco")
    # Forced breaks: a long line that continues on the next one in lowercase.
    lines = plain.split("\n")
    for current, next_step in zip(lines, lines[1:], strict=False):
        if len(current) >= 60 and next_step[:1].islower():
            problems.append(f"text/plain con un salto forzado tras «{current[-20:]}»")
            break
    return problems


def test_a_draft_is_a_normal_email():
    raw = build_mime(USER, [USER], "Raised beds", HARD_BODY)
    assert format_problems(raw) == []
    message = _parts(raw)
    assert message.get_content_type() == "multipart/alternative"
    plain, html_body = compose_body(HARD_BODY)
    # One-line paragraphs; the signature keeps its break.
    assert (
        "I organise a small community garden and we would like to rent two raised beds for the "
        "spring season, including the tools and the watering system that the municipal "
        "guidelines (sections 12 and following) require for shared plots in the city.\n"
    ) in plain
    assert "Thank you,\nAlex" in plain and "<p>Thank you,<br>Alex</p>" in html_body
    # Normal lists, bold text and links converted, and the accents intact.
    assert (
        "1. In that arrangement, who is the holder of the rental contract for the purposes of the "
        "insurance?" in plain
    )
    assert "- Renée, Jürgen and Niño are fine names." in plain
    assert "<ol><li>In that arrangement" in html_body and "<ul><li>Renée" in html_body
    assert "<strong>watering system and the tools</strong>" in html_body
    assert '<a href="https://example.com/faq">the FAQ</a>' in html_body
    assert "the FAQ (https://example.com/faq)" in plain
    assert "<p><strong>Two questions</strong></p>" in html_body
    assert "<p>Tracking: 1Z999AA10123456784</p>" in html_body


def test_with_attachments_both_parts_stay_together(tmp_path):
    attachment = tmp_path / "letter.pdf"
    attachment.write_bytes(b"%PDF-1.4")
    raw = build_mime(
        USER, [USER], "Letter", "Hi,\n\nPlease find the letter attached.", attachments=[attachment]
    )
    message = _parts(raw)
    assert message.get_content_type() == "multipart/mixed"
    assert format_problems(raw) == []
    assert [p.get_filename() for p in message.iter_attachments()] == ["letter.pdf"]


def test_the_checker_detects_a_badly_made_draft():
    """The tests above would fail with the old draft (text/plain only) or with HTML that has
    <pre>, a monospaced font, markdown or forced breaks."""
    before = EmailMessage()
    before["To"] = USER
    before.set_content(HARD_BODY)
    raw_before = base64.urlsafe_b64encode(before.as_bytes()).decode()
    assert format_problems(raw_before) == ["no es multipart/alternative"]

    bad = EmailMessage()
    bad["To"] = USER
    bad.set_content(
        "**Hello**\n## Title\nThis line is long and it is broken by hand in the middle of a\n"
        "sentence that goes on here in lowercase."
    )
    bad.add_alternative(
        '<pre style="font-family:monospace">**Hello**</pre>', subtype="html", charset="utf-8"
    )
    problems = format_problems(base64.urlsafe_b64encode(bad.as_bytes()).decode())
    for part in ("etiqueta de texto preformateado", "monoespaciada", "markdown sin convertir"):
        assert any(part in p for p in problems), part
    assert any("títulos o citas" in p for p in problems)
    assert any("salto forzado" in p for p in problems)
