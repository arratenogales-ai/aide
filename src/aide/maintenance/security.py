"""a) Security: fingerprints of the policy, Google, secrets, environment, panel,
dependencies and week."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import plistlib
import re
import subprocess
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aide.bot.channel import Button
from aide.common import api_key
from aide.common.tailscale import read_serve
from aide.maintenance.environment import search_uv
from aide.maintenance.section import Section

if TYPE_CHECKING:
    from aide.core.engine import Engine

# What decides what the agent can do: if it changes, the user has to approve it.
WATCHED_FILES = (
    "config/permissions.yaml",
    "src/aide/core/policy.py",
    "src/aide/core/hooks.py",
    "src/aide/core/permissions.py",
)
FORBIDDEN_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
PLIST = Path.home() / "Library" / "LaunchAgents" / "com.aide.agent.plist"
FINGERPRINTS_KEY = "approved_hashes"


def _n(n: int, one: str) -> str:
    """“1 blocked action”, “3 blocked actions”."""
    return f"{n} {one}" if n == 1 else f"{n} {one}s"


def fingerprints(root: Path) -> dict[str, str]:
    """Returns SHA-256 fingerprints of the watched files that exist."""
    return {
        r: hashlib.sha256((root / r).read_bytes()).hexdigest()
        for r in WATCHED_FILES
        if (root / r).is_file()
    }


def digest(values: dict[str, str]) -> str:
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()[:16]


async def approve_fingerprints(engine: Engine, seen_digest: str) -> bool:
    """The user approves the changes they saw in the report (only if they have not changed
    again)."""
    current = fingerprints(engine.config.paths.root)
    if digest(current) != seen_digest:
        return False
    await engine.db.kv_put(FINGERPRINTS_KEY, current)
    return True


def _variables_in_plist() -> list[str]:
    if not PLIST.is_file():
        return []
    try:
        data = plistlib.loads(PLIST.read_bytes())
    except Exception:
        return []
    return [v for v in FORBIDDEN_VARIABLES if v in (data.get("EnvironmentVariables") or {})]


def _variables_in_env(path: Path) -> list[str]:
    if not path.is_file():
        return []
    names = {
        line.split("=", 1)[0].strip()
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines()
        if "=" in line and not line.lstrip().startswith("#")
    }
    return [v for v in FORBIDDEN_VARIABLES if v in names]


def process_listeners(port: int, pid: int | None = None) -> list[str]:
    """Addresses the process listens on at that port (lsof)."""
    try:
        output = subprocess.run(
            ["lsof", "-nP", "-iTCP", "-sTCP:LISTEN", "-a", "-p", str(pid or os.getpid())],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return []
    addresses = []
    for line in output.splitlines()[1:]:
        m = re.search(r"TCP (\S+):(\d+) \(LISTEN\)", line)
        if m and int(m.group(2)) == port:
            addresses.append(m.group(1))
    return addresses


def audit_dependencies(root: Path) -> tuple[list[dict[str, Any]], str | None]:
    """Known vulnerabilities in the dependencies (pip-audit). Returns (vulnerable, error)."""
    with tempfile.TemporaryDirectory(prefix="aide-audit-") as tmp:
        requirements = Path(tmp) / "requirements.txt"
        try:
            exported = subprocess.run(
                [
                    search_uv(),
                    "export",
                    "--format",
                    "requirements-txt",
                    "--no-hashes",
                    "--frozen",
                    "--no-dev",
                    "--no-emit-project",
                    "-o",
                    str(requirements),
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=120,
                check=False,
            )
            if exported.returncode != 0:
                return [], f"uv export failed: {exported.stderr.strip()[-200:]}"
            audit = subprocess.run(
                [
                    sys.executable,
                    "-m",
                    "pip_audit",
                    "-r",
                    str(requirements),
                    "--disable-pip",
                    "--no-deps",
                    "--format",
                    "json",
                    "--progress-spinner",
                    "off",
                ],
                cwd=root,
                capture_output=True,
                text=True,
                timeout=300,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as e:
            return [], f"{type(e).__name__}: {e}"
        try:
            data = json.loads(audit.stdout or "{}")
        except ValueError:
            return (
                [],
                f"pip-audit did not return a readable result: {audit.stderr.strip()[-200:]}",
            )
    dependencies = data.get("dependencies", data if isinstance(data, list) else [])
    return [d for d in dependencies if d.get("vulns")], None


async def week_summary(engine: Engine) -> dict[str, Any]:
    """Summarizes the week's security events: blocked actions, injections and rejections."""
    since = (datetime.now(UTC) - timedelta(days=7)).isoformat(timespec="seconds")
    db = engine.db

    async def count(kind: str) -> tuple[int, list[str]]:
        rows = await db.everything(
            "SELECT summary FROM events WHERE kind=? AND hour >= ? ORDER BY id DESC", (kind, since)
        )
        return len(rows), [(f["summary"] or "")[:120] for f in rows[:3]]

    blocked, blocked_examples = await count("blocked")
    injections, injections_examples = await count("possible_injection")
    foreign, _ = await count("user_no_allowed")
    rejected = await db.value(
        "SELECT COUNT(*) FROM approvals WHERE state='rejected' AND resolved >= ?", (since,)
    )
    return {
        "blocked": blocked,
        "blocked_examples": blocked_examples,
        "rejected": rejected or 0,
        "injections": injections,
        "injections_examples": injections_examples,
        "disallowed_users": foreign,
    }


async def review(engine: Engine) -> Section:
    """Checks the watched files against their last approved fingerprints."""
    s = Section("security", "Security")
    config = engine.config
    paths = config.paths

    # Fingerprints of permissions.yaml, the policy and the hooks against the last approval.
    current = await asyncio.to_thread(fingerprints, paths.root)
    approved = await engine.db.kv_read(FINGERPRINTS_KEY)
    if not approved:
        await engine.db.kv_put(FINGERPRINTS_KEY, current)
        s.lines.append(
            "First check: I saved the current fingerprints of permissions.yaml, the policy and the "
            "hooks as a reference."
        )
    else:
        changed = [r for r in WATCHED_FILES if approved.get(r) != current.get(r)]
        if changed:
            s.failure(
                "Changed since the last approval: "
                + ", ".join(Path(r).name for r in changed)
                + ".",
                'If you or Claude Code made the change on purpose, tap "Approve security '
                "changes\". If you didn't expect it, don't approve it and ask Claude Code to review it.",
            )
            s.buttons.append(
                [Button("Approve security changes", f"security:approve:{digest(current)}")]
            )
        else:
            s.lines.append(
                "permissions.yaml, the policy and the hooks haven't changed since the last approval."
            )

    # Google permissions: exactly the three Aide uses.
    auth = engine.core.services.get("google")
    if auth is not None:
        from aide.tools.google_auth import SCOPES

        try:
            granted = set(await asyncio.to_thread(auth.granted_permissions))
        except Exception as e:
            s.failure(
                f"I couldn't check the Google permissions ({type(e).__name__}).",
                "That check is missing this week. If Google has asked you to authorize again, "
                "do it from the panel; if not, ask Claude Code to look into it.",
            )
        else:
            if granted == set(SCOPES):
                s.lines.append("Google has exactly Aide's three permissions.")
            else:
                extra = sorted(granted - set(SCOPES))
                missing = sorted(set(SCOPES) - granted)
                s.failure(
                    f"The Google permissions are not the expected ones (extra: {extra or 'none'}, "
                    f"missing: {missing or 'none'}).",
                    "Remove Aide's access at myaccount.google.com/permissions and authorize "
                    "it again with the link the bot sends you.",
                )
        # Without a configured account, it is not checked that the authorized one is the
        # user's.
        if not engine.config.google_account:
            s.failure(
                "No Google account is set up, so when the authorization is renewed there is no "
                "check that the account is yours.",
                "Add GOOGLE_ACCOUNT=<your Gmail address> to .env and restart the agent.",
            )

    # Secrets with 600 permissions.
    permissions_well = True
    for path in (paths.env, paths.config / "credentials.json"):
        if not path.exists():
            continue
        mode = path.stat().st_mode & 0o777
        if mode != 0o600:
            permissions_well = False
            s.failure(
                f"{path.name} has permissions {oct(mode)[2:]} instead of 600.",
                f'In Terminal: chmod 600 "{path}"',
            )
    if permissions_well:
        s.lines.append("Only your user can read .env and credentials.json (600).")

    # No Anthropic API credential within reach of the agent. With the API key, the key can only
    # be in the keychain: Claude Code asks for it with apiKeyHelper.
    present = sorted(
        {v for v in FORBIDDEN_VARIABLES if os.environ.get(v)}
        | set(_variables_in_plist())
        | set(_variables_in_env(paths.env))
    )
    if present and config.uses_api_key:
        s.failure(
            f"There are Anthropic credentials outside the keychain: {', '.join(present)}.",
            "The agent takes the API key from the keychain: remove them from the environment, the "
            ".env and the LaunchAgent and restart the agent (an ANTHROPIC_API_KEY found at "
            "startup is saved in the keychain first).",
        )
    elif present:
        s.failure(
            f"There are Anthropic API credentials set up: {', '.join(present)}.",
            "Remove them from the environment, the .env and the LaunchAgent and restart the agent: "
            "with claude.credential: login the agent uses your subscription, not the API.",
        )
    elif config.uses_api_key and not await asyncio.to_thread(api_key.read):
        s.failure(
            "There is no Claude API key in the keychain.",
            "In the Terminal, in the Aide folder: uv run aide --set-api-key",
        )
    elif config.uses_api_key:
        s.lines.append(
            "The Claude API key is in the keychain and reaches Claude Code through apiKeyHelper; "
            "there is no Anthropic credential in the environment."
        )
    else:
        s.lines.append("There is no ANTHROPIC_API_KEY or ANTHROPIC_AUTH_TOKEN in the environment.")

    # The panel only listens on this Mac.
    local = ("127.0.0.1", "localhost", "::1", "[::1]")
    if config.panel_host not in local:
        s.failure(
            f"The panel is set up to listen on {config.panel_host}.",
            "Set PANEL_HOST=127.0.0.1 in the .env and restart the agent.",
        )
    else:
        addresses = await asyncio.to_thread(process_listeners, config.panel_port)
        foreign_items = [d for d in addresses if d not in local]
        if foreign_items:
            s.failure(
                f"The panel listens on {', '.join(foreign_items)}.",
                "Restart the agent; if nothing changes, ask Claude Code to look into it.",
            )
        elif addresses:
            s.lines.append("The panel only listens on 127.0.0.1.")
        else:
            s.lines.append(
                f"The panel isn't listening in this process right now (set up on {config.panel_host})."
            )

    # Tailscale: the panel on the iPhone only inside the user's network, never with
    # Funnel.
    if config.panel_tailscale:
        state = await asyncio.to_thread(read_serve)
        own = {
            f"http://127.0.0.1:{config.panel_port}",
            f"http://localhost:{config.panel_port}",
        }
        if state.funnel:
            s.failure(
                "Tailscale Funnel is on: it would open the panel to the internet. The panel "
                "blocks it, so it won't open from the iPhone.",
                "Ask Claude Code to turn it off (tailscale funnel reset).",
            )
        elif state.funnel is None:
            s.failure(
                f"I couldn't check Tailscale ({state.error}).",
                "Make sure the Tailscale app is open on the Mac; if nothing changes, ask "
                "Claude Code to look into it.",
            )
        elif foreign := [d for d in state.destinations if d not in own]:
            s.failure(
                f"Tailscale shares more than the panel on your network: {', '.join(foreign)}.",
                "Ask Claude Code to look into it.",
            )
        else:
            s.lines.append("Tailscale Funnel is off: the panel only opens on your network.")

    # Known vulnerabilities in the dependencies.
    vulnerable, error = await asyncio.to_thread(audit_dependencies, paths.root)
    if error:
        s.failure(
            f"I couldn't look for vulnerabilities in the dependencies ({error}).",
            "That check is missing this week. Ask Claude Code to look into it with this message.",
        )
    elif vulnerable:
        detail = "; ".join(
            f"{d['name']} {d['version']} ({', '.join(v['id'] for v in d['vulns'][:3])})"
            for d in vulnerable[:5]
        )
        fixes = "; ".join(
            f"{d['name']} to {', '.join(sorted({f for v in d['vulns'] for f in v.get('fix_versions', [])})) or 'a fixed version'}"
            for d in vulnerable[:5]
        )
        s.failure(
            f"Dependencies with known vulnerabilities: {detail}.",
            f"Ask Claude Code to update {fixes} and run the tests.",
        )
        s.data["vulnerable"] = [d["name"] for d in vulnerable]
    else:
        s.lines.append("pip-audit finds no known vulnerabilities in the dependencies.")

    # Summary of the week.
    week = await week_summary(engine)
    s.data["week"] = week
    s.lines.append(
        f"This week: {_n(week['blocked'], 'blocked action')}, "
        f"{_n(week['rejected'], 'rejected approval')}, "
        f"{_n(week['injections'], 'possible injection')} detected and "
        f"{_n(week['disallowed_users'], 'message')} from users who aren't allowed "
        "(the bot ignores them)."
    )
    return s
