"""The Claude API key in the macOS keychain and the helper that gives it to Claude Code.

The Agent SDK documentation asks that an application built with it authenticate with an API key from
the Claude Console (or with Bedrock, Vertex or Foundry), and not with the claude.ai login:
https://code.claude.com/docs/en/agent-sdk/overview. The form the quickstart gives is
`ANTHROPIC_API_KEY` in the environment, but then the key would reach every Bash command of the
agent. Claude Code offers another route, `apiKeyHelper`
(https://code.claude.com/docs/en/authentication and
https://code.claude.com/docs/en/settings-reference#apikeyhelper): a command that prints the key and
that Claude Code runs itself. This way the key lives in the keychain and is never in the environment
of the agent's sessions.

The helper is this very module (`python -I -m aide.common.api_key`):
- with `-I`, Python does not add the current folder to the module path (Claude Code's working folder
  is agent/, where the agent can write) nor read PYTHON* variables;
- it refuses to give the key if launched by a process inside the macOS sandbox, which is
  where all the agent's Bash commands run (`allowUnsandboxedCommands` is off);
- the policy, in addition, blocks any command that names it.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import os
import shlex
import sys

SERVICE = "aide"
ACCOUNT = "anthropic-api-key"
MODULE = "aide.common.api_key"


def read() -> str | None:
    """Returns the API key stored in the keychain, or None if missing or unreadable."""
    import keyring

    try:
        value = keyring.get_password(SERVICE, ACCOUNT)
    except Exception:
        return None
    return value.strip() if value and value.strip() else None


def save(value: str) -> None:
    """Stores the API key, stripped of whitespace, in the keychain."""
    import keyring

    keyring.set_password(SERVICE, ACCOUNT, value.strip())


def delete() -> None:
    """Removes the API key from the keychain, ignoring errors."""
    import keyring

    try:
        keyring.delete_password(SERVICE, ACCOUNT)
    except Exception:
        pass


def valid_shape(value: str) -> bool:
    """A Claude Console key: a single word, no spaces, with its prefix."""
    value = value.strip()
    return value.startswith("sk-ant-") and len(value) >= 20 and not any(c.isspace() for c in value)


def helper_order(python: str | None = None) -> str:
    """The `apiKeyHelper` command (Claude Code launches it with /bin/sh)."""
    return f"{shlex.quote(python or sys.executable)} -I -m {MODULE}"


def in_sandbox(pid: int | None = None) -> bool:
    """Whether the process is inside a macOS sandbox (Seatbelt). Outside macOS, or if it cannot
    be known, the answer is yes: when in doubt, the helper does not give the key."""
    if sys.platform != "darwin":
        return True
    try:
        libc = ctypes.CDLL(ctypes.util.find_library("System") or "/usr/lib/libSystem.B.dylib")
        check = libc.sandbox_check
        check.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int]
        check.restype = ctypes.c_int
        return check(os.getpid() if pid is None else pid, None, 0) != 0
    except Exception:
        return True


def main() -> int:
    """The helper: prints the key and nothing else. Errors go to stderr, without the key."""
    if in_sandbox():
        print("aide: the API key helper doesn't run inside the sandbox", file=sys.stderr)
        return 1
    value = read()
    if not value:
        print(
            "aide: no API key in the keychain; run: uv run aide --set-api-key",
            file=sys.stderr,
        )
        return 1
    sys.stdout.write(value)
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
