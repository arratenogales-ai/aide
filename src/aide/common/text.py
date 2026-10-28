"""Text utilities shared by several parts of the program."""

from __future__ import annotations

import re
import unicodedata


def normalize(text: str | None) -> str:
    """Lowercase, no accents or punctuation: for comparing names and phrases."""
    t = unicodedata.normalize("NFKD", (text or "").replace("ß", "ss"))
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"[^a-z0-9]+", " ", t).strip()
