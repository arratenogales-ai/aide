"""Gmail and Calendar in memory to test the tools without the real account."""

from __future__ import annotations

import base64
import email
import itertools
import re
from email import policy
from typing import Any

import httplib2
from googleapiclient.errors import HttpError

from aide.tools.google_auth import GoogleNotAuthorized


def _error(state: int) -> HttpError:
    return HttpError(httplib2.Response({"status": state}), b'{"error": {"message": "fake"}}')


class _Executable:
    def __init__(self, function):
        self.function = function

    def execute(self):
        return self.function()


def _payload_from_raw(raw: str) -> tuple[dict[str, Any], dict[str, str]]:
    message = email.message_from_bytes(base64.urlsafe_b64decode(raw), policy=policy.default)
    headers = [{"name": k, "value": v} for k, v in message.items()]
    parts = []
    for part in message.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        data = part.get_payload(decode=True) or b""
        if name:
            parts.append(
                {
                    "mimeType": part.get_content_type(),
                    "filename": name,
                    "body": {"size": len(data), "attachmentId": "a1"},
                }
            )
        else:
            parts.append(
                {
                    "mimeType": part.get_content_type(),
                    "filename": "",
                    "body": {"data": base64.urlsafe_b64encode(data).decode()},
                }
            )
    return {"mimeType": "multipart/mixed", "headers": headers, "parts": parts}, dict(
        message.items()
    )


class FakeGmail:
    def __init__(self) -> None:
        self.stored_messages: dict[str, dict[str, Any]] = {}
        self.stored_drafts: dict[str, dict[str, Any]] = {}
        self.sent: list[dict[str, Any]] = []
        self.listed: list[str] = []  # the searches that have been made
        self.attachments: dict[tuple[str, str], bytes] = {}
        self._ids = itertools.count(1)
        self.add_message("jobs@technova.example", "Your application", "Thanks for your interest.")

    def add_message(
        self,
        of: str,
        subject: str,
        text: str,
        labels: tuple[str, ...] = ("INBOX",),
        date: int = 1792396800,
        headers: dict[str, str] | None = None,
        attachments: list[tuple[str, bytes]] | None = None,
    ) -> str:
        """`date`: Unix seconds of its arrival (internalDate); `headers`: others, such as
        List-Unsubscribe; `attachments`: (name, content), downloaded separately."""
        mid = f"m{next(self._ids)}"
        self.stored_messages[mid] = {
            "id": mid,
            "threadId": f"t{mid}",
            "snippet": text[:50],
            "labelIds": list(labels),
            "internalDate": str(date * 1000),
            "payload": {
                "mimeType": "text/plain",
                "headers": [
                    {"name": "From", "value": of},
                    {"name": "Subject", "value": subject},
                    {"name": "Date", "value": "Mon, 19 Oct 2026 10:00:00 +0200"},
                    {"name": "Message-ID", "value": f"<{mid}@mail.example>"},
                    *({"name": k, "value": v} for k, v in (headers or {}).items()),
                ],
                "body": {"data": base64.urlsafe_b64encode(text.encode()).decode()},
            },
        }
        if attachments:
            payment = self.stored_messages[mid]["payload"]
            parts = [{"mimeType": "text/plain", "body": payment.pop("body")}]
            for i, (name, content) in enumerate(attachments, 1):
                aid = f"{mid}-adj{i}"
                self.attachments[(mid, aid)] = content
                parts.append(
                    {
                        "mimeType": "application/pdf",
                        "filename": name,
                        "body": {"attachmentId": aid, "size": len(content)},
                    }
                )
            payment.update(mimeType="multipart/mixed", parts=parts, body={"size": 0})
        return mid

    # API encadenada: service.users().messages().list(...).execute()
    def users(self):
        return self

    def get_profile(self, user_id):
        return _Executable(lambda: {"emailAddress": "user@example.com"})

    def messages(self):
        gmail = self

        class Messages:
            def list(self, user_id, q="", max_results=10, page_token=None):
                def run():
                    if q.startswith("in:sent"):
                        subject = q.split('subject:"')[1].split('"')[0] if 'subject:"' in q else ""
                        found = [m for m in gmail.sent if m["subject"] == subject]
                        return {"messages": [{"id": m["id"], "threadId": m["id"]} for m in found]}
                    gmail.listed.append(q)
                    everything = list(gmail.stored_messages.values())
                    after = re.search(r"after:(\d+)", q)
                    if after:
                        # Like Gmail: those that arrived after that moment,
                        # newest first.
                        limit = int(after.group(1)) * 1000
                        everything = [
                            m for m in everything if int(m.get("internalDate") or 0) > limit
                        ]
                        everything.sort(key=lambda m: -int(m.get("internalDate") or 0))
                    start = int(page_token or 0)
                    output = {
                        "messages": [
                            {"id": m["id"], "threadId": m["threadId"]}
                            for m in everything[start : start + max_results]
                        ]
                    }
                    if start + max_results < len(everything):
                        output["nextPageToken"] = str(start + max_results)
                    return output

                return _Executable(run)

            def get(self, user_id, id, format="full", metadata_headers=None):
                if id not in gmail.stored_messages:
                    return _Executable(lambda: (_ for _ in ()).throw(_error(404)))
                return _Executable(lambda: gmail.stored_messages[id])

            def attachments(self):
                class Attachments:
                    def get(self, user_id, message_id, id):
                        content = gmail.attachments.get((message_id, id))
                        if content is None:
                            return _Executable(lambda: (_ for _ in ()).throw(_error(404)))
                        data = base64.urlsafe_b64encode(content).decode()
                        return _Executable(lambda: {"data": data, "size": len(content)})

                return Attachments()

        return Messages()

    def threads(self):
        gmail = self

        class Threads:
            def get(self, user_id, id, format="full"):
                messages = [m for m in gmail.stored_messages.values() if m["threadId"] == id]
                if not messages:
                    return _Executable(lambda: (_ for _ in ()).throw(_error(404)))
                return _Executable(lambda: {"id": id, "messages": messages})

        return Threads()

    def drafts(self):
        gmail = self

        class Drafts:
            def create(self, user_id, body):
                def run():
                    did = f"d{next(gmail._ids)}"
                    payload, _ = _payload_from_raw(body["message"]["raw"])
                    gmail.stored_drafts[did] = {
                        "id": did,
                        "message": {"id": f"msg{did}", "payload": payload},
                    }
                    return {"id": did, "message": {"id": f"msg{did}"}}

                return _Executable(run)

            def get(self, user_id, id, format="full"):
                if id not in gmail.stored_drafts:
                    return _Executable(lambda: (_ for _ in ()).throw(_error(404)))
                return _Executable(lambda: gmail.stored_drafts[id])

            def list(self, user_id, max_results=30):
                return _Executable(lambda: {"drafts": [{"id": d} for d in gmail.stored_drafts]})

            def send(self, user_id, body):
                def run():
                    did = body["id"]
                    if did not in gmail.stored_drafts:
                        raise _error(404)
                    draft = gmail.stored_drafts.pop(did)
                    header = {
                        h["name"].lower(): h["value"]
                        for h in draft["message"]["payload"]["headers"]
                    }
                    sent = {
                        "id": f"s{next(gmail._ids)}",
                        "to": header.get("to"),
                        "subject": header.get("subject"),
                    }
                    gmail.sent.append(sent)
                    return {"id": sent["id"], "threadId": sent["id"]}

                return _Executable(run)

        return Drafts()


class FakeCalendar:
    def __init__(self) -> None:
        self.stored_events: dict[str, dict[str, Any]] = {}
        self.sends: list[tuple[str, str]] = []

    def add(self, eid: str, title: str, start: str, end: str, **extra) -> None:
        self.stored_events[eid] = {
            "id": eid,
            "summary": title,
            "start": {"dateTime": start},
            "end": {"dateTime": end},
            "status": "confirmed",
            **extra,
        }

    def events(self):
        cal = self

        class Events:
            def list(
                self,
                calendar_id,
                time_min,
                time_max,
                single_events=True,
                order_by="startTime",
                max_results=250,
            ):
                return _Executable(
                    lambda: {
                        "items": sorted(
                            cal.stored_events.values(), key=lambda e: e["start"]["dateTime"]
                        )
                    }
                )

            def get(self, calendar_id, event_id):
                if event_id not in cal.stored_events:
                    return _Executable(lambda: (_ for _ in ()).throw(_error(404)))
                return _Executable(lambda: cal.stored_events[event_id])

            def insert(self, calendar_id, body, send_updates="none"):
                def run():
                    if body["id"] in cal.stored_events:
                        raise _error(409)
                    cal.stored_events[body["id"]] = {
                        **body,
                        "status": "confirmed",
                        "htmlLink": "https://cal/" + body["id"],
                    }
                    cal.sends.append((body["id"], send_updates))
                    return cal.stored_events[body["id"]]

                return _Executable(run)

            def patch(self, calendar_id, event_id, body, send_updates="none"):
                def run():
                    cal.stored_events[event_id].update(body)
                    cal.sends.append((event_id, send_updates))
                    return cal.stored_events[event_id]

                return _Executable(run)

            def delete(self, calendar_id, event_id, send_updates="none"):
                def run():
                    if event_id not in cal.stored_events:
                        raise _error(410)
                    del cal.stored_events[event_id]
                    cal.sends.append((event_id, send_updates))
                    return {}

                return _Executable(run)

        return Events()


class FakeAuthorization:
    def __init__(self) -> None:
        self.gmail = FakeGmail()
        self.calendar = FakeCalendar()
        self.expired = False

    def service(self, name: str, version: str):
        if self.expired:
            raise GoogleNotAuthorized("invalid_grant: Token has been expired or revoked.")
        return self.gmail if name == "gmail" else self.calendar

    def state(self) -> dict[str, Any]:
        return {"authorized": not self.expired, "account": "user@example.com"}

    def expires_in(self):
        return None
