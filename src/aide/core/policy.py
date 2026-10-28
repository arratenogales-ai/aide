"""Deterministic policy: classifies each tool call into a level.

    0 free · 1 notice · 2 approval · 3 forbidden

It doesn't use the model. The PreToolUse hook consults it before any other SDK rule.
On macOS the file system is case-insensitive, so paths are compared in
lowercase: a deny rule can't be evaded by changing case.

In real Chrome some pages are always forbidden (payment, billing, account settings...),
in addition to the forbidden domains.
"""

from __future__ import annotations

import fnmatch
import glob
import ipaddress
import os
import re
import shlex
import unicodedata
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from aide.common.config import Config
from aide.core import navigation as nav
from aide.core.chrome import forget_tab, is_password
from aide.core.chrome import key as chrome_key

FREE, NOTICE, APPROVAL, FORBIDDEN = 0, 1, 2, 3

SUBAGENTS = (
    "researcher",
    "reader",
    "builder",
    "writer",
    "reviewer",
    "quick_reviewer",
    "navigator",
)
# Subagents that only the orchestrator can launch (they use real Chrome).
ONLY_ORCHESTRATOR = {"navigator"}
# Real Chrome tools that don't act on a tab's page.
CHROME_NO_PAGE = {
    "navigate",
    "tabs_create_mcp",
    "tabs_context_mcp",
    "tabs_close_mcp",
    "list_connected_browsers",
    "select_browser",
    "switch_browser",
    "shortcuts_list",
    "resize_window",
    "browser_batch",
}

# Built-in tools with no effects outside the session. StructuredOutput is the one Claude Code uses
# to return structured output (the reviewer's verdict): without it allowed, the reviewer couldn't
# deliver its verdict and the review never finished.
BUILT_IN_FREE = {
    "StructuredOutput",
    "WebSearch",
    "Skill",
    "TaskCreate",
    "TaskUpdate",
    "TaskGet",
    "TaskList",
    "TodoWrite",
    "ToolSearch",
}
# Own read-only tools (allowed in the plan phase).
OWN_READ = {
    "gmail_search",
    "gmail_read",
    "gmail_thread",
    "gmail_list_drafts",
    "calendar_events",
    "calendar_slots",
    "memory_search",
    "history_search",
    "history_task",
    "browser_read",
    "browser_screenshot",
    "list_scheduled_tasks",
    # Search your saved documents: also in quick replies.
    "documents_search",
}
CHROME_READ = {
    "tabs_context_mcp",
    "tabs_close_mcp",
    "read_page",
    "get_page_text",
    "find",
    "read_console_messages",
    "read_network_requests",
    "resize_window",
    "list_connected_browsers",
    "select_browser",
    "switch_browser",
    "shortcuts_list",
    "gif_creator",
}
COMPUTER_FREE = {
    "screenshot",
    "scroll",
    "scroll_to",
    "zoom",
    "wait",
    "hover",
    "mouse_move",
    "cursor_position",
}
COMPUTER_CLICKS = {"left_click", "right_click", "double_click", "triple_click", "left_click_drag"}
# Keys that submit a form or activate the button that has focus.
SEND_KEYS = {"enter", "return", "kp_enter", "space"}
# browser_test: the steps that navigate (their address is checked) and those that fill in or press
# (on an external website they need approval). The tool only accepts its own names; the others are
# there out of caution, in case the model uses them: the list can only grow.
NAVIGATE_STEPS = {"ir", "go", "goto", "navigate", "open", "visit"}
INTERACTIVE_STEPS = {"click", "fill", "enviar", "press", "select"}
INTERACTIVE_STEPS |= {"click", "fill", "type", "submit", "send", "press", "select", "check"}

WRITE_COMMANDS = {
    "rm",
    "rmdir",
    "mv",
    "cp",
    "touch",
    "mkdir",
    "ln",
    "chmod",
    "chown",
    "chgrp",
    "tee",
    "truncate",
    "install",
    "rsync",
    "dd",
    "unzip",
    "patch",
}
DELETE_COMMANDS = {"rm", "rmdir", "mv"}
NETWORK_COMMANDS = {
    "curl",
    "wget",
    "http",
    "https",
    "nc",
    "ncat",
    "ssh",
    "scp",
    "sftp",
    "telnet",
    "ftp",
}
READ_ONLY_COMMANDS = {
    "ls",
    "cat",
    "head",
    "tail",
    "wc",
    "grep",
    "egrep",
    "rg",
    "find",
    "file",
    "stat",
    "du",
    "df",
    "tree",
    "pwd",
    "echo",
    "printf",
    "date",
    "which",
    "type",
    "sort",
    "uniq",
    "cut",
    "jq",
    "less",
    "realpath",
    "basename",
    "dirname",
    "true",
}
DANGEROUS_FIND = ("-delete", "-exec", "-execdir", "-ok", "-okdir", "-fprint", "-fls", "-fprintf")
SHELLS = {"bash", "sh", "zsh", "dash", "ksh"}
WRAPPERS = {
    "env",
    "nohup",
    "time",
    "command",
    "exec",
    "xargs",
    "timeout",
    "nice",
    "stdbuf",
    "caffeinate",
    "builtin",
}
# Shell words that can come before a command (`do curl ...`, `then cp ...`, `{ cd x`).
RESERVED_WORDS = {"do", "then", "else", "elif", "if", "while", "until", "{", "}", "!"}
TEXT_COMMANDS = {"echo", "printf", "tr"}
COMMANDS_WITH_PATTERN = {"sed", "awk", "gawk", "grep", "egrep", "fgrep", "rg", "jq", "perl"}
RE_HEREDOC = re.compile(r"(?<!<)<<-?(?!<)\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")


@dataclass
class Decision:
    """A permission decision with its level, reason, kind and detail."""

    level: int
    reason: str
    kind: str = "general"
    detail: str = ""


@dataclass
class CallContext:
    """What the policy needs to know about the session making the call."""

    task_id: int | None = None
    task_folder: Path | None = None
    agent: str | None = None  # subagent type; None is the orchestrator or the main session
    phase: str = "run"  # plan | run | quick | review
    # Real Chrome elements per tab and ref (core/chrome.py) and whether the last click was in a
    # password field.
    chrome_elements: dict[str, dict[str, str]] = field(default_factory=dict)
    password_focus: bool = False
    small: bool = False  # small task
    # The last known address of each real Chrome tab.
    urls_chrome: dict[str, str] = field(default_factory=dict)
    # Navigation: browser actions the task carries.
    browser_actions: int = 0
    # Task usage in tokens (the recorded one plus the current turn's) and its budget for tasks
    # with Chrome; 0 if unknown.
    tokens_task: int = 0
    token_cap: int = 0


def _min(path: Path | str) -> str:
    # In NFC: the Mac's disk treats a composed “Ó” and an “O” with a separate accent as the
    # same, so the policy does too.
    return unicodedata.normalize("NFC", str(path)).lower().rstrip("/") or "/"


def _inside(path: Path, base: Path) -> bool:
    r, b = _min(path), _min(base)
    return r == b or r.startswith(b + "/")


def _send_keys(text: str) -> bool:
    """Whether a key press (for example “Tab Tab space” or “ctrl+Return”) includes Enter or space."""
    return any(t in SEND_KEYS for t in re.split(r"[\s+]+", text.lower()) if t)


# Bash: a path with a variable or a shell command the policy can't resolve, and the “unknown” folder
# where the command ends up after a `cd` to such a path.
UNRESOLVED_CD = Path("/$cd-sin-resolver")
# Real Chrome: a tab's page after a click inside a batch, which isn't known until the hook reads the
# response.
UNKNOWN_PAGE = "about:unknown"
VARIABLE_REASON = (
    "there's no way to tell where this writes: the path has a variable or a shell command the "
    "policy can't resolve. Write the full path, without variables, or use Write, which creates "
    "any missing folders"
)


CD_REASON = (
    "after a cd, pushd, popd, source or eval there's no way to be sure which folder the command "
    "writes to (it may fail, run in a subshell or change it): write the full path so it doesn't "
    "depend on that, or use Write"
)


READ_OUTSIDE_REASON = "reading a path that isn't allowed"
# The built-in tools that receive a path in file_path, notebook_path or path.
PATH_TOOLS = {
    "Read",
    "NotebookRead",
    "Glob",
    "Grep",
    "Write",
    "Edit",
    "MultiEdit",
    "NotebookEdit",
}
GLOB_UPLOAD_REASON = 'a Glob pattern can\'t go up a folder with "..": use path for the folder'
AMBIGUOUS_PATH_REASON = (
    "the path starts or ends with quotes or spaces: write it without them, exactly as it is"
)


# xargs options (BSD and GNU) that carry their value in the next word.
XARGS_WITH_VALUE = {"-E", "-I", "-J", "-L", "-n", "-P", "-R", "-S", "-s", "-d", "-a"}
XARGS_WITH_VALUE |= {"--arg-file", "--delimiter", "--eof", "--max-lines", "--max-args"}
XARGS_WITH_VALUE |= {"--max-procs", "--max-chars"}


def _xargs_order(rest: list[str]) -> str:
    """The command xargs runs: the first word that is neither an option nor its value."""
    i = 0
    while i < len(rest):
        w = rest[i]
        if w == "--":
            return rest[i + 1] if i + 1 < len(rest) else ""
        if not w.startswith("-"):
            return w
        i += 2 if w in XARGS_WITH_VALUE else 1
    return ""


def _unresolved(text: str) -> bool:
    return "$" in text or "`" in text


def _domain(url: str) -> tuple[str, int | None, str]:
    url = nav.as_browser(url)
    try:
        p = urlparse(url if "://" in url else f"https://{url}")
        # “paypal.com.” (with the trailing dot) is the same domain: without removing it, it
        # skipped the domain and page rules.
        return (p.hostname or "").lower().rstrip("."), p.port, (p.scheme or "").lower()
    except ValueError:
        return "", None, ""


_TAILSCALE_NETWORK = ipaddress.ip_network("100.64.0.0/10")


def _is_tailscale(host: str) -> bool:
    """A MagicDNS name (`*.ts.net`) or an address on the Tailscale network."""
    if host == "ts.net" or host.endswith(".ts.net"):
        return True
    try:
        return ipaddress.ip_address(host.strip("[]")) in _TAILSCALE_NETWORK
    except ValueError:
        return False


def _matches_domain(host: str, pattern: str) -> bool:
    pattern = pattern.lower().strip()
    if any(c in pattern for c in "*?["):
        return fnmatch.fnmatch(host, pattern)
    return host == pattern or host.endswith("." + pattern)


def _millions(tokens: int) -> str:
    return f"{tokens / 1_000_000:.1f} M"


def _page(url: str) -> tuple[str, str]:
    """(domain without www, path) in lowercase, to compare with the forbidden pages."""
    url = nav.as_browser(url)
    try:
        p = urlparse(url if "://" in url else f"https://{url}")
        host = (p.hostname or "").lower().rstrip(".")
    except ValueError:
        return "", "/"
    return host.removeprefix("www."), (p.path or "/").lower()


def _with_private(text: str) -> str:
    """On macOS, /tmp, /etc and /var are links to /private/...: a pattern is written as the
    paths look once resolved (before, “/etc/sudoers*” never matched)."""
    for folder in ("/tmp", "/etc", "/var"):
        if text == folder or text.startswith(folder + "/"):
            return "/private" + text
    return text


def glob_no_uppercase(glob: str) -> str:
    """A ripgrep glob that matches case-insensitively: each letter is written as [xX].
    Ripgrep's --glob is case-sensitive and the policy is not: without this, from agent/
    “Token.json” or “.ENV” came out. Escapes and [...] classes are left as they are."""
    output: list[str] = []
    i = 0
    while i < len(glob):
        c = glob[i]
        if c == "\\" and i + 1 < len(glob):
            output.append(glob[i : i + 2])
            i += 2
            continue
        if c == "[":
            end = glob.find("]", i + 1)
            if end != -1:
                output.append(glob[i : end + 1])
                i = end + 1
                continue
        lower, greater = c.lower(), c.upper()
        if lower != greater and len(lower) == 1 and len(greater) == 1:
            output.append(f"[{lower}{greater}]")
        else:
            output.append(c)
        i += 1
    return "".join(output)


def ambiguous_path(value: Any) -> bool:
    """A path that starts or ends with quotes or spaces. The policy strips them when resolving
    the path, but the tool uses the path as is: “k'” could be a link to something
    forbidden while the policy looks at “k”."""
    return isinstance(value, str) and value != value.strip().strip("'\"")


def _fixed_prefix(pattern: str) -> tuple[str, str]:
    """The folders of a pattern before the first wildcard, and what follows."""
    parts = pattern.split("/")
    for i, part in enumerate(parts):
        if any(c in part for c in "*?["):
            return "/".join(parts[:i]), "/".join(parts[i:])
    return pattern, ""


class Policy:
    """The permission policy that classifies actions using the configured paths and rules."""

    def __init__(self, config: Config) -> None:
        self.config = config
        paths = config.paths
        self.root = paths.root.resolve()
        # The user's folders, which may be outside the root.
        self.folders = paths.folders.resolve()
        self.agent = paths.agent.resolve()
        self.tasks = paths.tasks.resolve()
        self.projects = paths.projects.resolve()
        self.home = Path.home().resolve()
        p = config.permissions
        self._forbidden = [self._pattern(x) for x in p.paths.forbidden]
        self._forbidden_write = [self._pattern(x) for x in p.paths.forbidden_write]
        self._write_approval = [self._pattern(x) for x in p.paths.write_approval]
        self._free_read = [self._pattern(x) for x in p.paths.free_read]
        self._folders = [(c.resolved(), c.mode) for c in config.settings.allowed_folders]
        self._forbidden_bash = [re.compile(x) for x in p.bash.forbidden]
        self._bash_approval = [re.compile(x) for x in p.bash.approval]
        self._bash_notice = [re.compile(x) for x in p.bash.notice]
        port = config.panel_port
        self.panel_port = port
        # The panel on this Mac and any Tailscale name or address (through
        # which the iPhone connects): the agent has nothing to do on that network.
        self._re_panel = re.compile(
            rf"(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\]|::1)[:/]+{port}\b"
            r"|\.ts\.net\b"
            r"|\b100\.(6[4-9]|[7-9][0-9]|1[01][0-9]|12[0-7])\.[0-9]{1,3}\.[0-9]{1,3}\b",
            re.IGNORECASE,
        )
        self._send_words = [w.lower() for w in p.chrome.send_words]
        self._forbidden_pages = [x.lower().strip() for x in p.chrome.forbidden_pages]
        self._max_actions = config.settings.real_browser.max_actions
        self._token_notice = config.settings.real_browser.token_notice

    # --- paths -----------------------------------------------------------------------

    def _pattern(self, pattern: str) -> str:
        text = (
            pattern.replace("{root}", str(self.root))
            .replace("{folders}", str(self.folders))
            .replace("{agent}", str(self.agent))
        )
        return unicodedata.normalize("NFC", _with_private(os.path.expanduser(text))).lower()

    def _original_pattern(self, pattern: str) -> str:
        """The pattern with its paths filled in and not lowercased (for ripgrep)."""
        text = (
            pattern.replace("{root}", str(self.root))
            .replace("{folders}", str(self.folders))
            .replace("{agent}", str(self.agent))
        )
        return _with_private(os.path.expanduser(text))

    def search_exclusions(self, path: str | Path | None) -> list[str]:
        """Ripgrep globs, with “!”, so that a Grep search from `path` reads nothing
        forbidden below. The policy only checks the folder where the
        search starts, and the CLI's Grep also searches hidden files (--hidden): without this, from
        agent/ a .env or a project key would be read. Paths
        with fixed folders are excluded with “**/” in front, which is broader: excess, but not a gap.
        Each letter goes as [xX], because ripgrep's --glob is case-sensitive."""
        root = self.resolve(path or ".")
        min_root = _min(root)
        # With root “/”, what's below starts right after the slash (before, “//” was left).
        base = min_root if min_root.endswith("/") else min_root + "/"
        output: list[str] = []
        for original in self.config.permissions.paths.forbidden:
            pattern = self._original_pattern(original)
            if pattern.startswith("**/"):
                relative = pattern
            else:
                prefix, rest = _fixed_prefix(pattern)
                min_prefix = prefix.lower()
                if min_prefix.startswith(base):
                    relative = "**/" + pattern[len(base) :]
                elif min_prefix == min_root or base.startswith(min_prefix + "/"):
                    # The fixed folder is at the root or above: it only counts
                    # if what follows is valid at any depth
                    # (“{root}/**/.git/**”).
                    if min_prefix != min_root and not rest.startswith("**/"):
                        continue
                    relative = rest if rest.startswith("**") else f"**/{rest}"
                else:
                    continue
            if relative in ("", "**", "**/"):
                continue
            for candidate in (relative, relative.removesuffix("/**")):
                # The CLI's Grep splits its glob on spaces and commas.
                exclusion = "!" + glob_no_uppercase(re.sub(r"[\s,]", "?", candidate))
                if exclusion not in output:
                    output.append(exclusion)
        return output

    def search_entry(self, tool: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """A Grep's input with the exclusions for what is forbidden added to its glob,
        or None if it isn't a Grep or there is nothing to exclude. If it already has them (the hook added them and
        then it goes through approval), they aren't repeated."""
        if tool != "Grep":
            return None
        exclusions = self.search_exclusions(data.get("path"))
        if not exclusions:
            return None
        own = str(data.get("glob") or "").strip()
        already = set(own.split())
        new = [x for x in exclusions if x not in already]
        if not new:
            return None
        return {**data, "glob": " ".join(x for x in (own, *new) if x)}

    def resolve(self, path: str | Path, base: Path | None = None) -> Path:
        """Resolves a path to an absolute one, expanding the home and using a base folder."""
        text = os.path.expanduser(str(path).strip().strip("'\""))
        p = Path(text)
        if not p.is_absolute():
            p = (base or self.agent) / p
        try:
            return p.resolve()
        except (OSError, RuntimeError):
            return Path(os.path.normpath(p))

    @staticmethod
    def _pattern_home(path: Path, patterns: list[str]) -> bool:
        r = _min(path)
        return any(
            fnmatch.fnmatchcase(r, pat) or fnmatch.fnmatchcase(r + "/", pat) for pat in patterns
        )

    def _contains_protected(self, path: Path) -> bool:
        """The path is a directory that contains something protected (deleting it would delete it)."""
        r = _min(path)
        candidates = [
            self.agent / "profile.md",
            self.agent / "memory.md",
            self.agent / "CLAUDE.md",
            self.agent / ".claude",
            self.root / "src",
            self.root / "config",
            self.root / "data",
            self.root / ".env",
        ]
        return any(_min(c).startswith(r + "/") for c in candidates) or r in ("/", _min(self.home))

    def classify_read(self, path: str | Path, base: Path | None = None) -> Decision:
        """Classifies reading a path as forbidden, free or needing approval."""
        r = self.resolve(path, base)
        if self._pattern_home(r, self._forbidden):
            return Decision(FORBIDDEN, f"reading a protected path: {r}", "file")
        if _inside(r, self.agent):
            return Decision(FREE, "inside agent/", "file")
        if self._pattern_home(r, self._free_read):
            return Decision(FREE, "path that is free to read", "file")
        for folder, _mode in self._folders:
            if _inside(r, folder):
                return Decision(FREE, "allowed personal folder", "file")
        return Decision(APPROVAL, f"{READ_OUTSIDE_REASON}: {r}", "file", detail=f"Read {r}")

    def classify_write(
        self,
        path: str | Path,
        ctx: CallContext,
        base: Path | None = None,
        deleted: bool = False,
    ) -> Decision:
        """Classifies writing or deleting a path, forbidding protected ones."""
        r = self.resolve(path, base)
        if self._pattern_home(r, self._forbidden) or self._pattern_home(r, self._forbidden_write):
            return Decision(FORBIDDEN, f"writing to a protected path: {r}", "file")
        if deleted and self._contains_protected(r):
            return Decision(FORBIDDEN, f"deleting {r} would remove protected files", "file")
        for folder, mode in self._folders:
            if _inside(r, folder) and mode == "write" and ctx.agent is None:
                return Decision(NOTICE, f"writing to an allowed personal folder: {r}", "file")
        if not _inside(r, self.agent):
            return Decision(FORBIDDEN, f"the agent only writes inside agent/ ({r})", "file")
        if self._pattern_home(r, self._write_approval):
            return Decision(
                APPROVAL,
                f"changing the agent's configuration: {r}",
                "file",
                detail=f"Write {r}",
            )
        task = ctx.task_folder.resolve() if ctx.task_folder else None
        in_task = task is not None and _inside(r, task)
        if deleted and (_inside(self.tasks, r) or _inside(self.projects, r)):
            return Decision(
                APPROVAL,
                f"deleting {r} affects every task or project",
                "file",
                detail=f"Delete {r}",
            )
        if deleted and _min(r.parent) == _min(self.tasks) and not in_task:
            return Decision(
                APPROVAL,
                f"deleting another task's folder: {r}",
                "file",
                detail=f"Delete {r}",
            )
        # Restrictions per subagent.
        in_projects = _inside(r, self.projects)
        agent = ctx.agent
        if agent in ("reviewer", "quick_reviewer"):
            return Decision(FORBIDDEN, "the reviewer doesn't change deliverables", "file")
        if agent in ("researcher", "reader", "navigator") and not in_task:
            return Decision(
                FORBIDDEN, f"the {agent} subagent only writes in the task folder", "file"
            )
        if agent == "builder" and not (in_task or in_projects):
            return Decision(
                FORBIDDEN,
                "the builder subagent only works in projects/ and in the task folder",
                "file",
            )
        if agent == "writer" and not (in_task or in_projects):
            return Decision(
                FORBIDDEN,
                "the writer subagent only writes in the task folder and in projects/",
                "file",
            )
        return Decision(FREE, "writing inside agent/", "file")

    # --- Bash ------------------------------------------------------------------------
    # Commands are split respecting quotes. Forbidden commands are searched for in command
    # position, not in any word (in Spanish "su" is a possessive). The body of a heredoc is data
    # unless it is passed to a shell interpreter, and `bash -c`, `sh -c` and `eval` are analyzed
    # inside. The system sandbox is the backstop.

    @staticmethod
    def _remove_heredocs(command: str) -> tuple[str, list[tuple[str, str]]]:
        """Returns the command without the heredoc bodies and the list (command line, body)."""
        lines = command.split("\n")
        output: list[str] = []
        bodies: list[tuple[str, str]] = []
        i = 0
        while i < len(lines):
            line = lines[i]
            output.append(line)
            m = RE_HEREDOC.search(line)
            if m:
                end, body = m.group(2), []
                i += 1
                while i < len(lines) and lines[i].strip() != end:
                    body.append(lines[i])
                    i += 1
                bodies.append((line, "\n".join(body)))
            i += 1
        return "\n".join(output), bodies

    @staticmethod
    def _without_quotes(text: str) -> str:
        """Empties the content of quoted strings (for the text patterns)."""
        text = re.sub(r"'[^']*'", "''", text)
        return re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)

    @staticmethod
    def _replacements(text: str) -> list[str]:
        """Commands inside $( ) and backticks outside single quotes."""
        no_simple = re.sub(r"'[^']*'", "''", text)
        return re.findall(r"\$\(([^()]*)\)", no_simple) + re.findall(r"`([^`]*)`", no_simple)

    @staticmethod
    def _words(text: str) -> list[list[str]]:
        """Splits into simple commands (lists of words) respecting quotes."""
        # Line breaks outside quotes separate commands.
        parts, quote, previous = [], None, ""
        for c in text:
            if quote:
                if c == quote and previous != "\\":
                    quote = None
            elif c in "'\"":
                quote = c
            elif c == "\n":
                c = " ; "
            parts.append(c)
            previous = c
        lexer = shlex.shlex("".join(parts), posix=True, punctuation_chars=";&|()<>")
        lexer.whitespace_split = True
        lexer.commenters = ""
        try:
            tokens = list(lexer)
        except ValueError:
            tokens = text.split()
        commands: list[list[str]] = [[]]
        for t in tokens:
            if t and set(t) <= set(";&|()"):
                commands.append([])
            else:
                commands[-1].append(t)
        return [o for o in commands if o]

    def _looks_like_path(self, token: str, base: Path) -> bool:
        if not token or "://" in token or token.startswith("-"):
            return False
        if token in (".", "..", "~", "/") or token.startswith(("./", "../", "~/")):
            return True
        if token.startswith("/"):
            return bool(re.match(r"^/[\w.~@+ -]", token))
        if "/" in token:
            return (base / token.split("/", 1)[0]).exists()
        return False

    def _simple_bash(
        self, words: list[str], ctx: CallContext, base: Path, detail: str, depth: int
    ) -> tuple[Decision | None, Path, bool]:
        """Analyzes a simple command. Returns (most severe decision, new base, read-only)."""
        p = self.config.permissions.bash
        # Variable assignments and wrappers before the command (env FOO=1 sudo ...).
        change = True
        while words and change:
            change = False
            while words and (re.match(r"^[A-Za-z_]\w*=", words[0]) or words[0] in RESERVED_WORDS):
                words, change = words[1:], True
            if words and os.path.basename(words[0]) in WRAPPERS:
                wrapper, rest = os.path.basename(words[0]), words[1:]
                if wrapper == "xargs" and os.path.basename(_xargs_order(rest)) in WRITE_COMMANDS:
                    # Files arrive through standard input: it isn't known which
                    # ones.
                    reason = "xargs with a command that writes or deletes: there's no way to know which files"
                    return Decision(FORBIDDEN, reason, "bash", detail), base, False
                if rest and rest[0].startswith("-"):
                    return self._after_wrapper(wrapper, rest, ctx, base, detail, depth)
                words, change = rest, True
                while words and re.match(r"^\d+(\.\d+)?[smhd]?$", words[0]):
                    words = words[1:]
        if not words:
            return None, base, True
        order = os.path.basename(words[0])
        args = words[1:]
        if order in p.forbidden_commands:
            return Decision(FORBIDDEN, f"forbidden command: {order}", "bash", detail), base, False
        if order in ("cd", "pushd", "popd"):
            return None, self._folder_after_cd(order, args, base, detail), True
        if order in SHELLS and "-c" in args:
            i = args.index("-c")
            if i + 1 < len(args):
                return self._nested_bash(args[i + 1], ctx, base, detail, depth), base, False
        if order == "eval":
            eval_text = " ".join(args)
            d = self._nested_bash(eval_text, ctx, base, detail, depth)
            # eval and source run in the shell itself: a cd in them changes the folder
            # .
            there_is_cd = re.search(r"\b(cd|pushd|popd|source)\b", eval_text)
            return d, UNRESOLVED_CD if there_is_cd else base, False
        if order in ("source", "."):
            return None, UNRESOLVED_CD, False
        worst: Decision | None = None
        if order in p.commands_approval:
            worst = Decision(APPROVAL, f"command that needs approval: {order}", "bash", detail)
        # Redirections: the next element is the destination.
        goals: list[str] = []
        clean: list[str] = []
        next_is_destination = False
        for w in args:
            if w in (">", ">>", ">|"):
                next_is_destination = True
                continue
            if w in ("<", "<<", "<<<"):
                continue
            if next_is_destination:
                if w not in ("/dev/null",) and not w.startswith("&"):
                    goals.append(w)
                next_is_destination = False
                continue
            clean.append(w)
        only_read = order in READ_ONLY_COMMANDS and not goals
        no_options = [w for w in clean if not w.startswith("-")]
        if order in TEXT_COMMANDS:
            candidates: list[str] = []
        elif order in COMMANDS_WITH_PATTERN and no_options:
            candidates = no_options[1:]
        else:
            candidates = [
                w.split("=", 1)[1] if w.startswith("-") and "=" in w else w for w in clean
            ]
        # Reads.
        for token in candidates:
            text = os.path.expandvars(token)
            if not text or "://" in text or text.startswith("-") or text == "/dev/null":
                continue
            if base == UNRESOLVED_CD and not os.path.isabs(os.path.expanduser(text)):
                continue  # it isn't known what it reads; the sandbox protects the secrets
            for r in self._expand(text, base):
                resolved = self.resolve(r, base)
                if self._pattern_home(resolved, self._forbidden):
                    return (
                        Decision(
                            FORBIDDEN, f"access to a protected path: {resolved}", "bash", detail
                        ),
                        base,
                        False,
                    )
                if self._looks_like_path(text, base):
                    d = self.classify_read(r, base)
                    if worst is None or d.level > worst.level:
                        worst = Decision(d.level, d.reason, "bash", detail) if d.level else worst
        # Writes.
        is_write = (
            order in WRITE_COMMANDS
            or (order in ("sed", "perl") and any(w.startswith("-i") for w in clean))
            or (order == "find" and any(w in DANGEROUS_FIND for w in clean))
        )
        if is_write:
            only_read = False
            destinations = [os.path.expandvars(w) for w in no_options]
            if order in ("sed", "perl") and destinations:
                destinations = destinations[1:]
            if order in ("cp", "install", "rsync") and len(destinations) >= 2:
                destinations = [destinations[-1]]
            elif order == "dd":
                destinations = [w[3:] for w in clean if w.startswith("of=")]
            elif order == "find":
                destinations = destinations[:1]
            goals += destinations
        # curl and wget write to the output file they are given.
        if order in ("curl", "wget"):
            for i, w in enumerate(clean):
                if w in ("-o", "--output", "-O", "--output-document") and i + 1 < len(clean):
                    if not (order == "curl" and w == "-O") and clean[i + 1] != "/dev/null":
                        goals.append(clean[i + 1])
                elif (
                    w.startswith(("--output=", "--output-document="))
                    and w.split("=", 1)[1] != "/dev/null"
                ):
                    goals.append(w.split("=", 1)[1])
        for goal in goals:
            expanded = os.path.expandvars(goal)
            if base == UNRESOLVED_CD and not os.path.isabs(os.path.expanduser(expanded)):
                return Decision(FORBIDDEN, CD_REASON, "bash", detail), base, False
            if _unresolved(expanded):
                return Decision(FORBIDDEN, VARIABLE_REASON, "bash", detail), base, False
            for r in self._expand(expanded, base):
                d = self.classify_write(r, ctx, base, deleted=order in DELETE_COMMANDS)
                if d.level == FORBIDDEN:
                    return Decision(FORBIDDEN, d.reason, "bash", detail), base, False
                if worst is None or d.level > worst.level:
                    worst = Decision(d.level, d.reason, "bash", detail) if d.level else worst
        if ctx.agent == "reader" and order in NETWORK_COMMANDS:
            return (
                Decision(FORBIDDEN, "the reader subagent doesn't use the live web", "bash", detail),
                base,
                False,
            )
        return worst, base, only_read

    def _after_wrapper(
        self,
        wrapper: str,
        rest: list[str],
        ctx: CallContext,
        base: Path,
        detail: str,
        depth: int,
    ) -> tuple[Decision | None, Path, bool]:
        """A command behind a wrapper with options (`env -u FOO rm ...`, `timeout -s KILL
        5 rm ...`): an option may carry its value in the next word, so the command that would
        start at each position is analyzed and the worst one counts. `env -C` changes the folder
        and `env -S` carries a command inside."""
        if depth > 4:
            return (
                Decision(APPROVAL, "nested command is too complex", "bash", detail),
                base,
                False,
            )
        bases, strings = [base], []
        if wrapper == "env":
            for i, w in enumerate(rest):
                next_step = rest[i + 1] if i + 1 < len(rest) else ""
                for short, long, destination in (
                    ("-C", "--chdir", bases),
                    ("-S", "--split-string", strings),
                ):
                    value = None
                    if w in (short, long):
                        value = next_step
                    elif w.startswith(long + "="):
                        value = w.split("=", 1)[1]
                    elif w.startswith(short) and len(w) > 2 and not w.startswith("--"):
                        value = w[2:]
                    if value:
                        destination.append(
                            self._folder_after_cd("cd", [value], base, detail)
                            if destination is bases
                            else value
                        )
        worst: Decision | None = None
        for b in bases:
            for i in range(len(rest)):
                d, _, _ = self._simple_bash(rest[i:], ctx, b, detail, depth + 1)
                if d is not None and (worst is None or d.level > worst.level):
                    worst = d
                if worst is not None and worst.level >= FORBIDDEN:
                    return worst, base, False
        for string in strings:
            d = self._nested_bash(string, ctx, base, detail, depth)
            if d is not None and (worst is None or d.level > worst.level):
                worst = d
        return worst, base, False

    def _folder_after_cd(self, order: str, args: list[str], base: Path, detail: str) -> Path:
        """The folder the command ends up in after `cd`, `pushd` or `popd`, or UNRESOLVED_CD if
        it can't be known: `cd -`, `~-`, odd options, variables, `popd` or CDPATH."""
        rest = [a for a in args if a not in ("-P", "-L", "-e", "-@", "--")]
        if order == "popd" or re.search(r"\b(CDPATH|cdable_vars)\b", detail):
            return UNRESOLVED_CD
        if rest and rest[0].startswith(("-", "+", "~-", "~+")):
            return UNRESOLVED_CD
        destination = os.path.expandvars(rest[0]) if rest else "~"
        absolute = os.path.isabs(os.path.expanduser(destination))
        if _unresolved(destination) or (base == UNRESOLVED_CD and not absolute):
            return UNRESOLVED_CD
        return self.resolve(destination, base)

    def _nested_bash(
        self, command: str, ctx: CallContext, base: Path, detail: str, depth: int
    ) -> Decision | None:
        if depth > 4:
            return Decision(APPROVAL, "nested command is too complex", "bash", detail)
        d = self._analyze_bash(command, ctx, base, detail, depth + 1)[0]
        return d if d.level > FREE else None

    def _analyze_bash(
        self, command: str, ctx: CallContext, base: Path, detail: str, depth: int = 0
    ) -> tuple[Decision, bool]:
        no_bodies, bodies = self._remove_heredocs(command)
        worst = Decision(FREE, "command inside agent/", "bash", detail)
        only_read = True

        def consider(d: Decision | None) -> bool:
            nonlocal worst
            if d is not None and d.level > worst.level:
                worst = d
            return worst.level >= FORBIDDEN

        # Heredocs passed to a shell are commands; the rest are data.
        for line, body in bodies:
            first = (self._words(line.split("<<")[0]) or [[""]])[-1]
            is_shell = bool(first) and os.path.basename(first[0]) in SHELLS
            if is_shell and consider(self._nested_bash(body, ctx, base, detail, depth)):
                return worst, False
        for replacement in self._replacements(no_bodies):
            if consider(self._nested_bash(replacement, ctx, base, detail, depth)):
                return worst, False
        # The policy doesn't know whether a `cd` actually happened (it may fail, be in a
        # subshell or after `||`): after one, each command is checked in the new folder and
        # in the previous ones, and if only a previous one forbids it, the rejection asks
        # for the full path.
        bases = [base]
        for words in self._words(no_bodies):
            following: list[Path] = []
            for i, current in enumerate(bases):
                d, new, reading = self._simple_bash(words, ctx, current, detail, depth)
                only_read = only_read and reading
                if i > 0 and d is not None and d.level >= FORBIDDEN:
                    d = Decision(FORBIDDEN, CD_REASON, "bash", detail)
                if consider(d):
                    return worst, False
                following += [new, current] if new != current else [current]
            bases = list(dict.fromkeys(following))
            if len(bases) > 8:
                bases = [UNRESOLVED_CD]
        return worst, only_read

    def _expand(self, path: str, base: Path) -> list[str]:
        if any(c in path for c in "*?["):
            pattern = path if os.path.isabs(os.path.expanduser(path)) else str(base / path)
            found = glob.glob(os.path.expanduser(pattern))
            return found or [path]
        return [path]

    def classify_bash(self, command: str, ctx: CallContext) -> Decision:
        """Classifies a Bash command, forbidding the panel and forbidden patterns."""
        detail = f"Run: {command}"
        if self._re_panel.search(command):
            return Decision(FORBIDDEN, "the agent can't access the panel", "bash", detail)
        no_bodies, _ = self._remove_heredocs(command)
        text = self._without_quotes(no_bodies)
        for pattern in self._forbidden_bash:
            if m := pattern.search(text):
                return Decision(
                    FORBIDDEN, f'forbidden command: "{m.group(0).strip()}"', "bash", detail
                )
        level, only_read = self._analyze_bash(command, ctx, self.agent, detail)
        if level.level >= FORBIDDEN:
            return level
        if ctx.phase == "plan" and not only_read:
            return Decision(
                FORBIDDEN, "plan phase: only read-only commands are allowed", "bash", detail
            )
        for pattern in self._bash_approval:
            if m := pattern.search(text):
                return Decision(
                    max(APPROVAL, level.level),
                    f'command that needs approval: "{m.group(0).strip()}"',
                    "bash",
                    detail,
                )
        if level.level < NOTICE:
            for pattern in self._bash_notice:
                if pattern.search(text):
                    return Decision(NOTICE, "installing packages inside a project", "bash", detail)
        return level

    # --- URL -------------------------------------------------------------------------

    def _is_panel(self, url: str) -> bool:
        host, port, _ = _domain(url)
        if _is_tailscale(host):
            return True  # the panel over Tailscale, or anything else on that network
        return host in ("127.0.0.1", "localhost", "0.0.0.0", "::1") and port == self.panel_port

    @staticmethod
    def is_local(url: str) -> bool:
        """Returns whether a URL points to a local file or host."""
        if url.startswith(("/", "~")):
            return True  # Mac file path
        host, _, schema = _domain(url)
        return (
            schema == "file"
            or host in ("127.0.0.1", "localhost", "0.0.0.0", "::1")
            or host.endswith(".localhost")
        )

    def classify_url_chrome(self, url: str, ctx: CallContext | None = None) -> Decision:
        """Classifies a URL for the real Chrome, forbidding disallowed schemes and domains."""
        if self._is_panel(url):
            return Decision(FORBIDDEN, "the agent can't open the panel", "chrome")
        host, _, schema = _domain(url)
        if schema in ("chrome", "chrome-extension", "about", "javascript", "data") and url not in (
            "about:blank",
        ):
            return Decision(FORBIDDEN, f"scheme not allowed in your Chrome: {schema}", "chrome")
        if schema in ("http", "https") and not host:
            # An address without a domain can't be checked against the rules.
            return Decision(FORBIDDEN, f"address without a domain: {url[:100]}", "chrome")
        p = self.config.permissions.chrome
        if any(_matches_domain(host, d) for d in p.forbidden_domains):
            return Decision(FORBIDDEN, f"forbidden domain in your Chrome: {host}", "chrome")
        forbidden = self.forbidden_page(url)
        if forbidden is not None:
            return forbidden
        if any(_matches_domain(host, d) for d in p.domains_only_with_approval):
            return Decision(
                APPROVAL,
                f"email and calendar are handled through their API, not from the browser ({host})",
                "chrome",
                detail=f"Open {url} in your Chrome",
            )
        if any(_matches_domain(host, d) for d in p.domains_with_account):
            return Decision(NOTICE, f"looking at a site where you're signed in: {host}", "chrome")
        return Decision(FREE, "public page", "chrome")

    # --- forbidden pages in real Chrome ---------------------------------------------------

    def is_forbidden_page(self, url: str) -> bool:
        """Returns whether a URL matches one of the forbidden page patterns."""
        host, path = _page(url)
        key = host + path
        return any(fnmatch.fnmatchcase(key, p) for p in self._forbidden_pages)

    def forbidden_page(self, url: str | None) -> Decision | None:
        """Payment, billing, tax or account settings pages are forbidden
        in any task (`chrome.forbidden_pages` in permissions.yaml)."""
        if not url or url in ("about:blank", "back", "forward"):
            return None
        if self.is_forbidden_page(url):
            host, _ = _page(url)
            return Decision(
                FORBIDDEN,
                f"payment, billing, tax or account settings page ({host}): it is never opened",
                "chrome",
            )
        return None

    def forbidden_read(self, url: str | None, ctx: CallContext) -> Decision | None:
        """Whether the content of a real Chrome page can't be seen. The hook checks it
        after reading, with the address the reading itself returns: the tab's may
        be unknown (new session) or out of date (a click that was still navigating)."""
        if not url or url.startswith(UNKNOWN_PAGE) or url in ("about:blank",):
            return None
        if self._is_panel(url):
            return Decision(FORBIDDEN, "the agent can't open the panel", "chrome")
        host, _, _ = _domain(url)
        if any(_matches_domain(host, d) for d in self.config.permissions.chrome.forbidden_domains):
            return Decision(FORBIDDEN, f"forbidden domain in your Chrome: {host}", "chrome")
        return self.forbidden_page(url)

    def _navigation_limits(
        self, name: str, entry: dict[str, Any], ctx: CallContext
    ) -> Decision | None:
        """Limits of each task with real Chrome: browser actions
        and token budget."""
        n = nav.actions(name, entry)
        if n and ctx.token_cap and ctx.tokens_task >= self._token_notice * ctx.token_cap:
            # Before the budget cut, the agent stops browsing and delivers what it has:
            # so half-done work isn't lost.
            return Decision(
                FORBIDDEN,
                f"the task has already used {_millions(ctx.tokens_task)} of its "
                f"{_millions(ctx.token_cap)} token budget: don't open more pages; deliver with "
                "what you have and say what was left unchecked",
                "chrome",
            )
        maximum = self._max_actions
        if n and ctx.browser_actions + n > maximum:
            remain = maximum - ctx.browser_actions
            if remain > 0:
                reason = (
                    f"this call has {n} browser actions and only {remain} of the {maximum} allowed "
                    "per task are left"
                )
            else:
                reason = (
                    f"all {maximum} browser actions allowed per task have been used: deliver with "
                    "what you have and say what was left unchecked"
                )
            return Decision(FORBIDDEN, reason, "chrome")
        return None

    def _classify_browser_url(self, url: str) -> Decision:
        """Browser URL without a session (Playwright)."""
        if self._is_panel(url):
            return Decision(FORBIDDEN, "the agent can't access the panel", "browser")
        if url.startswith(("file://", "/", "~")):
            d = self.classify_read(url.removeprefix("file://"))
            return (
                Decision(d.level, d.reason, "browser")
                if d.level > FREE
                else Decision(FREE, "local file", "browser")
            )
        host, _, _ = _domain(url)
        p = self.config.permissions.chrome
        if any(_matches_domain(host, d) for d in p.domains_only_with_approval):
            return Decision(
                APPROVAL,
                f"email and calendar are handled through their API, not from the browser ({host})",
                "browser",
                detail=f"Open {url} in the signed-out browser",
            )
        return Decision(FREE, "web page", "browser")

    def classify_network_sandbox(self, host: str) -> Decision:
        """Network connection of a command inside the sandbox to a non-preauthorized host."""
        host = host.lower().strip().rstrip(".")
        p = self.config.permissions.chrome
        if not host:
            return Decision(APPROVAL, "network connection to an unknown destination", "network")
        if any(_matches_domain(host, d) for d in p.forbidden_domains):
            return Decision(FORBIDDEN, f"connection to a forbidden domain: {host}", "network")
        if any(_matches_domain(host, d) for d in p.domains_only_with_approval):
            return Decision(
                APPROVAL,
                f"connection to {host} from a command",
                "network",
                detail=f"Allow a command to connect to {host}",
            )
        return Decision(FREE, f"network connection to {host}", "network")

    # --- general evaluation ------------------------------------------------------------

    def evaluate(self, tool: str, entry: dict[str, Any], ctx: CallContext) -> Decision | None:
        """Returns the decision, or None if the call must always go to can_use_tool."""
        return self._evaluate(tool, entry, ctx)

    def _evaluate(self, tool: str, entry: dict[str, Any], ctx: CallContext) -> Decision | None:
        if tool == "AskUserQuestion":
            return None
        if tool == "SandboxNetworkAccess":
            return self.classify_network_sandbox(str(entry.get("host", "")))
        if ctx.phase == "plan" and not self._allowed_in_plan(tool, entry):
            return Decision(FORBIDDEN, "plan phase: you can't take actions yet", "plan")

        if tool in BUILT_IN_FREE:
            return Decision(FREE, "tool with no external effects")
        if tool in ("Agent", "Task"):
            kind = entry.get("subagent_type")
            if kind not in SUBAGENTS:
                return Decision(FORBIDDEN, f"unknown subagent: {kind}", "agent")
            if kind in ONLY_ORCHESTRATOR and ctx.agent is not None:
                return Decision(FORBIDDEN, "only the orchestrator delegates", "agent")
            return Decision(FREE, f"delegating to {kind}", "agent")
        if tool in PATH_TOOLS and any(
            ambiguous_path(entry.get(field)) for field in ("file_path", "notebook_path", "path")
        ):
            return Decision(FORBIDDEN, AMBIGUOUS_PATH_REASON, "file")
        if tool in ("Read", "NotebookRead"):
            return self.classify_read(entry.get("file_path") or entry.get("notebook_path", ""))
        if tool in ("Glob", "Grep"):
            path = entry.get("path") or ""
            pattern = entry.get("pattern", "") if tool == "Glob" else ""
            if tool == "Glob" and ".." in Path(str(pattern)).parts:
                return Decision(FORBIDDEN, GLOB_UPLOAD_REASON, "file")
            if not path and pattern and os.path.isabs(os.path.expanduser(pattern)):
                path = os.path.expanduser(pattern).split("*")[0] or "/"
            decision = self.classify_read(path or ".")
            if tool == "Glob" and pattern and not os.path.isabs(os.path.expanduser(pattern)):
                # The folder the pattern reaches also counts: “data/**” from the
                # root would list the names of your documents.
                fixed, _rest = _fixed_prefix(str(pattern))
                if fixed:
                    arrives = self.classify_read(fixed, base=self.resolve(path or "."))
                    if arrives.level > decision.level:
                        decision = arrives
            return decision
        if tool in ("Write", "Edit", "MultiEdit", "NotebookEdit"):
            path = entry.get("file_path") or entry.get("notebook_path", "")
            return self.classify_write(path, ctx)
        if tool == "Bash":
            return self.classify_bash(str(entry.get("command", "")), ctx)
        if tool == "WebFetch":
            url = str(entry.get("url", ""))
            if self._is_panel(url):
                return Decision(FORBIDDEN, "the agent can't access the panel", "web")
            if ctx.agent == "reader":
                return Decision(FORBIDDEN, "the reader subagent doesn't use the live web", "web")
            return Decision(FREE, "reading a web page", "web")
        if tool.startswith("mcp__aide__"):
            return self._evaluate_own(tool.removeprefix("mcp__aide__"), entry, ctx)
        if tool.startswith("mcp__claude-in-chrome__"):
            name = tool.removeprefix("mcp__claude-in-chrome__")
            return self._navigation_limits(name, entry, ctx) or self._evaluate_chrome(
                name, entry, ctx
            )
        if tool.startswith("mcp__"):
            return self._evaluate_external(tool)
        return Decision(
            APPROVAL,
            f"tool with no declared level: {tool}",
            "general",
            detail=f"Use {tool}",
        )

    def _allowed_in_plan(self, tool: str, entry: dict[str, Any]) -> bool:
        if tool in (
            "Read",
            "Glob",
            "Grep",
            "WebSearch",
            "WebFetch",
            "Skill",
            "ToolSearch",
            "TaskCreate",
            "TaskUpdate",
            "TaskGet",
            "TaskList",
            "TodoWrite",
            "Bash",
        ):
            return True  # Bash is filtered afterwards with the list of read commands
        if tool.startswith("mcp__aide__"):
            return tool.removeprefix("mcp__aide__") in OWN_READ
        return False

    def _evaluate_own(self, name: str, entry: dict[str, Any], ctx: CallContext) -> Decision:
        levels = self.config.permissions.own_tools
        if name not in levels:
            return Decision(APPROVAL, f"own tool with no level: {name}", "general")
        base = Decision(levels[name], f"configured level for {name}", name)
        if name in ("browser_read", "browser_screenshot", "browser_test"):
            url = str(entry.get("url") or entry.get("url_or_local_path") or "")
            steps = [p for p in entry.get("steps") or [] if isinstance(p, dict)]
            # The initial URL and those of browser_test's “ir” steps are evaluated (and,
            # out of caution, those of any step with an English navigate-type name).
            urls = [url] + [
                str(p.get("value") or p.get("selector") or "")
                for p in steps
                if str(p.get("action", "")).lower() in NAVIGATE_STEPS
            ]
            worst = base
            for destination in urls:
                d = self._classify_browser_url(destination)
                if d.level > worst.level:
                    worst = d
            if worst.level >= FORBIDDEN:
                return worst
            if name == "browser_test" and not all(self.is_local(u) for u in urls):
                interactive = any(
                    str(p.get("action", "")).lower() in INTERACTIVE_STEPS for p in steps
                )
                if interactive and worst.level < APPROVAL:
                    worst = Decision(
                        APPROVAL,
                        "interacting with an external website",
                        "browser",
                        detail=f"Fill in or press on {url}",
                    )
            return worst
        if name == "calendar_create_event" and entry.get("guests"):
            return Decision(APPROVAL, "event with guests", "event")
        if name in ("telegram_send_file",):
            if ambiguous_path(entry.get("path")):
                return Decision(FORBIDDEN, AMBIGUOUS_PATH_REASON, "telegram")
            d = self.classify_read(str(entry.get("path", "")))
            if not _inside(self.resolve(str(entry.get("path", ""))), self.agent):
                return Decision(FORBIDDEN, "only files from agent/ can be sent", "telegram")
            if d.level == FORBIDDEN:
                return d
        if name == "gmail_create_draft":
            for attachment in entry.get("attachments") or []:
                if ambiguous_path(attachment):
                    return Decision(FORBIDDEN, AMBIGUOUS_PATH_REASON, "email")
                if not _inside(self.resolve(str(attachment)), self.agent):
                    return Decision(FORBIDDEN, f"attachment outside agent/: {attachment}", "email")
                if self.classify_read(str(attachment)).level == FORBIDDEN:
                    # A .env, key or token inside agent/ isn't attached either
                    # .
                    return Decision(FORBIDDEN, f"protected attachment: {attachment}", "email")
        return base

    def _evaluate_chrome(self, name: str, entry: dict[str, Any], ctx: CallContext) -> Decision:
        if ctx.agent not in (None, "navigator"):
            return Decision(
                FORBIDDEN,
                "only the orchestrator and the navigator subagent use your Chrome",
                "chrome",
            )
        tab = entry.get("tabId")
        url_tab = ctx.urls_chrome.get(str(tab)) if tab is not None else None
        if name not in CHROME_NO_PAGE and url_tab and not str(url_tab).startswith(UNKNOWN_PAGE):
            # If it is already known which page the tab is on, its rules also apply to
            # reading it or acting on it (for example, if a click led to a forbidden
            # page).
            d = self.forbidden_page(url_tab)
            if d is not None:
                return d
        if name in ("navigate", "tabs_create_mcp"):
            url = str(entry.get("url") or "")
            if url in ("back", "forward"):
                return Decision(FREE, "back or forward in the tab", "chrome")
            return (
                self.classify_url_chrome(url, ctx) if url else Decision(FREE, "new tab", "chrome")
            )
        if name in CHROME_READ:
            return Decision(FREE, "reading in your Chrome", "chrome")
        if name == "form_input":
            element = ctx.chrome_elements.get(
                chrome_key(entry.get("tabId"), str(entry.get("ref", "")))
            )
            if is_password(element):
                return Decision(FORBIDDEN, "passwords are never typed", "chrome")
            return Decision(NOTICE, "filling in a field without sending", "chrome")
        if name == "computer":
            action = str(entry.get("action", "")).lower()
            if action in COMPUTER_FREE:
                return Decision(FREE, "reading the page", "chrome")
            if action == "type":
                if ctx.password_focus:
                    return Decision(FORBIDDEN, "passwords are never typed", "chrome")
                if re.search(r"[\r\n]", str(entry.get("text", ""))):
                    return self._send_key(entry, "typing a line break can submit a form")
                return Decision(NOTICE, "typing in a field without sending", "chrome")
            if action == "key":
                if _send_keys(str(entry.get("text", ""))):
                    return self._send_key(
                        entry,
                        "pressing Enter or the space bar can submit a form or activate the "
                        "focused button",
                    )
                return Decision(NOTICE, "pressing a key", "chrome")
            if action in COMPUTER_CLICKS:
                return self._classify_click(entry, ctx)
            return Decision(APPROVAL, f"unclassified browser action: {action}", "chrome")
        if name in ("javascript_tool", "shortcuts_execute"):
            return Decision(
                APPROVAL,
                "running code or shortcuts on the page",
                "chrome_send",
                detail=f"{name}: {str(entry)[:300]}",
            )
        if name in ("file_upload", "upload_image"):
            paths = entry.get("paths") or []
            paths = paths if isinstance(paths, list) else [paths]
            if any(not os.path.isabs(os.path.expanduser(str(r))) for r in paths):
                return Decision(FORBIDDEN, "to upload a file, use its absolute path", "chrome")
            return Decision(
                APPROVAL,
                "uploading files to a website",
                "chrome_send",
                detail=f"Upload files: {str(entry)[:300]}",
            )
        if name == "browser_batch":
            # Each action is evaluated with what the previous ones leave: after
            # navigating, the tab is on another page, and after navigating or pressing,
            # its refs are no longer valid.
            elements = dict(ctx.chrome_elements)
            urls = dict(ctx.urls_chrome)
            worst = Decision(FREE, "batch of browser actions", "chrome")
            pending: list[tuple[str, Decision]] = []
            for action in entry.get("actions") or []:
                if not isinstance(action, dict):
                    continue
                internal = str(action.get("tool") or action.get("name") or "")
                internal = internal.removeprefix("mcp__claude-in-chrome__")
                data = action.get("input") or action.get("args") or action
                data = data if isinstance(data, dict) else {}
                d = self._evaluate_chrome(
                    internal,
                    data,
                    replace(ctx, chrome_elements=elements, urls_chrome=urls),
                )
                if d.level >= FORBIDDEN:
                    return d
                if d.level == APPROVAL:
                    pending.append((internal, d))
                if d.level > worst.level:
                    worst = d
                self._simulate_action(internal, data, elements, urls)
            if any(d.kind == "chrome_send" for _, d in pending):
                # The screenshot from before the batch wouldn't show what is being
                # sent.
                return Decision(
                    FORBIDDEN,
                    "pressing, sending, uploading files or running code in your Chrome is done "
                    "outside a batch and after a screenshot, so the user sees what will be sent",
                    "chrome",
                )
            if pending:
                # The user sees everything that needs their permission, not just the
                # first action.
                lines = [f"- {d.detail or f'{n}: {d.reason}'}" for n, d in pending]
                return Decision(
                    APPROVAL,
                    "; ".join(dict.fromkeys(d.reason for _, d in pending)),
                    pending[0][1].kind,
                    detail="Batch of actions in your Chrome:\n" + "\n".join(lines),
                )
            return worst
        return Decision(APPROVAL, f"unclassified Chrome tool: {name}", "chrome")

    @staticmethod
    def _simulate_action(
        name: str, data: dict[str, Any], elements: dict[str, Any], urls: dict[str, str]
    ) -> None:
        """What a batch action changes, to evaluate the following ones, as the
        hook will do afterwards (hooks.change_in_page): with no known tab, all of them."""
        action = str(data.get("action", "")).lower()
        if not (
            name in ("navigate", "tabs_create_mcp", "tabs_close_mcp", "switch_browser")
            or (name == "computer" and (action in COMPUTER_CLICKS or action == "key"))
        ):
            return
        tab = data.get("tabId")
        if tab is None or name == "switch_browser":
            elements.clear()
            urls.clear()
            return
        forget_tab(elements, tab)
        url = str(data.get("url") or "")
        if name == "navigate" and url and url not in ("back", "forward"):
            urls[str(tab)] = url
        else:
            # After a click, a key press or going back, it isn't known which page the
            # tab ends up on; the page it came from is saved.
            previous = urls.get(str(tab), "").removeprefix(UNKNOWN_PAGE).strip()
            urls[str(tab)] = f"{UNKNOWN_PAGE} {previous}".strip()

    @staticmethod
    def _send_key(entry: dict[str, Any], reason: str) -> Decision:
        return Decision(APPROVAL, reason, "chrome_send", detail=f"Press {entry.get('text')}")

    def _classify_click(self, entry: dict[str, Any], ctx: CallContext) -> Decision:
        ref = str(entry.get("ref", ""))
        # A drag or a click with coordinates acts where the coordinates say, not on the ref.
        coordinates = entry.get("coordinate") or entry.get("start_coordinate")
        drag = str(entry.get("action", "")).lower() == "left_click_drag"
        element = (
            ctx.chrome_elements.get(chrome_key(entry.get("tabId"), ref))
            if ref and not coordinates and not drag
            else None
        )
        if not element:
            return Decision(
                APPROVAL,
                "click on an element that can't be identified",
                "chrome_send",
                detail=f"Click on {coordinates or ref}",
            )
        text = " ".join(element.get(k, "") for k in ("name", "role", "kind")).lower()
        if element.get("kind", "").lower() == "submit" or any(
            re.search(rf"\b{re.escape(p)}\b", text) for p in self._send_words
        ):
            return Decision(
                APPROVAL,
                f"pressing a button that sends or confirms ({element.get('name', '')})",
                "chrome_send",
                detail=f'Press "{element.get("name", ref)}"',
            )
        return Decision(
            FREE,
            f'click on {element.get("role", "element")} "{element.get("name", "")}"',
            "chrome",
        )

    def _evaluate_external(self, tool: str) -> Decision:
        parts = tool.split("__", 2)
        server = parts[1] if len(parts) > 1 else ""
        name = parts[2] if len(parts) > 2 else ""
        data = self.config.settings.mcp_servers.get(server)
        if data is None:
            level = self.config.permissions.default_external_mcp_level
        else:
            level = data.levels.get(name, data.default_level)
        return Decision(level, f"{server} tool", "mcp", detail=f"Use {name} from {server}")
