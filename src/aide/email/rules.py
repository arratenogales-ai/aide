"""Rules without a model for the user's new emails.

They decide, in this order:
1. The senders the user marked with the buttons, by exact address (never by domain).
2. Important domains (banks, insurers, official bodies): direct alert, except bulk
   mailings, which go to the model. The lists are in settings.yaml (`email.domains`):
   the ones there are an example that everyone replaces with their own. Addresses of
   official bodies with a government domain (.gov, gov.uk, gob.es, gouv.fr...) are
   recognized automatically.
3. Words about appointments, invitations, invoices or payments: to the model.
4. Everything else: silent.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Literal

Action = Literal["notice", "silence", "model"]
CATEGORIES = ("appointment", "bank", "insurance", "official", "invoice", "other")
# What is taught about each category (the keys are saved and the model returns them).
CATEGORY_NAME = {
    "appointment": "appointment or invitation",
    "bank": "bank",
    "insurance": "insurance",
    "official": "government",
    "invoice": "invoice or payment",
    "other": "other",
}

# Domains per category (“bank”, “insurance”, “official”), in the order they are tried. They come
# from settings.yaml; without them, none.
Domains = Mapping[str, Sequence[str]]
NO_DOMAINS: dict[str, tuple[str, ...]] = {"bank": (), "insurance": (), "official": ()}
# Government domains of any country: .gov and .mil, and gov., gob., gouv. or govt. before a country
# domain (gov.uk, gob.es, gouv.fr, govt.nz...), with their subdomains.
RE_OFFICIAL = re.compile(r"(^|\.)(gov|mil)$|(^|\.)(gov|gob|gouv|govt)\.[a-z]{2}$")

RE_APPOINTMENT = re.compile(
    r"\b(termin\w*|appointment\w*|einladung\w*|invitation\w*|invite\w*|invitaci[oó]n\w*|"
    r"cita|citas|convocatoria\w*|meeting\w*|besprechung\w*|rendez-vous|interview\w*|"
    r"entrevista\w*|reuni[oó]n|r[ée]union\w*)\b",
    re.IGNORECASE,
)
RE_INVOICE = re.compile(
    r"\b(rechnung\w*|invoice\w*|zahlung\w*|zahlungserinnerung|mahnung|payment\w*|receipt\w*|"
    r"quittung|beleg|factura\w*|facture\w*|recibo\w*|abbuchung|lastschrift|überweisung|"
    r"ueberweisung|steuer\w*|prämie\w*|praemie\w*|premium|billing|bill|bills|refund\w*|"
    r"tax|taxes|impuesto\w*|pago|pagos|paiement\w*|imp[oô]ts?)\b",
    re.IGNORECASE,
)


@dataclass
class Email:
    """An email with its sender, subject, start of the body and whether it is bulk."""

    id: str
    thread: str = ""
    received: str = ""
    from_name: str = ""
    from_address: str = ""
    subject: str = ""
    start: str = ""
    bulk: bool = False

    @property
    def domain(self) -> str:
        return self.from_address.rsplit("@", 1)[-1] if "@" in self.from_address else ""


@dataclass
class Decision:
    """The decision about an email: its action, category and reason."""

    action: Action
    category: str | None = None
    reason: str = ""
    # What the user marked for that sender (important or silence), if they marked it.
    preference: str | None = None


def from_headers(email_id: str, thread: str, headers: dict[str, str], start: str) -> Email:
    """Builds an Email from message headers, cleaning up the sender and body start."""
    name, address = parseaddr(headers.get("from") or "")
    bulk = bool(headers.get("list-unsubscribe")) or (
        (headers.get("precedence") or "").lower() in ("bulk", "list", "junk")
    )
    return Email(
        id=email_id,
        thread=thread,
        received=headers.get("date") or "",
        from_name=name.strip().strip('"'),
        from_address=address.strip().lower(),
        subject=(headers.get("subject") or "").strip(),
        start=" ".join((start or "").split())[:500],
        bulk=bulk,
    )


def _from_domains(domain: str, domains: Sequence[str]) -> bool:
    domain = domain.lower().rstrip(".")
    return any(domain == d or domain.endswith("." + d) for d in domains)


def domain_category(domain: str, domains: Domains | None = None) -> str | None:
    """Returns the category of a sender domain, or None if none matches."""
    domain = domain.lower().rstrip(".")
    for category, items in (NO_DOMAINS if domains is None else domains).items():
        if _from_domains(domain, items):
            return category
    if RE_OFFICIAL.search(domain):
        return "official"
    return None


def decide(email: Email, preferences: dict[str, str], domains: Domains | None = None) -> Decision:
    """Decides what to do with an email, using sender preferences first and then its domain."""
    text = f"{email.subject} {email.start}"
    preference = preferences.get(email.from_address)
    if preference == "important":
        return Decision(
            "notice",
            domain_category(email.domain, domains) or "other",
            "sender you marked as important",
            preference=preference,
        )
    if preference == "silence":
        return Decision("silence", None, "sender you muted", preference=preference)
    category = domain_category(email.domain, domains)
    if category is not None:
        if email.bulk:
            return Decision("model", category, "mass mailing from an important sender")
        return Decision("notice", category, f"{CATEGORY_NAME[category]} sender")
    if RE_APPOINTMENT.search(text):
        return Decision("model", "appointment", "mentions an appointment or invitation")
    if RE_INVOICE.search(text):
        return Decision("model", "invoice", "mentions an invoice or payment")
    return Decision("silence", None, "nothing important")
