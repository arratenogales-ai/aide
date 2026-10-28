"""Google authorization.

- Minimal permissions: gmail.readonly, gmail.compose and calendar.events. None allows
  deleting, moving to the trash or changing email labels (verified against Google: those
  calls return 403). gmail.compose does allow deleting drafts, but no code does so
  (tests/test_google.py watches for this).
- On authorizing and on renewing, the code checks that Google has not granted extra
  permissions; if there are any, the token is neither saved nor used
  (ExcessivePermissions).
- The token lives in the macOS keychain (keyring), never in files or logs.
- The OAuth app is in “Testing” status: the refresh token expires after 7 days. An
  `invalid_grant` becomes `GoogleNotAuthorized`, which pauses only the Google tasks.
- First consent: `uv run python -m aide.tools.google_auth`.
- Reauthorization: link in the local panel (redirect to 127.0.0.1, desktop client).
"""

from __future__ import annotations

import json
import logging
import secrets
import threading
from collections.abc import Iterable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import keyring
import requests
from google.auth.exceptions import RefreshError
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow, InstalledAppFlow
from googleapiclient.discovery import build

from aide.common.config import Config
from aide.common.logs import log_secret

log = logging.getLogger(__name__)

SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/calendar.events",
]
KEYCHAIN_SERVICE = "aide"
KEYCHAIN_ACCOUNT = "google-oauth"
TOKENINFO = "https://oauth2.googleapis.com/tokeninfo"


class GoogleNotAuthorized(Exception):
    """Google authorization is missing or has expired (invalid_grant)."""


class GoogleUnauthorized(GoogleNotAuthorized):
    """Google has never been authorized on this Mac (there is nothing in the keychain)."""


class ExcessivePermissions(GoogleNotAuthorized):
    """Google has granted permissions the program does not request (for example, gmail.modify)."""


def granted_scopes(creds: Credentials) -> set[str] | None:
    """Scopes Google returned when issuing or renewing the token (None if it gave none)."""
    granted = creds.granted_scopes
    if granted is None:
        return None
    if isinstance(granted, str):
        granted = granted.split()
    return set(granted)


def check_permissions(granted: Iterable[str]) -> None:
    """Only the permissions in SCOPES are accepted; any other invalidates the token."""
    leftover = sorted(set(granted) - set(SCOPES))
    if leftover:
        raise ExcessivePermissions(
            "Google granted permissions that Aide does not ask for ("
            + ", ".join(leftover)
            + "). This token will not be used: authorize again from the panel link."
        )


class WrongAccount(Exception):
    """A different account from the configured one was authorized."""


class GoogleAuthorization:
    """Handles Google authorization, with credentials stored in the system keychain."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.credentials_path = (
            config.paths.root / config.environment.GOOGLE_CREDENTIALS_PATH
        ).resolve()
        self._lock = threading.Lock()
        self._creds: Credentials | None = None
        self._flows: dict[str, Flow] = {}

    # --- storage ------------------------------------------------------------------

    def _read_keychain(self) -> dict[str, Any] | None:
        text = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        return json.loads(text) if text else None

    def _save(
        self, creds: Credentials, account: str | None = None, authorized_in: str | None = None
    ) -> None:
        data = json.loads(creds.to_json())
        previous = self._read_keychain() or {}
        data["_account"] = account or previous.get("_account")
        data["_authorized_in"] = authorized_in or previous.get("_authorized_in")
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, json.dumps(data))
        for secret in (creds.token, creds.refresh_token):
            log_secret(secret)

    def forget(self) -> None:
        """Deletes the stored Google credentials from the keychain."""
        try:
            keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        except keyring.errors.PasswordDeleteError:
            pass
        self._creds = None

    # --- state --------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        """Returns whether Google is authorized, with the account and expiry date."""
        data = self._read_keychain()
        if not data:
            return {"authorized": False}
        authorized_in = data.get("_authorized_in")
        expires = None
        if authorized_in:
            expires = (
                datetime.fromisoformat(authorized_in)
                + timedelta(days=self.config.settings.google.validity_days)
            ).isoformat(timespec="minutes")
        return {
            "authorized": True,
            "account": data.get("_account"),
            "authorized_in": authorized_in,
            "expires": expires,
        }

    def expires_in(self) -> datetime | None:
        value = self.state().get("expires")
        return datetime.fromisoformat(value) if value else None

    # --- credentials ----------------------------------------------------------------------

    def credentials(self) -> Credentials:
        """Valid credentials (renewed if necessary). Blocking: use in a thread."""
        with self._lock:
            creds = self._creds
            if creds is None:
                data = self._read_keychain()
                if not data:
                    raise GoogleUnauthorized("Google is not authorized yet")
                info = {k: v for k, v in data.items() if not k.startswith("_")}
                creds = Credentials.from_authorized_user_info(info, SCOPES)
            if not creds.valid:
                if not creds.refresh_token:
                    raise GoogleNotAuthorized("there is no refresh token")
                try:
                    creds.refresh(Request())
                except RefreshError as e:
                    self._creds = None
                    raise GoogleNotAuthorized(f"the Google authorization has expired ({e})") from e
                granted = granted_scopes(creds)
                if granted is not None:
                    try:
                        check_permissions(granted)
                    except ExcessivePermissions:
                        self._creds = None
                        raise
                self._save(creds)
            self._creds = creds
            return creds

    def service(self, name: str, version: str) -> Any:
        return build(name, version, credentials=self.credentials(), cache_discovery=False)

    def authorized_email(self, creds: Credentials) -> str:
        gmail = build("gmail", "v1", credentials=creds, cache_discovery=False)
        return gmail.users().get_profile(user_id="me").execute()["emailAddress"]

    def query_tokeninfo(self, creds: Credentials) -> list[str]:
        """Scopes Google associates with the access token (the token goes in the body, not in the
        URL)."""
        reply = requests.post(TOKENINFO, data={"access_token": creds.token}, timeout=20)
        reply.raise_for_status()
        return sorted(reply.json().get("scope", "").split())

    def granted_permissions(self) -> list[str]:
        """Diagnostic: the scopes of the current token according to Google."""
        return self.query_tokeninfo(self.credentials())

    def _check_new_permissions(self, creds: Credentials) -> None:
        granted = granted_scopes(creds)
        if granted is None:
            granted = set(self.query_tokeninfo(creds))
        check_permissions(granted)

    def _check_account(self, creds: Credentials) -> str:
        email = self.authorized_email(creds)
        expected = self.config.google_account
        if expected and email.lower() != expected.lower():
            raise WrongAccount(f"{email} was authorized, but the configured account is {expected}")
        return email

    # --- authorization flows ----------------------------------------------------------------

    def authorize_local(self) -> str:
        """First consent: opens the browser and waits for the local redirect."""
        flow = InstalledAppFlow.from_client_secrets_file(str(self.credentials_path), SCOPES)
        creds = flow.run_local_server(
            host="localhost",
            port=0,
            open_browser=True,
            authorization_prompt_message="Open this address to authorize Aide: {url}",
            success_message="Authorization complete. You can close this window.",
            access_type="offline",
            prompt="consent",
            login_hint=self.config.google_account or None,
        )
        self._check_new_permissions(creds)
        email = self._check_account(creds)
        self._save(creds, email, datetime.now(UTC).isoformat(timespec="seconds"))
        self._creds = creds
        return email

    def start_web(self, redirect_uri: str) -> str:
        """Consent URL for the panel link (desktop client, loopback)."""
        flow = Flow.from_client_secrets_file(
            str(self.credentials_path), SCOPES, redirect_uri=redirect_uri
        )
        state = secrets.token_urlsafe(24)
        url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            # Without include_granted_scopes: the new token never inherits permissions
            # granted before.
            login_hint=self.config.google_account or "",
            state=state,
        )
        self._flows[state] = flow
        return url

    def complete_web(self, state: str, code: str) -> str:
        """Exchanges the redirect code, checks the account and saves the token."""
        flow = self._flows.pop(state, None)
        if flow is None:
            raise GoogleNotAuthorized(
                "the authorization link is no longer valid; ask for a new one"
            )
        flow.fetch_token(code=code)
        creds = flow.credentials
        self._check_new_permissions(creds)
        email = self._check_account(creds)
        self._save(creds, email, datetime.now(UTC).isoformat(timespec="seconds"))
        self._creds = creds
        return email


def main(argv: list[str] | None = None) -> None:
    """First consent from the terminal (the user's browser opens).

    With `--permissions` it authorizes nothing: it shows the permissions Google associates with
    the current token and checks that they are exactly those of SCOPES.
    """
    import sys

    from aide.main import prepare

    arguments = sys.argv[1:] if argv is None else argv
    config, _ = prepare(console=False)
    auth = GoogleAuthorization(config)
    if not Path(auth.credentials_path).exists():
        raise SystemExit(f"{auth.credentials_path} does not exist")
    if "--permissions" in arguments:
        granted = auth.granted_permissions()
        print("Permissions granted according to Google:")
        for scope in granted:
            print(f"- {scope}")
        try:
            check_permissions(granted)
        except ExcessivePermissions as e:
            raise SystemExit(str(e)) from e
        print("OK: only Aide's permissions (none of them can delete or move emails).")
        return
    try:
        email = auth.authorize_local()
    except WrongAccount as e:
        # The token of the wrong account was not saved; the previous one (if any) stays as
        # it was.
        raise SystemExit(f"Wrong account: {e}. Try again with the right account.") from e
    except ExcessivePermissions as e:
        raise SystemExit(str(e)) from e
    print(f"Authorization OK for {email}. It expires on {auth.state().get('expires')}.")


if __name__ == "__main__":
    main()


async def expired_authorization(ses: Any, e: Exception) -> dict[str, Any]:
    """What a Gmail or Calendar tool returns when the authorization is no longer valid: the
    task is paused (if the core knows how) and the agent does not look for another route."""
    from aide.tools.server import error

    handler = ses.core.services.get("expired_google")
    if handler is not None:
        await handler(ses)
    return error(
        f"The Google authorization is not valid ({e}). The user has to renew it; "
        "the task is paused and will resume on its own. Do not try another way."
    )
