"""Sending an approved message through the user's own Gmail.

Written against the REST API with `httpx` rather than Google's client libraries. That is a
deliberate trade: the official stack pulls in a large dependency tree, and the two things
needed here — refresh an access token, post one message — are a form POST and a JSON POST.
Fewer moving parts, and the scope is visible in one place instead of inferred from a
library's defaults.

Four rules, and the first three are the reason this file is small.

**One scope, and it is send-only.** `gmail.send` cannot read a mailbox, cannot list
threads, cannot modify or delete anything. If this code were wrong in the worst way it
could send a message; it could not read the user's mail. Reply detection would need
`gmail.readonly`, which is a much larger ask, and is deliberately not requested — the
outreach tracker already records replies the user tells it about.

**Nothing sends itself.** `send_message` is the transport and does exactly what it is
told. The decision to send belongs to `applications.outreach`, which requires an approved
record and enforces the rate limits, and above that to a person pressing a button.

**The token is a secret and is treated as one.** It lives in a gitignored file, is never
logged, and is never returned by any API in this project.

**Consent is the user's, and it is interactive.** There is no way to authorise this without
a human visiting a Google consent screen. `authorization_url()` builds the link and
`exchange_code()` completes it; neither can be done on the user's behalf.
"""

from __future__ import annotations

import base64
import contextlib
import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from ..logging import get_logger

log = get_logger("outreach.gmail")

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
SEND_URL = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
PROFILE_URL = "https://gmail.googleapis.com/gmail/v1/users/me/profile"

# The only scope requested. Send, and nothing else — this cannot read the user's mailbox.
SCOPES = ("https://www.googleapis.com/auth/gmail.send",)

DEFAULT_TOKEN_PATH = "data/google_token.json"

# Refresh a little before expiry rather than on it, so a send does not fail on a token
# that was valid when the request was assembled and stale by the time it arrived.
REFRESH_MARGIN = timedelta(seconds=120)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class GmailError(RuntimeError):
    """A refusal or failure the user should see."""


def _client_id() -> str:
    return os.getenv("GOOGLE_CLIENT_ID", "").strip()


def _client_secret() -> str:
    return os.getenv("GOOGLE_CLIENT_SECRET", "").strip()


def _redirect_uri() -> str:
    return os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/api/v1/outreach/gmail/callback")


def token_path() -> Path:
    return Path(os.getenv("GOOGLE_TOKEN_PATH", DEFAULT_TOKEN_PATH))


def configured() -> bool:
    """Is there an OAuth client? Not the same as being authorised."""
    return bool(_client_id() and _client_secret())


def authorised() -> bool:
    """Has the user completed consent and given us a refresh token?"""
    return bool((_stored() or {}).get("refresh_token"))


# ─── The stored token ───────────────────────────────────────────────────────


def _stored() -> dict[str, Any] | None:
    path = token_path()
    try:
        if not path.exists():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError) as exc:
        log.warning("gmail_token_unreadable", error=f"{type(exc).__name__}")
        return None


def _store(data: dict[str, Any]) -> None:
    """Write the token, preserving a refresh token the response omitted.

    Google returns `refresh_token` only on the first authorisation. A later refresh
    response carries a new access token and no refresh token, and writing that verbatim
    would silently destroy the user's consent — the next send would fail and the only fix
    would be going through the consent screen again.
    """
    path = token_path()
    path.parent.mkdir(parents=True, exist_ok=True)

    previous = _stored() or {}
    if not data.get("refresh_token") and previous.get("refresh_token"):
        data["refresh_token"] = previous["refresh_token"]

    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    # Owner-only where the platform honours it. Windows ignores the mode, which is why
    # the file also lives under a gitignored directory rather than relying on this.
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def forget() -> bool:
    """Delete the stored token. The user's consent at Google is theirs to revoke."""
    path = token_path()
    if path.exists():
        path.unlink()
        log.info("gmail_token_deleted")
        return True
    return False


# ─── Consent ────────────────────────────────────────────────────────────────


def authorization_url(state: str = "") -> str:
    """The Google consent screen to send the user to.

    `access_type=offline` with `prompt=consent` is what makes Google return a refresh
    token; without both, authorisation lasts an hour and then silently stops working.
    """
    if not configured():
        raise GmailError(
            "no Google OAuth client configured — set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET"
        )
    params = {
        "client_id": _client_id(),
        "redirect_uri": _redirect_uri(),
        "response_type": "code",
        "scope": " ".join(SCOPES),
        "access_type": "offline",
        "prompt": "consent",
        "include_granted_scopes": "true",
    }
    if state:
        params["state"] = state
    return f"{AUTH_URL}?{urlencode(params)}"


async def exchange_code(code: str, *, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Turn the one-time code from the consent redirect into a stored token."""
    if not configured():
        raise GmailError("no Google OAuth client configured")
    if not code:
        raise GmailError("no authorization code given")

    owns = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        response = await client.post(
            TOKEN_URL,
            data={
                "code": code,
                "client_id": _client_id(),
                "client_secret": _client_secret(),
                "redirect_uri": _redirect_uri(),
                "grant_type": "authorization_code",
            },
        )
    finally:
        if owns:
            await client.aclose()

    if response.status_code >= 400:
        raise GmailError(f"Google refused the authorization code (HTTP {response.status_code})")

    payload = response.json()
    if not payload.get("refresh_token"):
        raise GmailError(
            "Google returned no refresh token. Revoke this app's access in your Google "
            "account and authorise again — without one, access expires in an hour."
        )
    _store(_with_expiry(payload))
    log.info("gmail_authorised", scopes=payload.get("scope"))
    return {"authorised": True, "scope": payload.get("scope")}


def _with_expiry(payload: dict[str, Any]) -> dict[str, Any]:
    seconds = int(payload.get("expires_in") or 0)
    payload = dict(payload)
    payload["expires_at"] = (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()
    payload.pop("expires_in", None)
    return payload


async def _access_token(client: httpx.AsyncClient) -> str:
    """A valid access token, refreshing it if the stored one is spent."""
    stored = _stored()
    if not stored or not stored.get("refresh_token"):
        raise GmailError(
            "Gmail is not authorised. Open the consent link from the Outreach page once; "
            "it cannot be done on your behalf."
        )

    expires_at = stored.get("expires_at")
    if stored.get("access_token") and expires_at:
        try:
            if datetime.fromisoformat(expires_at) - REFRESH_MARGIN > datetime.now(UTC):
                return str(stored["access_token"])
        except ValueError:
            pass

    response = await client.post(
        TOKEN_URL,
        data={
            "refresh_token": stored["refresh_token"],
            "client_id": _client_id(),
            "client_secret": _client_secret(),
            "grant_type": "refresh_token",
        },
    )
    if response.status_code >= 400:
        raise GmailError(
            "Google would not refresh the token. Your consent may have been revoked; "
            "authorise again from the Outreach page."
        )
    payload = _with_expiry(response.json())
    _store(payload)
    return str(payload.get("access_token") or "")


# ─── Sending ────────────────────────────────────────────────────────────────


@dataclass
class SentMessage:
    """Proof that Gmail accepted a message. Absent proof, nothing is claimed as sent."""

    message_id: str
    thread_id: str
    to: str
    subject: str
    sent_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "thread_id": self.thread_id,
            "to": self.to,
            "subject": self.subject,
            "sent_at": self.sent_at.isoformat(),
        }


def build_mime(*, to: str, subject: str, body: str, from_name: str = "") -> str:
    """One plain-text message, base64url encoded the way Gmail wants it.

    Plain text only. An outreach message that needs HTML to make its point is a message
    that should be shorter, and HTML is a way to hide a link's real destination — which is
    not something this project wants to be able to do.
    """
    if not _EMAIL.match((to or "").strip()):
        raise GmailError(f"{to!r} is not an email address")
    if not subject.strip():
        raise GmailError("a message needs a subject")
    if not body.strip():
        raise GmailError("a message needs a body")

    message = EmailMessage()
    message["To"] = to.strip()
    message["Subject"] = subject.strip()
    if from_name:
        message["From"] = from_name
    message.set_content(body)
    return base64.urlsafe_b64encode(message.as_bytes()).decode("ascii")


async def send_message(
    *,
    to: str,
    subject: str,
    body: str,
    from_name: str = "",
    thread_id: str = "",
    client: httpx.AsyncClient | None = None,
) -> SentMessage:
    """Send one message. The transport, and only the transport.

    This does not decide whether a message should be sent, who it goes to, or how many
    have gone today. `applications.outreach` owns all of that — it requires an approved
    record with a permitted contact source and enforces the daily and weekly limits — and
    above that a person presses the button. Keeping the decision out of here is what makes
    it possible to say with confidence that nothing sends itself.
    """
    raw = build_mime(to=to, subject=subject, body=body, from_name=from_name)

    owns = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        token = await _access_token(client)
        payload: dict[str, Any] = {"raw": raw}
        if thread_id:
            payload["threadId"] = thread_id

        response = await client.post(
            SEND_URL,
            json=payload,
            headers={"authorization": f"Bearer {token}"},
        )
    except httpx.HTTPError as exc:
        raise GmailError(f"could not reach Gmail: {type(exc).__name__}") from exc
    finally:
        if owns:
            await client.aclose()

    if response.status_code >= 400:
        detail = ""
        with contextlib.suppress(ValueError, AttributeError):
            detail = str((response.json().get("error") or {}).get("message") or "")[:160]
        raise GmailError(
            f"Gmail refused the message (HTTP {response.status_code}) {detail}".strip()
        )

    body_json = response.json()
    message_id = str(body_json.get("id") or "")
    if not message_id:
        # No id means no evidence it was accepted, and an outreach record must never be
        # marked sent on a hopeful reading of an ambiguous response.
        raise GmailError("Gmail returned no message id; treating this as not sent")

    sent = SentMessage(
        message_id=message_id,
        thread_id=str(body_json.get("threadId") or ""),
        to=to.strip(),
        subject=subject.strip(),
        sent_at=datetime.now(UTC),
    )
    # The recipient is deliberately absent from the log line.
    log.info("gmail_sent", message_id=sent.message_id, thread_id=sent.thread_id)
    return sent


async def status(*, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """What state the Gmail connection is in, without sending anything."""
    if not configured():
        return {
            "configured": False,
            "authorised": False,
            "note": "No Google OAuth client. Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET.",
        }
    if not authorised():
        return {
            "configured": True,
            "authorised": False,
            "scopes": list(SCOPES),
            "note": (
                "Configured but not authorised. Open the consent link once — it asks only "
                "for permission to send, not to read your mail."
            ),
        }

    owns = client is None
    client = client or httpx.AsyncClient(timeout=30.0)
    try:
        token = await _access_token(client)
        response = await client.get(PROFILE_URL, headers={"authorization": f"Bearer {token}"})
        address = (response.json() or {}).get("emailAddress", "") if response.is_success else ""
    except (GmailError, httpx.HTTPError) as exc:
        return {
            "configured": True,
            "authorised": True,
            "working": False,
            "note": f"Authorised, but Gmail did not answer: {exc}",
        }
    finally:
        if owns:
            await client.aclose()

    return {
        "configured": True,
        "authorised": True,
        "working": bool(address),
        "account": address,
        "scopes": list(SCOPES),
        "note": "Send-only access. This cannot read your mailbox.",
    }
