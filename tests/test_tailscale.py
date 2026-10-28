"""The panel on the iPhone through Tailscale: only on the user's network, never with
Funnel, and always with the entry link."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from aide.bot.channel import Entry, FakeChannel
from aide.bot.commands import run_command
from aide.common import tailscale
from aide.common.config import Environment
from aide.common.tailscale import ServeState, analyze_serve
from aide.core.engine import Engine
from aide.core.orchestrator import AgentSession
from aide.maintenance import security
from aide.panel.app import COOKIE, Panel
from tests.fake_sdk import FakeClient, Recorder

TOKEN = "token-de-prueba"
NAME = "aide.tail0000.ts.net"
MOBILE = f"https://{NAME}"

# What `tailscale serve status --json` returns with the panel published and Funnel off.
SERVE = {
    "TCP": {"443": {"HTTPS": True}},
    "Web": {f"{NAME}:443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:8765"}}}},
}
WITH_FUNNEL = {**SERVE, "AllowFunnel": {f"{NAME}:443": True}}


def test_reads_the_serve_state():
    empty = analyze_serve("{}")
    assert empty.funnel is False and empty.safe and empty.destinations == []
    published = analyze_serve(json.dumps(SERVE))
    assert published.safe and published.destinations == ["http://127.0.0.1:8765"]
    assert analyze_serve(json.dumps(WITH_FUNNEL)).funnel is True
    # Also in a foreground configuration (`tailscale funnel 8765` without --bg).
    first_plain = {"Foreground": {"session": {"AllowFunnel": {f"{NAME}:443": True}}}}
    assert analyze_serve(json.dumps(first_plain)).funnel is True
    assert analyze_serve(json.dumps({"AllowFunnel": {f"{NAME}:443": False}})).safe
    # What is not understood does not count as safe.
    for rare in ("", "no es json", "[]", "null"):
        assert analyze_serve(rare).funnel is None
        assert not analyze_serve(rare).safe


def test_without_the_tailscale_command_it_is_not_safe(monkeypatch):
    monkeypatch.setattr(tailscale, "search_tailscale", lambda: None)
    state = tailscale.read_serve()
    assert state.funnel is None and not state.safe and state.error


def test_the_tailscale_name_is_validated():
    assert Environment(PANEL_TAILSCALE=" Aide.Tail0000.TS.net. ").PANEL_TAILSCALE == NAME
    assert Environment(PANEL_TAILSCALE="").PANEL_TAILSCALE is None
    assert Environment().PANEL_TAILSCALE is None
    for bad in ("0.0.0.0", "100.101.102.103", "evil.example", "x.ts.net", "a.b.ts.net:443"):
        with pytest.raises(ValidationError):
            Environment(PANEL_TAILSCALE=bad)


@pytest.fixture
async def panel(config_tmp, monkeypatch):
    FakeClient.registry = Recorder()
    monkeypatch.setattr(AgentSession, "client_factory", FakeClient)
    config_tmp.environment = config_tmp.environment.model_copy(update={"PANEL_TAILSCALE": NAME})
    engine = Engine(config_tmp, channel=FakeChannel(print_out=False))
    await engine.start(workers=False)
    engine.commands = run_command
    p = Panel(engine, token=TOKEN)
    p.read_serve = lambda: analyze_serve(json.dumps(SERVE))
    yield engine, p
    await engine.stop()


def _client(p: Panel, base: str) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=p.app), base_url=base, follow_redirects=False
    )


async def test_the_iphone_enters_through_tailscale_only_with_the_link(panel):
    engine, p = panel
    # Before checking Funnel, the Tailscale address is not valid.
    async with _client(p, MOBILE) as mobile:
        assert (await mobile.get("/style.css")).status_code == 400
    await p.check_tailscale()
    assert p.safe_tailscale
    async with _client(p, MOBILE) as mobile:
        # On the network, but without the link, it does not get in.
        assert (await mobile.get("/")).status_code == 401
        assert (await mobile.get("/enter?t=otro")).status_code == 403
        r = await mobile.get(f"/enter?t={TOKEN}&go=/")
        assert r.status_code == 200
        # It passes to the destination from a page of the panel itself, not with a redirect.
        assert 'http-equiv="refresh" content="0;url=/"' in r.text
        assert r.headers["cache-control"] == "no-store"
        cookie = r.headers["set-cookie"].lower()
        assert "secure" in cookie and "httponly" in cookie and "samesite=strict" in cookie
        assert (await mobile.get("/")).status_code == 200
        # Forms, only with their own origin.
        r = await mobile.post("/scheduled", data={"csrf": p.csrf}, headers={"Origin": MOBILE})
        assert r.status_code != 403
        for origin in (f"http://{NAME}", "http://127.0.0.1:8765", "https://evil.example"):
            r = await mobile.post("/scheduled", data={"csrf": p.csrf}, headers={"Origin": origin})
            assert r.status_code == 403
    # Another network name (or another device) is not valid.
    async with _client(p, "https://otro.tail0000.ts.net") as other:
        assert (await other.get("/style.css")).status_code == 400
    # On the Mac, everything stays the same and the cookie does not carry Secure (it goes over
    # http).
    async with _client(p, f"http://127.0.0.1:{p.port}") as mac:
        r = await mac.get(f"/enter?t={TOKEN}&go=/")
        assert r.status_code == 200 and "secure" not in r.headers["set-cookie"].lower()
        assert COOKIE in r.cookies
        r = await mac.post("/scheduled", data={"csrf": p.csrf}, headers={"Origin": MOBILE})
        assert r.status_code == 403


async def test_with_funnel_on_it_is_blocked_and_warns_once(panel):
    engine, p = panel
    await p.check_tailscale()
    async with _client(p, MOBILE) as mobile:
        assert (await mobile.get(f"/enter?t={TOKEN}&go=/")).status_code == 200
        p.read_serve = lambda: analyze_serve(json.dumps(WITH_FUNNEL))
        await p.check_tailscale()
        await p.check_tailscale()
        # Not even with the cookie already set.
        assert (await mobile.get("/")).status_code == 400
    notices = [m.text for m in engine.channel.messages if "Funnel" in m.text]
    assert len(notices) == 1 and "iPhone" in notices[0]
    # The Mac keeps working.
    async with _client(p, f"http://127.0.0.1:{p.port}") as mac:
        assert (await mac.get(f"/enter?t={TOKEN}&go=/")).status_code == 200
    # Turned off again, it is valid again; if turned on again, it warns again.
    p.read_serve = lambda: analyze_serve(json.dumps(SERVE))
    await p.check_tailscale()
    assert p.safe_tailscale
    p.read_serve = lambda: analyze_serve(json.dumps(WITH_FUNNEL))
    await p.check_tailscale()
    assert len([m for m in engine.channel.messages if "Funnel" in m.text]) == 2


async def test_if_it_cannot_be_known_it_is_blocked_without_warning(panel):
    engine, p = panel
    await p.check_tailscale()
    p.read_serve = lambda: ServeState(None, error="tailscale did not respond")
    await p.check_tailscale()
    assert not p.safe_tailscale
    async with _client(p, MOBILE) as mobile:
        assert (await mobile.get("/style.css")).status_code == 400
    assert not [m for m in engine.channel.messages if "Funnel" in m.text]


async def test_without_a_tailscale_panel_the_address_is_not_valid(config_tmp, monkeypatch):
    engine = Engine(config_tmp, channel=FakeChannel(print_out=False))
    await engine.start(workers=False)
    p = Panel(engine, token=TOKEN)
    p.read_serve = lambda: analyze_serve(json.dumps(SERVE))
    await p.check_tailscale()
    assert p.mobile_link() is None
    async with _client(p, MOBILE) as mobile:
        assert (await mobile.get("/style.css")).status_code == 400
    await engine.stop()


async def test_the_panel_command_gives_both_links(panel):
    engine, p = panel
    engine.core.services["panel_link"] = p.link
    engine.core.services["link_panel_mobile"] = p.mobile_link
    await run_command(engine, Entry("command", 0, text="/panel", command="panel"))
    text = engine.channel.messages[-1].text
    assert f"http://127.0.0.1:{p.port}/enter?t={TOKEN}" in text
    assert f"{MOBILE}/enter?t={TOKEN}" in text and "iPhone" in text
    engine.core.services["link_panel_mobile"] = lambda: None
    await run_command(engine, Entry("command", 0, text="/panel", command="panel"))
    assert "iPhone" not in engine.channel.messages[-1].text


async def test_the_weekly_report_checks_funnel(panel, monkeypatch, tmp_path):
    engine, _ = panel
    monkeypatch.setattr(security, "PLIST", tmp_path / "no-existe.plist")
    monkeypatch.setattr(security, "audit_dependencies", lambda root: ([], None))
    monkeypatch.setattr(security, "process_listeners", lambda port, pid=None: ["127.0.0.1"])

    async def review(state: ServeState):
        monkeypatch.setattr(security, "read_serve", lambda: state)
        return await security.review(engine)

    s = await review(analyze_serve(json.dumps(SERVE)))
    assert any("Funnel is off" in line for line in s.lines)
    s = await review(analyze_serve(json.dumps(WITH_FUNNEL)))
    assert not s.ok and any("Funnel is on" in line for line in s.lines)
    s = await review(ServeState(None, error="can't find the tailscale command"))
    assert not s.ok and any("I couldn't check Tailscale" in line for line in s.lines)
    other = {**SERVE, "Web": {"x:8443": {"Handlers": {"/": {"Proxy": "http://127.0.0.1:3000"}}}}}
    s = await review(analyze_serve(json.dumps(other)))
    assert not s.ok and any("127.0.0.1:3000" in line for line in s.lines)


async def test_the_entry_page_lets_nothing_out_or_in(panel):
    _, p = panel
    async with _client(p, f"http://127.0.0.1:{p.port}") as mac:
        for go in ("//evil.example/x", "https://evil.example", "javascript:alert(1)"):
            r = await mac.get("/enter", params={"t": TOKEN, "go": go})
            assert 'content="0;url=/"' in r.text and "evil" not in r.text, go
        r = await mac.get("/enter", params={"t": TOKEN, "go": '/x"><script>alert(1)</script>'})
        assert "<script>" not in r.text and "&quot;&gt;&lt;script&gt;" in r.text
        assert "script-src 'self'" in r.headers["content-security-policy"]


async def test_the_session_is_renewed_on_every_visit(panel):
    """The session expires after 30 days without opening the panel, not 30 from signing in."""
    _, p = panel
    await p.check_tailscale()
    async with _client(p, MOBILE) as mobile:
        assert (await mobile.get(f"/enter?t={TOKEN}&go=/")).status_code == 200
        r = await mobile.get("/")
        assert r.status_code == 200
        cookie = r.headers["set-cookie"].lower()
        assert f"max-age={30 * 24 * 3600}" in cookie and "secure" in cookie
        assert "httponly" in cookie and "samesite=strict" in cookie
    async with _client(p, f"http://127.0.0.1:{p.port}") as anonymous:
        # Without a session nothing is renewed, and public things do not touch it.
        r = await anonymous.get("/")
        assert r.status_code == 401 and "set-cookie" not in r.headers
        assert "set-cookie" not in (await anonymous.get("/style.css")).headers
        # A cookie with another value is neither renewed nor turned into a good one.
        anonymous.cookies.set(COOKIE, "other")
        r = await anonymous.get("/")
        assert r.status_code == 401 and "set-cookie" not in r.headers


async def test_the_home_screen_icon(panel):
    _, p = panel
    async with _client(p, f"http://127.0.0.1:{p.port}") as anonymous:
        for path in ("/apple-touch-icon.png", "/apple-touch-icon-precomposed.png"):
            r = await anonymous.get(path)
            assert r.status_code == 200 and r.headers["content-type"] == "image/png"
            assert r.content[:8] == b"\x89PNG\r\n\x1a\n" and "set-cookie" not in r.headers
        # Only the icon is public: what starts the same but is another route asks for a
        # session.
        assert (await anonymous.get("/apple-touch-icon.png/../")).status_code in (401, 404)
    async with _client(p, f"http://127.0.0.1:{p.port}") as mac:
        await mac.get(f"/enter?t={TOKEN}&go=/")
        html = (await mac.get("/")).text
        assert '<link rel="apple-touch-icon" href="/apple-touch-icon.png">' in html
