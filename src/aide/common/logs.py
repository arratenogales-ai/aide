"""Rotating log with a filter that redacts secrets."""

from __future__ import annotations

import logging
import re
import sys
import threading
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path

DRAFTED = "[REDACTED]"

_PATTERNS = [
    re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b"),  # Telegram bot token
    re.compile(r"sk-ant-[A-Za-z0-9_\-]{8,}"),  # Anthropic keys and tokens
    re.compile(r"ya29\.[A-Za-z0-9_\-\.]{10,}"),  # Google access token
    re.compile(r"\b1//[A-Za-z0-9_\-]{20,}"),  # Google refresh token
    re.compile(r"GOCSPX-[A-Za-z0-9_\-]{10,}"),  # Google OAuth client secret
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/\-]{12,}=*"),
    re.compile(
        r"(?i)\b(password|passwd|contrase(?:ñ|n)a|secret|client_secret|token|api[_-]?key|"
        r"refresh_token|access_token)\b(\s*[\"']?\s*[=:]\s*[\"']?)([^\s\"',}]+)"
    ),
]

_known_secrets: set[str] = set()
_lock = threading.Lock()


def log_secret(value: str | None) -> None:
    """Adds a specific value (for example, the bot token) to the redaction list."""
    if value and len(value) >= 8:
        with _lock:
            _known_secrets.add(value)


def draft(text: str) -> str:
    """Replaces known secrets and secret patterns in text with a redaction marker."""
    if not text:
        return text
    with _lock:
        known = sorted(_known_secrets, key=len, reverse=True)
    for value in known:
        text = text.replace(value, DRAFTED)
    for pattern in _PATTERNS:
        if pattern.groups >= 3:
            text = pattern.sub(lambda m: f"{m.group(1)}{m.group(2)}{DRAFTED}", text)
        else:
            text = pattern.sub(DRAFTED, text)
    return text


class SecretFilter(logging.Filter):
    """A logging filter that redacts secrets from messages and exception text."""

    def filter(self, record: logging.LogRecord) -> bool:
        """Redacts secrets in a log record message and exception text, always keeping it."""
        try:
            message = record.getMessage()
        except Exception:
            return True
        record.msg = draft(message)
        record.args = None
        if record.exc_info and record.exc_info[1] is not None:
            # The exception text is formatted later; it is redacted when formatting.
            record.exc_text = draft(logging.Formatter().formatException(record.exc_info))
            record.exc_info = None
        return True


_configured = False


def configure_log(folder: Path, level: int = logging.INFO, console: bool = True) -> None:
    """Sets up logging once, with a daily rotating file in the given folder."""
    global _configured
    root = logging.getLogger()
    if _configured:
        return
    folder.mkdir(parents=True, exist_ok=True)
    format_ = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    filter_ = SecretFilter()

    # One file per day (aide.log.YYYY-MM-DD), kept for 30 days.
    file = TimedRotatingFileHandler(
        folder / "aide.log", when="midnight", backupCount=30, encoding="utf-8"
    )
    file.setFormatter(format_)
    file.addFilter(filter_)
    root.addHandler(file)

    if console:
        output = logging.StreamHandler(sys.stderr)
        output.setFormatter(format_)
        output.addFilter(filter_)
        # Under launchd the error output goes to launchd.errors.log: only warnings and
        # errors there, so as not to repeat the full log, which is already in aide.log.
        if not sys.stderr.isatty():
            output.setLevel(logging.WARNING)
        root.addHandler(output)

    root.setLevel(level)
    for noisy in ("httpx", "httpcore", "telegram", "apscheduler", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    _configured = True
