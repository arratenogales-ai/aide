"""Deterministic detection of instructions in external content.

External content (websites, emails, documents) is treated as data. This module decides
nothing by itself: it flags the session and the affected approval requests so
the user sees it visibly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

# Spanish and English. The lists only grow: a new pattern cannot remove an old one.
INSTRUCTION_PATTERNS = [
    r"ignor\w*\s+(todas?\s+)?(las|tus|sus|the|all|any|previous|prior|above|your|my|these|those)\s+(\w+\s+){0,2}(instrucciones|instructions|rules|reglas|guidelines|directions)",
    r"(disregard|forget)\s+(all\s+|any\s+|the\s+|your\s+)?(previous\s+|prior\s+)?(instructions|rules|context)",
    r"\b(disregard|forget)\s+(\w+\s+){0,2}(instructions|rules|guidelines|context|prompt)\b",
    r"\bforget\s+(everything|all)\s+(you|that|above|before|previous)",
    r"olvida\s+(todo|tus|las)\s+",
    r"(nuevas?|new)\s+(instrucciones|instructions)",
    r"(system\s+prompt|prompt\s+del\s+sistema|developer\s+message)",
    r"you\s+are\s+now\s+",
    r"(ahora\s+eres|actúa\s+como|act\s+as)\s+",
    r"\b(pretend\s+(to\s+be|you\s+are)|finge\s+(ser|que))\b",
    r"(env[ií]a|manda|reenv[ií]a|send|forward|email)\s+(un|una|el|este|esta|los|las|an?|the|this|these|all)?\s*"
    r"(correo|email|e-mail|mensaje|message|archivo|file|datos|data|contraseñ\w*|password|perfil|profile|credentials|credenciales|api\s+keys?|tokens?)",
    r"(ejecuta|run|execute)\s+(el\s+|este\s+|the\s+|this\s+)?(comando|command|script|código|code)",
    r"\b(curl|wget)\s+https?://",
    r"(IMPORTANTE|IMPORTANT|ATENCIÓN|ATTENTION)\s*[:!]\s*(para|to|for)?\s*(el|la|the)?\s*(agente|asistente|assistant|ai|ia|modelo|model|claude)",
    r"\b(important|attention|note|notice|importante|atención|aviso|nota)\b[^\n.]{0,20}\b(to|for|para)\s+(the\s+|el\s+|la\s+|los\s+)?"
    r"(ai|ia|llm|claude|chatgpt|gpt|ai\s+assistants?|ai\s+agents?|asistente\s+de\s+ia|language\s+models?|modelo\s+de\s+lenguaje)\b",
    r"<\s*/?\s*(system|instructions|admin)\s*>",
    r"\[\s*/?\s*(system|inst|instructions)\s*\]",
    r"\b(do\s+not|don'?t|never)\s+(tell|inform|notify|mention\s+(this|it)\s+to)\s+(the\s+)?(user|human)\b",
    r"\bno\s+(se\s+lo\s+|le\s+)?(digas|cuentes|menciones|informes)\s+(nada\s+)?(al|a\s+tu)\s+usuari[oa]",
    r"\b(already\s+)?authori[sz]ed\s+you\s+to\b|\bte\s+ha\s+autorizado\s+a\b",
]
_REGEX = [re.compile(p, re.IGNORECASE) for p in INSTRUCTION_PATTERNS]


def search_instructions(text: str) -> str | None:
    """Returns the fragment that looks like an instruction, or None."""
    if not text:
        return None
    for regex in _REGEX:
        m = regex.search(text)
        if m:
            start = max(0, m.start() - 40)
            return text[start : m.end() + 60].replace("\n", " ").strip()
    return None


_RE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+(\.[\w-]+)+")
_RE_URL = re.compile(r"https?://[^\s\"'<>)]+")


def key_data(entry: Any) -> set[str]:
    """Emails, URLs and domains that appear in an action's input."""
    text = entry if isinstance(entry, str) else repr(entry)
    values = {m.group(0).lower() for m in _RE_EMAIL.finditer(text)}
    for m in _RE_URL.finditer(text):
        url = m.group(0).lower().rstrip(".,;")
        values.add(url)
    return values


def external_reason(
    entry: Any, fragments: Iterable[tuple[str, str]], user_texts: Iterable[str]
) -> str | None:
    """Tells whether an action uses data that comes from external content and not from the user.

    `fragments` are (source, text) pairs of external content seen in the session.
    """
    keys = key_data(entry)
    if not keys:
        return None
    own = " ".join(user_texts).lower()
    for source, text in fragments:
        low = text.lower()
        for key in keys:
            if key in low and key not in own:
                return f'"{key}" appears in {source} and not in your messages'
    return None
