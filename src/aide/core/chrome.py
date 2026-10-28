"""Reading of the real Chrome's responses so the policy knows what is clicked.

The extension identifies elements with `ref_N`, different in each tab. Formats verified with the
real extension (F7 probe):

- read_page: `button "Send request" [ref_4] type="submit"`
- find:      `- ref_4: button "Send request" (submit) - Matches ...`
- screenshot: image block `{"type": "image", "source": {"type": "base64", ...}}`

The element map is stored per tab and emptied after navigating, clicking or pressing a key
in it: an old `ref` is never used to classify a click (approval would be requested).
"""

from __future__ import annotations

import base64
import re
from datetime import datetime
from pathlib import Path
from typing import Any

RE_READ_PAGE = re.compile(
    r'^\s*(?P<role>[A-Za-z][\w-]*)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?\s+\[(?P<ref>ref_\d+)\](?P<rest>[^\n]*)$',
    re.MULTILINE,
)
RE_FIND = re.compile(
    r'(?P<ref>ref_\d+):\s*(?P<role>[A-Za-z][\w-]*)(?:\s+"(?P<name>(?:[^"\\]|\\.)*)")?(?:\s+\((?P<kind>[\w-]+)\))?'
)
RE_KIND = re.compile(r'\btype="(?P<kind>[\w-]+)"')


def key(tab: Any, ref: str) -> str:
    return f"{tab}:{ref}"


def reply_elements(tool: str, text: str) -> dict[str, dict[str, str]]:
    """`ref` -> {role, name, type} from the output of read_page or find."""
    elements: dict[str, dict[str, str]] = {}
    if tool == "read_page":
        for m in RE_READ_PAGE.finditer(text):
            kind = RE_KIND.search(m.group("rest") or "")
            elements[m.group("ref")] = {
                "role": m.group("role").lower(),
                "name": m.group("name") or "",
                "kind": kind.group("kind").lower() if kind else "",
            }
    elif tool == "find":
        for m in RE_FIND.finditer(text):
            elements[m.group("ref")] = {
                "role": m.group("role").lower(),
                "name": m.group("name") or "",
                "kind": (m.group("kind") or "").lower(),
            }
    return elements


def forget_tab(elements: dict[str, dict[str, str]], tab: Any) -> None:
    """Removes the stored elements that belong to a tab."""
    prefix = f"{tab}:"
    for k in [k for k in elements if k.startswith(prefix)]:
        del elements[k]


def is_password(element: dict[str, str] | None) -> bool:
    """Returns whether an element looks like a password field."""
    if not element:
        return False
    return "password" in (element.get("kind", "") + " " + element.get("role", "")).lower()


def reply_images(reply: Any) -> list[tuple[bytes, str]]:
    """Images in a tool's response: (data, extension)."""
    blocks = (
        reply
        if isinstance(reply, list)
        else (reply or {}).get("content", [])
        if isinstance(reply, dict)
        else []
    )
    images = []
    for block in blocks:
        if not isinstance(block, dict) or block.get("type") != "image":
            continue
        source = block.get("source") or {}
        data = source.get("data") or block.get("data")
        kind = source.get("type_mean") or block.get("mimeType") or "image/png"
        if not data:
            continue
        try:
            images.append(
                (base64.b64decode(data), "jpg" if "jpeg" in kind else kind.split("/")[-1])
            )
        except (ValueError, TypeError):
            continue
    return images


def save_screenshot(task_folder: Path | None, data: bytes, extension: str) -> Path | None:
    """Real Chrome screenshots may show personal data: only in the task folder."""
    if task_folder is None:
        return None
    destination = (
        task_folder
        / "data"
        / "screenshots"
        / f"chrome-{datetime.now():%Y%m%d-%H%M%S-%f}.{extension}"
    )
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(data)
    return destination


RE_TAB = re.compile(r"^\s*•\s*tabId\s+(\d+):.*\(\"([^\"\s]*)\"\)\s*$", re.MULTILINE)


def urls_of_tabs(text: str) -> dict[str, str]:
    """Address of each tab, from the list the extension appends to each response.

    Format verified with the real extension on 2026-10-21:
    `  • tabId 1725492044: "Example Domain" ("https://example.com/")`.
    """
    return dict(RE_TAB.findall(text or ""))
