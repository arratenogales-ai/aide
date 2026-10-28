"""Check before publishing the repository.

Walks the files that would go into the repository (those not ignored by the root .gitignore, without
needing git) and fails if it finds anything that should not leave:

- anything shaped like a token or key (Telegram, Google, Anthropic, private keys, AWS,
  GitHub);
- email addresses that are not from example domains;
- secrets files (.env, credentials.json, token.json, keys), databases, PDFs, Word documents, Excel
  sheets, presentations or audio;
- images with metadata (EXIF, XMP or text in a PNG);
- files over 2 MB;
- absolute paths of a home folder (/Users/<name>, /home/<name>);
- mentions of authorship or attribution to an AI (co-author lines, “generated with...”) and session
  notes or transcripts. Technical uses of “Claude”, “Claude Code”, “Agent SDK” or `CLAUDE.md` do not
  count.

The exceptions (the made-up tokens in tests, or the addresses a test needs) go in
`publish-allowlist.txt`: one exact string per line, as it is found. Exits with 0 if all is
clean and with 1 if not.

    uv run python scripts/check_publish.py [folder]
"""

from __future__ import annotations

import argparse
import re
import struct
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

BYTES_LIMIT = 2 * 1024 * 1024
EXCEPTIONS_FILE = "publish-allowlist.txt"

SECRETS = {
    "Telegram bot token": r"\b\d{8,10}:[A-Za-z0-9_-]{30,}",
    "Google API key": r"\bAIza[0-9A-Za-z_-]{35}\b",
    "Google OAuth client secret": r"\bGOCSPX-[A-Za-z0-9_-]{20,}",
    "Google OAuth client id": r"\b\d{6,}-[a-z0-9]{32}\.apps\.googleusercontent\.com\b",
    "Google access token": r"\bya29\.[A-Za-z0-9_-]{20,}",
    "Google refresh token": r"(?<![\w/])1//0[A-Za-z0-9_-]{20,}",
    "Anthropic key or token": r"\bsk-ant-[A-Za-z0-9_-]{8,}",
    "private key": r"-----BEGIN (?:[A-Z]+ )*PRIVATE KEY(?: BLOCK)?-----",
    "AWS access key": r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b",
    "AWS secret key": r"(?i)aws_secret_access_key\s*[=:]\s*['\"]?[A-Za-z0-9/+=]{40}",
    "GitHub token": r"\b(?:gh[pousr]_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{40,})",
    "Slack token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
}
EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@((?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,})\b")
# Domains reserved for examples (RFC 2606 and RFC 6761) and their Spanish equivalent.
EXAMPLE_DOMAINS = ("example.com", "example.org", "example.net", "ejemplo.com", "ejemplo.es")
TLD_EXAMPLE = (".example", ".test", ".invalid", ".localhost")
PERSONAL_PATH = re.compile(
    r"(?:/Users/(?!Shared\b)[A-Za-z0-9._-]+|/home/[a-z_][a-z0-9_.-]*|[A-Z]:\\Users\\[^\\\s]+)"
)
# Authorship or attribution to an AI. Written so that this file does not find itself.
ATTRIBUTION = {
    "co-author line": r"(?i)\bco-?authored-?by\s*:",
    "AI attribution": r"(?i)\bgenerated\s+(?:with|by|using)\s+\[?(?:claude|chatgpt|an?\s+ai\b|ai\b|copilot)",
    # “Built with the Claude Agent SDK” is a technical use; a Claude authorship is not.
    "AI attribution (written/made by)": r"(?i)\b(?:written|made|created|built)\s+(?:with|by)\s+"
    r"(?:claude(?!\s+(?:agent\s+sdk|api|models?)\b)|chatgpt|an?\s+ai\b|copilot)",
    "note of machine authorship": r"(?i)\bai[- ]generated\b",
    "Anthropic no-reply address": r"(?i)noreply@anthropic\.com",
    "robot emoji": "\U0001f916",
    "AI attribution in Spanish": r"(?i)\b(?:generad[oa]|escrit[oa]|hech[oa]|cread[oa])\s+(?:con|por)\s+"
    r"(?:claude(?!\s+(?:agent\s+sdk|api)\b)|chatgpt|(?:una\s+|la\s+)?ia)\b",
}
ATTRIBUTION_RE = {name: re.compile(p) for name, p in ATTRIBUTION.items()}
SECRETS_RE = {name: re.compile(p) for name, p in SECRETS.items()}

FORBIDDEN_NAME = {
    ".env": "environment file with secrets",
    "credentials.json": "Google OAuth client file",
    "token.json": "OAuth token",
    ".credentials.json": "credentials",
}
FORBIDDEN_EXTENSIONS = {
    **dict.fromkeys((".db", ".sqlite", ".sqlite3", ".db-wal", ".db-shm"), "database"),
    ".pdf": "PDF",
    **dict.fromkeys((".doc", ".docx", ".odt", ".rtf", ".pages"), "word-processor document"),
    **dict.fromkeys((".xls", ".xlsx", ".xlsm", ".ods", ".numbers"), "spreadsheet"),
    **dict.fromkeys((".ppt", ".pptx", ".odp", ".key"), "presentation or key file"),
    **dict.fromkeys(
        (".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".flac", ".aac", ".aiff", ".amr"),
        "audio",
    ),
    **dict.fromkeys((".pem", ".p12", ".pfx"), "key or certificate"),
    ".jsonl": "session transcript",
}
# Session notes, transcripts and internal files of the developer.
INTERNAL_NAMES = re.compile(
    r"(?i)^(?:notas[_ -]?sesion.*|session[_ -]?notes.*|decisiones\.md|spec\.md|claude\.local\.md|"
    r"memory\.md|transcript.*\.(?:md|txt|json))$"
)
IMAGES = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".tif", ".tiff", ".heic")
PNG_METADATA = {b"tEXt", b"iTXt", b"zTXt", b"eXIf", b"tIME"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    reason: str

    def text(self) -> str:
        where = f"{self.path}:{self.line}" if self.line else self.path
        return f"{where}: {self.reason}"


# --- .gitignore ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Rule:
    pattern: re.Pattern[str]
    negated: bool
    only_folders: bool


def _glob_to_regex(glob: str) -> str:
    output, i = "", 0
    while i < len(glob):
        c = glob[i]
        if glob.startswith("**/", i):
            output += "(?:.*/)?"
            i += 3
        elif glob.startswith("/**", i) and i + 3 == len(glob):
            output += "/.*"
            i += 3
        elif glob.startswith("**", i):
            output += ".*"
            i += 2
        elif c == "*":
            output += "[^/]*"
            i += 1
        elif c == "?":
            output += "[^/]"
            i += 1
        elif c == "[":
            end = glob.find("]", i + 1)
            if end < 0:
                output += re.escape(c)
                i += 1
            else:
                kind = glob[i + 1 : end].replace("\\", "\\\\")
                if kind.startswith("!"):
                    kind = "^" + kind[1:]
                output += f"[{kind}]"
                i = end + 1
        elif c == "\\" and i + 1 < len(glob):
            output += re.escape(glob[i + 1])
            i += 2
        else:
            output += re.escape(c)
            i += 1
    return output


def read_gitignore(text: str) -> list[Rule]:
    rules = []
    for line in text.splitlines():
        line = line.rstrip()
        if not line or line.startswith("#"):
            continue
        negated = line.startswith("!")
        if negated:
            line = line[1:]
        only_folders = line.endswith("/")
        line = line.rstrip("/")
        anchored = "/" in line
        line = line.lstrip("/")
        body = _glob_to_regex(line)
        pattern = f"^{body}$" if anchored else f"^(?:.*/)?{body}$"
        rules.append(Rule(re.compile(pattern), negated, only_folders))
    return rules


def ignored(relative: str, is_folder: bool, rules: list[Rule]) -> bool:
    result = False
    for rule in rules:
        if rule.only_folders and not is_folder:
            continue
        if rule.pattern.match(relative):
            result = not rule.negated
    return result


def publishable_files(root: Path) -> Iterator[Path]:
    """The files that would go into the repository: whatever .gitignore (and .git) does not
    ignore."""
    gitignore = root / ".gitignore"
    rules = read_gitignore(gitignore.read_text(encoding="utf-8")) if gitignore.is_file() else []
    pending = [root]
    while pending:
        folder = pending.pop()
        for path in sorted(folder.iterdir()):
            relative = path.relative_to(root).as_posix()
            if path.name == ".git":
                continue
            is_folder = path.is_dir() and not path.is_symlink()
            if ignored(relative, is_folder, rules):
                continue
            if is_folder:
                pending.append(path)
            else:
                yield path


# --- checks -------------------------------------------------------------------------------


def example_domain(domain: str) -> bool:
    domain = domain.lower()
    return domain.endswith(TLD_EXAMPLE) or any(
        domain == d or domain.endswith("." + d) for d in EXAMPLE_DOMAINS
    )


def png_metadata(data: bytes) -> list[str]:
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        return []
    found, pos = [], 8
    while pos + 8 <= len(data):
        n, kind = struct.unpack(">I4s", data[pos : pos + 8])
        if kind in PNG_METADATA:
            found.append(kind.decode("latin-1"))
        if kind == b"IEND":
            break
        pos += 12 + n
    return found


def jpeg_metadata(data: bytes) -> list[str]:
    if data[:2] != b"\xff\xd8":
        return []
    found, pos = [], 2
    while pos + 4 <= len(data) and data[pos] == 0xFF:
        mark = data[pos + 1]
        if mark in (0xD9, 0xDA):  # end of image or start of the compressed image
            break
        n = struct.unpack(">H", data[pos + 2 : pos + 4])[0]
        segment = data[pos + 4 : pos + 2 + n]
        if mark == 0xE1 and segment.startswith(b"Exif"):
            found.append("EXIF")
        elif mark == 0xE1 and b"ns.adobe.com/xap" in segment[:64]:
            found.append("XMP")
        elif mark == 0xFE:
            found.append("comment")
        pos += 2 + n
    return found


def image_metadata(data: bytes) -> list[str]:
    found = png_metadata(data) + jpeg_metadata(data)
    if not found and (b"Exif\x00\x00" in data[:65536] or b"<x:xmpmeta" in data):
        found.append("EXIF or XMP")
    return found


def read_exceptions(root: Path) -> set[str]:
    path = root / EXCEPTIONS_FILE
    if not path.is_file():
        return set()
    return {
        line.strip()
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }


def review_text(relative: str, text: str, exceptions: set[str]) -> list[Finding]:
    findings = []
    is_exceptions = relative == EXCEPTIONS_FILE
    for n, line in enumerate(text.splitlines(), 1):
        if not is_exceptions:
            for name, pattern in SECRETS_RE.items():
                for m in pattern.finditer(line):
                    if m.group(0) not in exceptions:
                        findings.append(Finding(relative, n, f"looks like a {name}"))
        for m in EMAIL.finditer(line):
            if not example_domain(m.group(1)) and m.group(0) not in exceptions:
                findings.append(Finding(relative, n, f"email address {m.group(0)}"))
        for m in PERSONAL_PATH.finditer(line):
            if m.group(0) in exceptions:
                continue
            findings.append(Finding(relative, n, f"personal absolute path {m.group(0)}"))
        for name, pattern in ATTRIBUTION_RE.items():
            if pattern.search(line):
                findings.append(Finding(relative, n, name))
    return findings


def review_file(root: Path, path: Path, exceptions: set[str]) -> list[Finding]:
    relative = path.relative_to(root).as_posix()
    name = path.name
    findings: list[Finding] = []
    if name in FORBIDDEN_NAME or (name.startswith(".env.") and name != ".env.example"):
        findings.append(Finding(relative, 0, FORBIDDEN_NAME.get(name, "environment file")))
    extension = path.suffix.lower()
    if extension in FORBIDDEN_EXTENSIONS:
        findings.append(Finding(relative, 0, f"{FORBIDDEN_EXTENSIONS[extension]} file"))
    if INTERNAL_NAMES.match(name):
        findings.append(Finding(relative, 0, "session notes or internal development file"))
    if path.is_symlink():
        destination = str(path.readlink())
        if destination.startswith("/"):
            findings.append(
                Finding(relative, 0, f"symbolic link to an absolute path {destination}")
            )
        return findings
    size = path.stat().st_size
    if size > BYTES_LIMIT:
        findings.append(Finding(relative, 0, f"larger than 2 MB ({size / 1048576:.1f} MB)"))
        return findings
    data = path.read_bytes()
    if extension in IMAGES:
        for kind in image_metadata(data):
            findings.append(Finding(relative, 0, f"image metadata ({kind})"))
        return findings
    if b"\x00" in data[:8192]:
        return findings  # binary: not read as text
    text = data.decode("utf-8", errors="replace")
    return findings + review_text(relative, text, exceptions)


def review(root: Path) -> list[Finding]:
    root = root.resolve()
    exceptions = read_exceptions(root)
    findings: list[Finding] = []
    for path in publishable_files(root):
        findings += review_file(root, path, exceptions)
    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the files that would be published")
    parser.add_argument("folder", nargs="?", default=str(Path(__file__).resolve().parents[1]))
    args = parser.parse_args(argv)
    root = Path(args.folder)
    findings = review(root)
    total = sum(1 for _ in publishable_files(root.resolve()))
    if findings:
        for h in findings:
            print(h.text())
        print(f"\n{len(findings)} problem(s) in {total} files that would be published.")
        return 1
    print(f"Clean: {total} files checked, nothing that shouldn't be published.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
