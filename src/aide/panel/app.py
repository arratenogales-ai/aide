"""Local web panel: FastAPI on 127.0.0.1 with an access token.

Security:
- It only listens on 127.0.0.1 and rejects requests with another Host header (DNS
  rebinding).
- With PANEL_TAILSCALE, it also accepts the Mac's name on the Tailscale network,
  where `tailscale serve` arrives with HTTPS, but only while Funnel is off (checked every
  minute).
- Access with a token saved in the keychain; `/panel` in Telegram gives the entry link,
  which leaves an HttpOnly, SameSite=Strict cookie.
- Forms carry a CSRF token and the Origin is checked. Strict content policy: no inline
  scripts or outside resources.
- Google's redirect (`/google/callback`) does not need the cookie: `state` protects it.
- Downloads only serve files from the agent's folder.
- Decisions (approvals, plans, caps and memory proposals) are made only in Telegram: the
  panel has no buttons or routes for them. permissions.yaml can only be viewed.
- “Open in Finder” only opens the task's saved final folder, and only if it is inside
  “Finished Tasks”, or a project's folder, and only if it is inside agent/projects (with
  the paths resolved, so a link that leaves there is not valid).

Live update: `/events` (server events) signals when something changes and the page
requests its content again (panel.js).
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import html
import logging
import secrets
import shutil
import subprocess
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo

import jinja2
import keyring
import uvicorn
import yaml
from fastapi import FastAPI, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.templating import Jinja2Templates

from aide.common.config import Models, Settings
from aide.common.logs import log_secret
from aide.common.tailscale import ServeState, read_serve
from aide.core import scheduler
from aide.core.approvals import KIND_TITLES
from aide.core.db import now
from aide.core.finished import inside_of
from aide.core.presentation import GROUPS
from aide.core.usage import format_tokens
from aide.health import number
from aide.panel.views import (
    APPROVAL_NAMES,
    ONLY_READ,
    Views,
    event_title,
    state_name,
)

if TYPE_CHECKING:
    from aide.core.engine import Engine

log = logging.getLogger(__name__)

KEYCHAIN_SERVICE = "aide"
TOKEN_ACCOUNT = "panel-token"
COOKIE = "aide_session"
# How often it is checked that Funnel is still off.
TAILSCALE_INTERVAL = 60
PUBLIC = ("/enter", "/google/callback", "/style.css", "/panel.js", "/apple-touch-icon")
# The session lasts 30 days from the last time the panel is opened.
SESSION_DURATION = 30 * 24 * 3600
HERE = Path(__file__).parent
TEMPLATES = HERE / "templates"
STATIC = HERE / "static"
# The side menu: route, name, icon and pastel color of each section. Projects and Scheduled
# go inside Tasks, as tabs, and Health keeps the pink.
MENU = [
    ("/", "Today", "today", "lilac"),
    ("/tasks", "Tasks", "tasks", "blue"),
    ("/documents", "Documents", "documents", "peach"),
    ("/health", "Health", "health", "pink"),
    ("/profile", "My profile", "profile", "turquoise"),
]
# The Tasks tabs.
TASKS_VIEWS = ("tasks", "projects", "scheduled")
PROJECTS = "/tasks?view=projects"
SCHEDULED_TASKS = "/tasks?view=scheduled"
# At the foot of the page, in the discreet “Advanced” link.
ADVANCED_MENU = [("/settings", "Settings"), ("/email", "Email"), ("/logs", "Logs")]
# Configuration files that can be edited from the panel (permissions.yaml is view-only).
VALIDATORS = {"models.yaml": Models, "settings.yaml": Settings}
CSP = (
    "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
    "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)
INSTRUCTIONS_LINE_LIMIT = 150


class PanelServer(uvicorn.Server):
    """Uvicorn without capturing SIGTERM or SIGINT: the application already handles shutdown
    and stops the panel itself. If they were captured, on shutdown it would wait for the
    open connections to close, and a panel tab keeps the live events one open."""

    @contextlib.contextmanager
    def capture_signals(self):  # type: ignore[override]
        yield


def token_panel() -> str:
    """Returns the panel access token, creating and saving it if needed."""
    token = keyring.get_password(KEYCHAIN_SERVICE, TOKEN_ACCOUNT)
    if not token:
        token = secrets.token_urlsafe(32)
        keyring.set_password(KEYCHAIN_SERVICE, TOKEN_ACCOUNT, token)
    log_secret(token)
    return token


async def open_in_finder(folder: Path) -> None:
    """Opens the folder in Finder (the panel runs in the user's session on the Mac)."""
    await asyncio.to_thread(
        subprocess.run, ["open", str(folder)], check=False, timeout=15, capture_output=True
    )


def timing_from_form(form: Any) -> dict[str, Any]:
    """The scheduler's `timing` from the form fields."""
    kind = str(form.get("kind", ""))
    data: dict[str, Any] = {"kind": kind}
    if kind == "one_time":
        data["date"] = str(form.get("date", ""))
    elif kind == "every_hours":
        data["hours"] = form.get("hours") or ""
    else:
        data["hour"] = str(form.get("hour", ""))
        if kind == "weekly":
            data["days"] = [d.strip() for d in str(form.get("days", "")).split(",") if d.strip()]
        elif kind == "monthly":
            data["day"] = form.get("day") or ""
    return data


class Panel:
    """The local web panel, with its server, access token and CSRF protection."""

    def __init__(self, engine: Engine, token: str | None = None) -> None:
        self.engine = engine
        self.config = engine.config
        self.host = self.config.panel_host
        self.port = self.config.panel_port
        self.token = token or token_panel()
        self.csrf = secrets.token_urlsafe(24)
        # The iPhone through Tailscale: the address is only valid after checking
        # that Funnel is off, and stops being valid if it is turned on or it cannot be
        # known.
        self.host_tailscale = self.config.panel_tailscale
        self.safe_tailscale = False
        self.read_serve: Callable[[], ServeState] = read_serve
        self._funnel_notice = False
        self.views = Views(engine)
        self.zone = ZoneInfo(self.config.time_zone)
        self.app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
        environment = jinja2.Environment(
            loader=jinja2.FileSystemLoader(str(TEMPLATES)), autoescape=True, trim_blocks=True
        )
        environment.globals["date"] = self.views.date
        environment.globals["groups"] = GROUPS
        environment.globals["tokens"] = format_tokens
        environment.globals["event_title"] = event_title
        environment.globals["state_name"] = state_name
        environment.globals["approval_kinds"] = KIND_TITLES
        environment.globals["approval_states"] = APPROVAL_NAMES
        # Numbers with a thousands separator (1,450), for Health.
        environment.globals["number"] = number
        self.templates = Jinja2Templates(env=environment)
        self._server: uvicorn.Server | None = None
        # Opens a folder in Finder (the tests replace it).
        self.open_in_finder: Callable[[Path], Awaitable[None]] = open_in_finder
        self._configure()

    # --- links -----------------------------------------------------------------------------

    @property
    def base(self) -> str:
        return f"http://{self.host}:{self.port}"

    def link(self, destination: str = "/") -> str:
        return f"{self.base}/enter?t={self.token}&go={quote(destination)}"

    def mobile_link(self, destination: str = "/") -> str | None:
        """The entry link through Tailscale, for the iPhone."""
        if not self.host_tailscale:
            return None
        return f"https://{self.host_tailscale}/enter?t={self.token}&go={quote(destination)}"

    # --- security --------------------------------------------------------------------------

    def by_tailscale(self, request: Request) -> bool:
        """Tells whether a request arrived through the Tailscale address."""
        host = request.headers.get("host", "").lower()
        return bool(self.host_tailscale) and host in (
            self.host_tailscale,
            f"{self.host_tailscale}:443",
        )

    def _valid_host(self, request: Request) -> bool:
        host = request.headers.get("host", "")
        if self.by_tailscale(request):
            return self.safe_tailscale
        return host in (
            f"{self.host}:{self.port}",
            f"127.0.0.1:{self.port}",
            f"localhost:{self.port}",
        )

    def authenticated(self, request: Request) -> bool:
        cookie = request.cookies.get(COOKIE, "")
        return bool(cookie) and hmac.compare_digest(cookie, self.token)

    def valid_origin(self, request: Request) -> bool:
        """Checks that the request origin is allowed, so cross-site requests are rejected."""
        origin = request.headers.get("origin")
        if origin is None:
            return True  # direct navigation or same origin without a header
        if self.by_tailscale(request):
            return origin == f"https://{self.host_tailscale}"
        return origin in (
            self.base,
            f"http://127.0.0.1:{self.port}",
            f"http://localhost:{self.port}",
        )

    def _put_session(self, reply: Response, request: Request) -> None:
        reply.set_cookie(
            COOKIE,
            self.token,
            max_age=SESSION_DURATION,
            httponly=True,
            samesite="strict",
            # Through Tailscale it arrives over HTTPS: the cookie never travels
            # unencrypted.
            secure=self.by_tailscale(request),
        )

    def valid_csrf(self, value: str | None) -> bool:
        return bool(value) and hmac.compare_digest(value, self.csrf)

    def allowed_file(self, path: str) -> Path | None:
        """Only files from the agent's folder (tasks, projects and screenshots)."""
        try:
            destination = Path(path).expanduser().resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        agent = self.config.paths.agent.resolve()
        if not destination.is_file() or not destination.is_relative_to(agent):
            return None
        return destination

    def allowed_project(self, name: str) -> Path | None:
        """Only project folders that exist inside agent/projects (not the root)."""
        root = self.config.paths.projects
        if not name or "/" in name or "\\" in name or name in (".", ".."):
            return None
        try:
            destination = (root / name).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        if (
            not destination.is_dir()
            or destination == root.resolve()
            or not inside_of(destination, root)
        ):
            return None
        return destination

    def allowed_final_folder(self, path: str | None) -> Path | None:
        """Only folders that exist inside “Finished Tasks” (not the root)."""
        if not path:
            return None
        try:
            destination = Path(path).resolve(strict=True)
        except (OSError, RuntimeError):
            return None
        root = self.config.paths.finished
        if (
            not destination.is_dir()
            or destination == root.resolve()
            or not inside_of(destination, root)
        ):
            return None
        return destination

    # --- replies ----------------------------------------------------------------------------

    async def page(
        self, request: Request, template: str, title: str, section: str, **data: Any
    ) -> Response:
        """Renders a panel page from a template with the shared context and the given data."""
        context = {
            "title": title,
            "section": section,
            "menu": MENU,
            "advanced_menu": ADVANCED_MENU,
            "csrf": self.csrf,
            "notice": request.query_params.get("notice"),
            "kind_notice": request.query_params.get("kind"),
            "partial": request.headers.get("x-partial") == "1",
            **data,
        }
        return self.templates.TemplateResponse(request, template, context)

    @staticmethod
    def go_back(
        destination: str, notice: str, kind: str = "ok", anchor: str = ""
    ) -> RedirectResponse:
        """Returns a redirect to a destination with a notice message in the query string."""
        separator = "&" if "?" in destination else "?"
        fragment = f"#{anchor}" if anchor else ""
        return RedirectResponse(
            f"{destination}{separator}{urlencode({'notice': notice, 'kind': kind})}{fragment}",
            status_code=303,
        )

    @staticmethod
    def to_tab(destination: str, request: Request) -> RedirectResponse:
        """An old address that is now a tab: leads there, with the notice."""
        notices = {k: v for k, v in request.query_params.items() if k in ("notice", "kind")}
        return RedirectResponse(
            destination + (f"&{urlencode(notices)}" if notices else ""), status_code=303
        )

    async def form(self, request: Request) -> Any | None:
        data = await request.form()
        return data if self.valid_csrf(str(data.get("csrf", ""))) else None

    # --- paths ------------------------------------------------------------------------------

    def _configure(self) -> None:
        app = self.app

        @app.middleware("http")
        async def security(
            request: Request, next_step: Callable[[Request], Awaitable[Response]]
        ) -> Response:
            if not self._valid_host(request):
                return PlainTextResponse("Host not allowed", status_code=400)
            if request.method not in ("GET", "HEAD") and not self.valid_origin(request):
                return PlainTextResponse("Origin not allowed", status_code=403)
            if not request.url.path.startswith(PUBLIC) and not self.authenticated(request):
                return HTMLResponse(
                    "<h1>Aide</h1><p>Open the panel with the link from the "
                    "<code>/panel</code> command in Telegram.</p>",
                    status_code=401,
                )
            reply = await next_step(request)
            if not request.url.path.startswith(PUBLIC):
                # Every visit renews the session: it only expires after 30
                # days without opening it.
                self._put_session(reply, request)
            reply.headers["X-Frame-Options"] = "DENY"
            # same-origin and not no-referrer: with no-referrer Chrome sends `Origin:
            # null` on the panel's own forms and the origin check would reject them.
            reply.headers["Referrer-Policy"] = "same-origin"
            reply.headers["X-Content-Type-Options"] = "nosniff"
            reply.headers.setdefault("Content-Security-Policy", CSP)
            return reply

        @app.get("/enter")
        async def enter(request: Request, t: str = "", go: str = "/") -> Response:
            if not t or not hmac.compare_digest(t, self.token):
                return PlainTextResponse("Invalid link", status_code=403)
            destination = go if go.startswith("/") and not go.startswith("//") else "/"
            # A page of its own that passes on to the destination, and not a redirect
            #: if the link is opened from another app (Telegram on the iPhone),
            # the browser treats the whole chain of redirects as external and does not
            # send the SameSite=Strict cookie on the first hop. From this page the hop
            # is already the panel's own.
            go_to = html.escape(destination, quote=True)
            reply = HTMLResponse(
                f'<!doctype html><meta charset="utf-8"><meta http-equiv="refresh" '
                f'content="0;url={go_to}"><title>Aide</title>'
                f'<p>Opening the panel… <a href="{go_to}">Open the panel</a></p>'
            )
            reply.headers["Cache-Control"] = "no-store"
            self._put_session(reply, request)
            return reply

        @app.get("/apple-touch-icon.png")
        @app.get("/apple-touch-icon-precomposed.png")
        async def icon() -> Response:
            """The icon for the shortcut on the iPhone home screen."""
            return FileResponse(STATIC / "icon.png", media_type="image/png")

        @app.get("/style.css")
        async def style() -> Response:
            return FileResponse(STATIC / "style.css", media_type="text/css")

        @app.get("/panel.js")
        async def script() -> Response:
            return FileResponse(STATIC / "panel.js", media_type="text/javascript")

        @app.get("/events")
        async def events(request: Request) -> Response:
            async def flow() -> AsyncIterator[str]:
                previous = await self.views.version()
                heartbeat = 0
                while not await request.is_disconnected():
                    if self._server is not None and self._server.should_exit:
                        return  # panel shutdown
                    await asyncio.sleep(2)
                    current = await self.views.version()
                    if current != previous:
                        previous = current
                        yield "event: change\ndata: 1\n\n"
                    heartbeat += 2
                    if heartbeat >= 20:
                        heartbeat = 0
                        yield ": heartbeat\n\n"

            return StreamingResponse(
                flow(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"}
            )

        # --- views ----

        @app.get("/", response_class=HTMLResponse)
        async def start(request: Request) -> Response:
            return await self.page(request, "home.html", "Today", "/", d=await self.views.start())

        @app.get("/tasks", response_class=HTMLResponse)
        async def tasks(
            request: Request, group: str = "", q: str = "", view: str = "tasks"
        ) -> Response:
            # Tasks, Projects and Scheduled go together in three tabs.
            page_view = view if view in TASKS_VIEWS else "tasks"
            projects = await asyncio.to_thread(self.views.projects)
            return await self.page(
                request,
                "tasks.html",
                {"projects": "Projects", "scheduled": "Scheduled"}.get(page_view, "Tasks"),
                "/tasks",
                view=page_view,
                tasks=await self.views.tasks(group or None, q or None)
                if page_view == "tasks"
                else [],
                projects=projects,
                scheduled_tasks=await self.views.scheduled_tasks()
                if page_view == "scheduled"
                else [],
                scheduled_tasks_how_many=await self.views.scheduled_tasks_how_many(),
                zone=self.config.time_zone,
                sel_group=group,
                q=q,
            )

        @app.get("/task/{task_id}", response_class=HTMLResponse)
        async def task(request: Request, task_id: int) -> Response:
            data = await self.views.task(task_id)
            if data is None:
                return PlainTextResponse("That task doesn't exist", status_code=404)
            return await self.page(request, "task.html", f"Task {task_id}", "/tasks", d=data)

        @app.get("/task/{task_id}/transcript", response_class=HTMLResponse)
        async def transcription(request: Request, task_id: int) -> Response:
            task = await self.engine.tasks.get(task_id)
            if task is None:
                return PlainTextResponse("That task doesn't exist", status_code=404)
            messages = await asyncio.to_thread(self.views.transcription, task.session_id)
            return await self.page(
                request,
                "transcription.html",
                f"Transcript {task_id}",
                "/tasks",
                task_id=task_id,
                messages=messages,
            )

        @app.post("/task/{task_id}/open")
        async def open_folder(request: Request, task_id: int) -> Response:
            data = await self.form(request)
            if data is None:
                return PlainTextResponse("Form expired: reload the page", status_code=403)
            task = await self.engine.tasks.get(task_id)
            # The path comes from the database, never from the form, and is checked
            # anyway.
            folder = self.allowed_final_folder(task.final_folder if task else None)
            if folder is None:
                return self.go_back(f"/task/{task_id}", "That folder isn't available.", "error")
            await self.open_in_finder(folder)
            return self.go_back(f"/task/{task_id}", f'Opened "{folder.name}" in Finder.')

        @app.get("/file")
        async def file(path: str = "") -> Response:
            destination = self.allowed_file(path)
            if destination is None:
                return PlainTextResponse("File not available", status_code=404)
            return FileResponse(destination, filename=destination.name)

        # --- scheduled tasks ---

        @app.get("/scheduled")
        async def scheduled_tasks(request: Request) -> Response:
            """Scheduled tasks are a tab of Tasks: the old address leads there, with its
            notice if it has one. Their forms are still submitted to /scheduled."""
            return self.to_tab(SCHEDULED_TASKS, request)

        async def save_scheduled_task(request: Request, scheduled_task_id: int | None) -> Response:
            data = await self.form(request)
            if data is None:
                return PlainTextResponse("Form expired: reload the page", status_code=403)
            if scheduled_task_id is not None:
                current = await self.engine.db.one(
                    "SELECT kind FROM scheduled_tasks WHERE id=?", (scheduled_task_id,)
                )
                if current is not None and (current.get("kind") or "model") != "model":
                    # A script's command only changes with the user's approval
                    # in Telegram, and maintenance is configured in
                    # settings.yaml.
                    return self.go_back(
                        SCHEDULED_TASKS, "That scheduled task can't be edited here.", "error"
                    )
            description = " ".join(str(data.get("description", "")).split())
            policy = str(data.get("policy", ""))
            try:
                if not description:
                    raise scheduler.InvalidTiming("the description is missing")
                if policy not in scheduler.POLICIES:
                    raise scheduler.InvalidTiming("invalid policy")
                timing = scheduler.interpret(timing_from_form(data), self.zone)
            except scheduler.InvalidTiming as e:
                return self.go_back(SCHEDULED_TASKS, f"Not saved: {e}.", "error")
            next_run = scheduler.next_step(timing, datetime.now(self.zone), self.zone)
            if next_run is None:
                return self.go_back(
                    SCHEDULED_TASKS, "Not saved: that date has already passed.", "error"
                )
            values = (
                description,
                timing.to_json(),
                policy,
                next_run.astimezone(UTC).isoformat(timespec="seconds"),
                scheduler.describe(timing),
            )
            if scheduled_task_id is None:
                await self.engine.db.run(
                    "INSERT INTO scheduled_tasks(description, timing, policy, next_run, timing_text, active, created) "
                    "VALUES (?, ?, ?, ?, ?, 1, ?)",
                    (*values, now()),
                )
                return self.go_back(SCHEDULED_TASKS, f"Scheduled: {scheduler.describe(timing)}.")
            await self.engine.db.run(
                "UPDATE scheduled_tasks SET description=?, timing=?, policy=?, next_run=?, timing_text=?, active=1 "
                "WHERE id=?",
                (*values, scheduled_task_id),
            )
            return self.go_back(SCHEDULED_TASKS, f"Scheduled task {scheduled_task_id} updated.")

        @app.post("/scheduled")
        async def create_scheduled_task(request: Request) -> Response:
            return await save_scheduled_task(request, None)

        @app.post("/scheduled/{scheduled_task_id}")
        async def edit_scheduled_task(request: Request, scheduled_task_id: int) -> Response:
            return await save_scheduled_task(request, scheduled_task_id)

        @app.post("/scheduled/{scheduled_task_id}/{action}")
        async def scheduled_action(
            request: Request, scheduled_task_id: int, action: str
        ) -> Response:
            data = await self.form(request)
            if data is None:
                return PlainTextResponse("Form expired: reload the page", status_code=403)
            row = await self.engine.db.one(
                "SELECT * FROM scheduled_tasks WHERE id=?", (scheduled_task_id,)
            )
            if row is None:
                return self.go_back(SCHEDULED_TASKS, "That scheduled task doesn't exist.", "error")
            if row.get("kind") == "maintenance" or row.get("pending"):
                return self.go_back(
                    SCHEDULED_TASKS,
                    "Maintenance is set in settings.yaml, and a pending script is approved "
                    "in Telegram.",
                    "error",
                )
            if action == "pause":
                await self.engine.db.run(
                    "UPDATE scheduled_tasks SET active=0 WHERE id=?", (scheduled_task_id,)
                )
                return self.go_back(SCHEDULED_TASKS, f"Scheduled task {scheduled_task_id} paused.")
            if action == "resume":
                timing = scheduler.interpret(row["timing"], self.zone)
                next_run = scheduler.next_step(timing, datetime.now(self.zone), self.zone)
                if next_run is None:
                    return self.go_back(
                        SCHEDULED_TASKS, "Can't resume: its date has passed.", "error"
                    )
                await self.engine.db.run(
                    "UPDATE scheduled_tasks SET active=1, next_run=? WHERE id=?",
                    (next_run.astimezone(UTC).isoformat(timespec="seconds"), scheduled_task_id),
                )
                return self.go_back(SCHEDULED_TASKS, f"Scheduled task {scheduled_task_id} resumed.")
            if action == "delete":
                await self.engine.db.run(
                    "DELETE FROM scheduled_tasks WHERE id=?", (scheduled_task_id,)
                )
                return self.go_back(SCHEDULED_TASKS, f"Scheduled task {scheduled_task_id} deleted.")
            return PlainTextResponse("Unknown action", status_code=404)

        # --- your documents -----------

        @app.get("/documents", response_class=HTMLResponse)
        async def documents_page(request: Request) -> Response:
            return await self.page(
                request,
                "documents.html",
                "Documents",
                "/documents",
                d=await self.views.documents(),
            )

        @app.post("/documents/inbox")
        async def read_inbox(request: Request) -> Response:
            """The inbox button: like the /documents command, the question arrives in Telegram."""
            from aide.paperwork import inbox

            data = await self.form(request)
            if data is None:
                return PlainTextResponse("Form expired: reload the page", status_code=403)
            text = await inbox.ask(self.engine)
            return self.go_back("/documents", f"I've messaged you in Telegram. {text}")

        @app.post("/documents/upload")
        async def upload_documents(request: Request) -> Response:
            """Uploading documents from the panel: they go to the inbox and the usual flow follows (the
            question with how many there are and what reading them will cost arrives in Telegram)."""
            from aide.paperwork import inbox

            data = await self.form(request)
            if data is None:
                return PlainTextResponse("Form expired: reload the page", status_code=403)
            files = [a for a in data.getlist("files") if getattr(a, "filename", "")]
            uploaded, rejected = await asyncio.to_thread(
                inbox.save_uploaded, self.config.paths.inbox, files
            )
            if not uploaded:
                reason = "; ".join(rejected) if rejected else "you didn't choose a file"
                return self.go_back("/documents", f"I didn't upload anything: {reason}.", "error")
            text = await inbox.ask(self.engine)
            parts = [
                f"{len(uploaded)} {'document' if len(uploaded) == 1 else 'documents'} uploaded to the"
                " inbox.",
                f"I didn't upload {'; '.join(rejected)}." if rejected else "",
                f"I've messaged you in Telegram. {text}",
            ]
            return self.go_back("/documents", " ".join(p for p in parts if p))

        @app.post("/documents/{document_id}/remove")
        async def remove_document(request: Request, document_id: int) -> Response:
            """Removing a saved document: the file goes to the Trash and stops appearing in the search,
            in the folders and in the alerts."""
            from aide.paperwork import inbox, registry
            from aide.paperwork.flow import path_of

            data = await self.form(request)
            if data is None:
                return PlainTextResponse("Form expired: reload the page", status_code=403)
            d = await registry.get(self.engine.db, document_id)
            if d is None or d["state"] != "saved":
                return self.go_back("/documents", "That document was already gone.", "error")
            path = path_of(self.config.paths.documents, d)
            if (
                path is not None
                and path.exists()
                and not await asyncio.to_thread(inbox.to_trash, path)
            ):
                return self.go_back(
                    "/documents",
                    "I couldn't move the file to the Trash, so I didn't remove anything.",
                    "error",
                )
            await registry.remove(self.engine.db, document_id)
            return self.go_back(
                "/documents",
                f"Removed “{d.get('title') or 'Untitled document'}”: it's in the Trash.",
            )

        @app.get("/documents/{document_id}/file")
        async def document_file(document_id: int) -> Response:
            from aide.paperwork import registry
            from aide.paperwork.flow import path_of

            # The path comes from the database and has to be inside data/documents.
            d = await registry.get(self.engine.db, document_id)
            path = (
                path_of(self.config.paths.documents, d)
                if d is not None and d["state"] in ("saved", "proposed")
                else None
            )
            if path is None or not path.is_file():
                return PlainTextResponse("Document not available", status_code=404)
            return FileResponse(
                path,
                filename=d.get("original_name") or path.name,
                content_disposition_type="inline",
            )

        # --- mail -------------

        @app.get("/email", response_class=HTMLResponse)
        async def email_page(request: Request) -> Response:
            return await self.page(
                request, "email.html", "Email", "/email", d=await self.views.email()
            )

        @app.post("/email/senders/remove")
        async def remove_sender(request: Request) -> Response:
            data = await self.form(request)
            if data is None:
                return PlainTextResponse("Form expired: reload the page", status_code=403)
            address = str(data.get("address") or "").strip().lower()
            await self.engine.db.run("DELETE FROM senders WHERE address=?", (address,))
            return self.go_back("/email", f"Removed {address}: the filter decides again.")

        # --- memory ----

        @app.get("/profile", response_class=HTMLResponse)
        async def memory(request: Request) -> Response:
            return await self.page(
                request, "memory.html", "My profile", "/profile", d=await self.views.memory()
            )

        @app.post("/profile/{file}")
        async def save_memory(request: Request, file: str) -> Response:
            data = await self.form(request)
            if data is None:
                return PlainTextResponse("Form expired: reload the page", status_code=403)
            paths = self.config.paths
            destination = {"profile.md": paths.profile, "memory.md": paths.memory}.get(file)
            if destination is None:
                return PlainTextResponse("Unknown file", status_code=404)
            content = str(data.get("content", "")).replace("\r\n", "\n")
            if not content.endswith("\n"):
                content += "\n"
            destination.write_text(content, encoding="utf-8")
            notice = f"{destination.name} saved."
            if file == "profile.md":
                lines = self.views.instructions_lines()
                if lines > INSTRUCTIONS_LINE_LIMIT:
                    return self.go_back(
                        "/profile",
                        f"{notice} Heads up: CLAUDE.md and profile.md add up to {lines} lines and the limit "
                        f"is {INSTRUCTIONS_LINE_LIMIT}; every line goes into every request.",
                        "error",
                    )
            return self.go_back("/profile", notice)

        # --- projects, settings and logs ------

        @app.get("/projects")
        async def projects(request: Request) -> Response:
            """Projects are a tab of Tasks: the old address leads there, with its notice if
            it has one."""
            return self.to_tab(PROJECTS, request)

        @app.post("/projects/{name}/open")
        async def open_project(request: Request, name: str) -> Response:
            data = await self.form(request)
            if data is None:
                return PlainTextResponse("Form expired: reload the page", status_code=403)
            folder = self.allowed_project(name)
            if folder is None:
                return self.go_back(PROJECTS, "That project isn't available.", "error")
            await self.open_in_finder(folder)
            return self.go_back(PROJECTS, f'Opened "{folder.name}" in Finder.')

        @app.get("/projects/{name}", response_class=HTMLResponse)
        async def project(request: Request, name: str) -> Response:
            data = await asyncio.to_thread(self.views.project, name)
            if data is None:
                return PlainTextResponse("That project doesn't exist", status_code=404)
            return await self.page(request, "project.html", f"Project {name}", "/tasks", p=data)

        @app.get("/settings", response_class=HTMLResponse)
        async def settings(request: Request) -> Response:
            return await self.page(
                request, "settings.html", "Settings", "/settings", files=self.views.settings_view()
            )

        @app.post("/settings/{name}")
        async def save_settings(request: Request, name: str) -> Response:
            data = await self.form(request)
            if data is None:
                return PlainTextResponse("Form expired: reload the page", status_code=403)
            if name in ONLY_READ:
                return PlainTextResponse(f"{name} is view-only in the panel", status_code=403)
            model = VALIDATORS.get(name)
            if model is None:
                return PlainTextResponse("Unknown file", status_code=404)
            content = str(data.get("content", "")).replace("\r\n", "\n")
            try:
                model.model_validate(yaml.safe_load(content) or {})
            except Exception as e:
                return self.go_back("/settings", f"{name} not saved: {str(e)[:600]}", "error")
            destination = self.config.paths.config / name
            backups = self.config.paths.config / "backups"
            backups.mkdir(exist_ok=True)
            if destination.exists():
                shutil.copy2(destination, backups / f"{name}.{datetime.now():%Y%m%d-%H%M%S}")
            destination.write_text(
                content if content.endswith("\n") else content + "\n", encoding="utf-8"
            )
            return self.go_back(
                "/settings",
                f"{name} is valid and saved. It takes effect when the agent restarts.",
            )

        @app.get("/logs", response_class=HTMLResponse)
        async def logs(request: Request) -> Response:
            return await self.page(
                request,
                "logs.html",
                "Logs",
                "/logs",
                d=await self.views.logs(),
            )

        # --- Google ---

        @app.get("/google/authorize")
        async def google_authorize() -> Response:
            auth = self.engine.core.services.get("google")
            if auth is None:
                return PlainTextResponse(
                    "Google isn't set up (config/credentials.json is missing).", status_code=404
                )
            url = await asyncio.to_thread(auth.start_web, f"{self.base}/google/callback")
            return RedirectResponse(url, status_code=303)

        @app.get("/google/callback")
        async def google_callback(state: str = "", code: str = "", error: str = "") -> Response:
            auth = self.engine.core.services.get("google")
            if auth is None:
                return PlainTextResponse("Google isn't set up.", status_code=404)
            if error or not code:
                return PlainTextResponse(
                    f"Authorization canceled: {error or 'no code'}", status_code=400
                )
            try:
                account = await asyncio.to_thread(auth.complete_web, state, code)
            except Exception as e:
                log.warning("couldn't complete Google authorization: %s", e)
                return PlainTextResponse(
                    f"Couldn't authorize: {type(e).__name__}: {e}", status_code=400
                )
            await self.engine.reauthorized_google(account)
            return PlainTextResponse(
                f"Google authorized for {account}. You can close this tab now."
            )

        # --- health -----------
        from aide.panel import health

        health.configure(self)

    # --- server -----------------------------------------------------------------------------

    async def start(self) -> Callable[[], Awaitable[None]]:
        """Starts the panel web server and returns a function to stop it."""
        configuration = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
            access_log=False,
            lifespan="off",
            timeout_graceful_shutdown=3,
        )
        self._server = PanelServer(configuration)
        task = asyncio.create_task(self._server.serve())
        for _ in range(50):
            if self._server.started:
                break
            await asyncio.sleep(0.1)
        log.info("panel at %s", self.base)
        monitoring = asyncio.create_task(self.watch_tailscale()) if self.host_tailscale else None

        async def stop() -> None:
            if monitoring is not None:
                monitoring.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await monitoring
            if self._server is not None:
                self._server.should_exit = True
            with contextlib.suppress(Exception):
                await asyncio.wait_for(task, timeout=10)

        return stop

    async def watch_tailscale(self) -> None:
        """Keeps checking the Tailscale state at a fixed interval."""
        while True:
            try:
                await self.check_tailscale()
            except Exception:
                self.safe_tailscale = False
                log.exception("error checking Tailscale")
            await asyncio.sleep(TAILSCALE_INTERVAL)

    async def check_tailscale(self) -> None:
        """The Tailscale address is valid only with Funnel off. If it is turned on, it is
        rejected at once and a notice is given once; if it cannot be known, it is also rejected."""
        state = await asyncio.to_thread(self.read_serve)
        before = self.safe_tailscale
        self.safe_tailscale = state.safe
        if state.funnel and not self._funnel_notice:
            self._funnel_notice = True
            log.warning("Tailscale Funnel is on: the panel rejects the Tailscale address")
            await self.engine.notify(
                None,
                "⚠️ Tailscale Funnel is on on this Mac, which would open the panel to the "
                "internet. I've blocked it: until it's turned off, the panel won't open from "
                "your iPhone. Ask Claude Code to check it.",
            )
        elif state.funnel is False:
            self._funnel_notice = False
        if before != self.safe_tailscale:
            if self.safe_tailscale:
                log.info("panel also at https://%s (Tailscale, no Funnel)", self.host_tailscale)
            else:
                log.warning(
                    "the panel stops accepting the Tailscale address: %s",
                    state.error or "Funnel is on",
                )
