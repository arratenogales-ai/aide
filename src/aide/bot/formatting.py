"""Telegram HTML formatting with correct escaping."""

from __future__ import annotations

import html
import re

TELEGRAM_LIMIT = 4096


def esc(text: object) -> str:
    """Escapes text for Telegram's HTML mode."""
    return html.escape(str(text), quote=False)


def bold(text: object) -> str:
    return f"<b>{esc(text)}</b>"


def code(text: object) -> str:
    return f"<code>{esc(text)}</code>"


def block(text: object) -> str:
    return f"<pre>{esc(text)}</pre>"


def truncate(text: str, maximum: int) -> str:
    return text if len(text) <= maximum else text[: maximum - 3].rstrip() + "..."


def basic_markdown_to_html(text: str) -> str:
    """Converts the model's text (simple markdown) to safe Telegram HTML.

    Only bold, italics, inline code and code blocks; the rest is escaped.
    """
    parts = re.split(r"(```[\s\S]*?```)", text)
    output = []
    for part in parts:
        if part.startswith("```") and part.endswith("```"):
            content = part[3:-3]
            content = content.split("\n", 1)[1] if "\n" in content else content
            output.append(block(content.rstrip("\n")))
            continue
        e = esc(part)
        e = re.sub(r"`([^`\n]+)`", lambda m: f"<code>{m.group(1)}</code>", e)
        e = re.sub(r"\*\*([^*\n]+)\*\*", r"<b>\1</b>", e)
        e = re.sub(r"(?<![\w*])\*([^*\n]+)\*(?![\w*])", r"<i>\1</i>", e)
        e = re.sub(r"^#{1,6}\s*(.+)$", r"<b>\1</b>", e, flags=re.MULTILINE)
        output.append(e)
    return "".join(output)
