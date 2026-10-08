"""Sending an approved message through the user's own Gmail.

Almost everything worth testing here is a refusal. This code can send mail from a person's
account, so the tests that matter are the ones proving it will not: not without approval,
not to a contact with no recorded source, not past the rate limit, and never marking a
record as sent when the send failed.

The scope is `gmail.send` alone. A test asserts that, because a scope is the one security
boundary in this file that cannot be enforced by any amount of careful code — if the list
were wrong, this would be reading the user's mail with their blessing.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta

import pytest

from careeragent.applications import outreach as ox
from careeragent.outreach import gmail

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def oauth(tmp_path, monkeypatch):
    """A configured OAuth client and an isolated token file."""
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "client-id.apps.googleusercontent.com")
    monkeypatch.setenv("GOOGLE_CLIENT_SECRET", "client-secret-value")
    monkeypatch.setenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/cb")
    monkeypatch.setenv("GOOGLE_TOKEN_PATH", str(tmp_path / "token.json"))
    return tmp_path / "token.json"


def _write_token(path, *, refresh="refresh-token", access="access-token", ahead_seconds=3600):
    path.write_text(
        json.dumps(
            {
                "refresh_token": refresh,
                "access_token": access,
                "expires_at": (datetime.now(UTC) + timedelta(seconds=ahead_seconds)).isoformat(),
            }
        ),
        encoding="utf-8",
    )


# ─── The scope ──────────────────────────────────────────────────────────────


def test_the_only_scope_requested_is_send():
    """If this list were wrong, the app would be reading the user's mailbox with their
    blessing. No amount of careful code below can undo a scope granted above."""
    assert gmail.SCOPES == ("https://www.googleapis.com/auth/gmail.send",)
    for forbidden in ("readonly", "modify", "compose", "labels", "https://mail.google.com/"):
        assert not any(forbidden in scope for scope in gmail.SCOPES)


def test_the_consent_link_asks_for_offline_access(oauth):
    """Without access_type=offline and prompt=consent, Google returns no refresh token and
    authorisation silently stops working an hour later."""
    url = gmail.authorization_url()

    assert "access_type=offline" in url
    assert "prompt=consent" in url
    assert "gmail.send" in url
    assert "client-id.apps.googleusercontent.com" in url


def test_there_is_no_consent_link_without_a_client(monkeypatch):
    monkeypatch.delenv("GOOGLE_CLIENT_ID", raising=False)
    monkeypatch.delenv("GOOGLE_CLIENT_SECRET", raising=False)

    with pytest.raises(gmail.GmailError, match="GOOGLE_CLIENT_ID"):
        gmail.authorization_url()


def test_being_configured_is_not_the_same_as_being_authorised(oauth):
    assert gmail.configured()
    assert not gmail.authorised(), "an OAuth client is not a user's consent"

    _write_token(oauth)
    assert gmail.authorised()


# ─── The token ──────────────────────────────────────────────────────────────


def test_a_refresh_response_does_not_destroy_the_refresh_token(oauth):
    """Google returns `refresh_token` only on the first authorisation. Writing a later
    refresh response verbatim would erase the user's consent, and the only fix would be
    sending them back through the consent screen."""
    _write_token(oauth, refresh="the-only-one")

    gmail._store({"access_token": "new-access", "expires_at": "2030-01-01T00:00:00+00:00"})

    assert json.loads(oauth.read_text())["refresh_token"] == "the-only-one"


def test_a_corrupt_token_file_reads_as_not_authorised(oauth):
    oauth.write_text("{not json", encoding="utf-8")

    assert gmail._stored() is None
    assert not gmail.authorised()


def test_forgetting_removes_the_token(oauth):
    _write_token(oauth)

    assert gmail.forget() is True
    assert not oauth.exists()
    assert gmail.forget() is False, "forgetting twice is not an error"


# ─── The message ────────────────────────────────────────────────────────────


def test_a_message_is_built_as_plain_text():
    """Plain text only. HTML is a way to hide a link's real destination, which is not
    something this project wants to be able to do."""
    raw = gmail.build_mime(to="someone@example.com", subject="Hello", body="A short note.")
    decoded = base64.urlsafe_b64decode(raw).decode("utf-8")

    assert "To: someone@example.com" in decoded
    assert "Subject: Hello" in decoded
    assert "A short note." in decoded
    assert "text/html" not in decoded


@pytest.mark.parametrize("address", ["", "not-an-address", "missing@domain", "a b@c.com"])
def test_a_message_needs_a_real_address(address):
    with pytest.raises(gmail.GmailError):
        gmail.build_mime(to=address, subject="Hi", body="Body")


def test_a_message_needs_a_subject_and_a_body():
    with pytest.raises(gmail.GmailError, match="subject"):
        gmail.build_mime(to="a@b.com", subject="   ", body="Body")
    with pytest.raises(gmail.GmailError, match="body"):
        gmail.build_mime(to="a@b.com", subject="Hi", body="  ")


# ─── Sending ────────────────────────────────────────────────────────────────


class FakeResponse:
    def __init__(self, status=200, payload=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}

    @property
    def is_success(self):
        return self.status_code < 400

    def json(self):
        return self._payload


class FakeClient:
    """Records every request instead of making one."""

    def __init__(self, *responses):
        self._responses = list(responses)
        self.posts: list[tuple[str, dict]] = []
        self.gets: list[str] = []

    async def post(self, url, json=None, data=None, headers=None):
        self.posts.append((url, json or data or {}))
        return self._responses.pop(0) if self._responses else FakeResponse()

    async def get(self, url, headers=None):
        self.gets.append(url)
        return self._responses.pop(0) if self._responses else FakeResponse()

    async def aclose(self):
        return None


async def test_a_send_returns_the_message_id_as_evidence(oauth):
    _write_token(oauth)
    client = FakeClient(FakeResponse(200, {"id": "msg-123", "threadId": "thr-9"}))

    sent = await gmail.send_message(
        to="someone@example.com", subject="Hi", body="Note", client=client
    )

    assert sent.message_id == "msg-123"
    assert sent.thread_id == "thr-9"
    assert client.posts[0][0] == gmail.SEND_URL


async def test_a_response_with_no_message_id_is_not_a_send(oauth):
    """An outreach record must never be marked sent on a hopeful reading of an ambiguous
    response."""
    _write_token(oauth)
    client = FakeClient(FakeResponse(200, {}))

    with pytest.raises(gmail.GmailError, match="no message id"):
        await gmail.send_message(to="someone@example.com", subject="Hi", body="Note", client=client)


async def test_sending_without_consent_says_what_to_do(oauth):
    client = FakeClient()

    with pytest.raises(gmail.GmailError, match="not authorised"):
        await gmail.send_message(to="a@b.com", subject="Hi", body="Note", client=client)

    assert client.posts == [], "nothing should have been attempted"


async def test_an_expired_token_is_refreshed_before_sending(oauth):
    _write_token(oauth, access="stale", ahead_seconds=-60)
    client = FakeClient(
        FakeResponse(200, {"access_token": "fresh", "expires_in": 3600}),
        FakeResponse(200, {"id": "msg-1", "threadId": "t"}),
    )

    await gmail.send_message(to="a@b.com", subject="Hi", body="Note", client=client)

    assert client.posts[0][0] == gmail.TOKEN_URL
    assert client.posts[1][0] == gmail.SEND_URL
    assert json.loads(oauth.read_text())["access_token"] == "fresh"


async def test_a_valid_token_is_not_refreshed(oauth):
    _write_token(oauth, ahead_seconds=3600)
    client = FakeClient(FakeResponse(200, {"id": "m", "threadId": "t"}))

    await gmail.send_message(to="a@b.com", subject="Hi", body="Note", client=client)

    assert [url for url, _ in client.posts] == [gmail.SEND_URL]


async def test_a_revoked_consent_reads_as_such(oauth):
    _write_token(oauth, ahead_seconds=-60)
    client = FakeClient(FakeResponse(400, {"error": "invalid_grant"}))

    with pytest.raises(gmail.GmailError, match="authorise again"):
        await gmail.send_message(to="a@b.com", subject="Hi", body="Note", client=client)


# ─── What outreach.deliver refuses ──────────────────────────────────────────


async def test_delivering_an_unapproved_message_is_refused(isolated, monkeypatch):
    """The whole approval chain exists to be in the way here."""
    from careeragent.storage import session_scope

    outreach_id, _ = await _seed_outreach(status="drafted")

    async def must_not_send(**kwargs):
        raise AssertionError("an unapproved message must never reach the transport")

    monkeypatch.setattr(gmail, "send_message", must_not_send)

    async with session_scope() as session:
        with pytest.raises(ox.OutreachError, match="approved"):
            await ox.deliver(session, outreach_id)


async def test_delivering_to_a_contact_with_no_permitted_source_is_refused(isolated, monkeypatch):
    """A contact with no recorded provenance cannot have come from a page the company
    published, and this project has no other legitimate way to have obtained one."""
    from careeragent.storage import session_scope
    from careeragent.storage.models import OutreachContact

    outreach_id, contact_id = await _seed_outreach(status="approved")

    async def must_not_send(**kwargs):
        raise AssertionError("a contact with no permitted source must never be messaged")

    monkeypatch.setattr(gmail, "send_message", must_not_send)

    async with session_scope() as session:
        contact = await session.get(OutreachContact, contact_id)
        contact.source = "scraped_from_linkedin"
        await session.flush()

    async with session_scope() as session:
        with pytest.raises(ox.OutreachError, match="permitted recorded source"):
            await ox.deliver(session, outreach_id)


async def test_a_failed_send_leaves_the_record_approved(isolated, monkeypatch):
    """Marking it sent would lose the message and claim something that did not happen."""
    from careeragent.storage import session_scope
    from careeragent.storage.models import Outreach

    outreach_id, _ = await _seed_outreach(status="approved")

    async def refuse(**kwargs):
        raise gmail.GmailError("Gmail refused the message")

    monkeypatch.setattr(gmail, "send_message", refuse)

    async with session_scope() as session:
        with pytest.raises(ox.OutreachError, match="refused"):
            await ox.deliver(session, outreach_id)

    async with session_scope() as session:
        row = await session.get(Outreach, outreach_id)
        assert row.status == "approved", "still approved, so it can be retried"
        assert row.sent_at is None
        assert row.transport_message_id is None


async def test_a_successful_send_records_the_evidence(isolated, monkeypatch):
    from careeragent.storage import session_scope
    from careeragent.storage.models import Outreach

    outreach_id, _ = await _seed_outreach(status="approved")

    async def accept(**kwargs):
        return gmail.SentMessage(
            message_id="msg-abc",
            thread_id="thr-1",
            to=kwargs["to"],
            subject=kwargs["subject"],
            sent_at=datetime.now(UTC),
        )

    monkeypatch.setattr(gmail, "send_message", accept)

    async with session_scope() as session:
        await ox.deliver(session, outreach_id)

    async with session_scope() as session:
        row = await session.get(Outreach, outreach_id)
        assert row.status == "sent"
        assert row.transport == "gmail"
        assert row.transport_message_id == "msg-abc"
        assert row.sent_at is not None
        assert row.follow_up_date is not None


# ─── Fixtures ───────────────────────────────────────────────────────────────


async def _seed_outreach(*, status: str) -> tuple[str, str]:
    from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo
    from careeragent.storage.models import Outreach

    await init_db(drop=True)
    candidate = Candidate(
        name="T",
        email="t@example.com",
        facts=[
            Fact(
                kind=FactKind.SKILL,
                label="python",
                evidence_source=EvidenceSource.MASTER_RESUME,
                evidence_locator="cv.pdf#page=1",
            )
        ],
    )
    async with session_scope() as session:
        row = await repo.save_candidate_profile(session, candidate)
        company = await repo.upsert_company(session, "Acme", "acme.com")
        await session.flush()

        contact = await ox.add_contact(
            session,
            company.id,
            channel="email",
            address="hiring@acme.com",
            source="careers_page",
            name="Hiring Team",
        )
        outreach = Outreach(
            candidate_id=row.id,
            company_id=company.id,
            contact_id=contact.id,
            channel="email",
            subject="Interest in your data roles",
            body="A short, specific note.",
            status=status,
        )
        session.add(outreach)
        await session.flush()
        return outreach.id, contact.id


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'g.db'}")
    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("CAREERAGENT_CACHE_DIR", str(tmp_path / "cache"))

    from careeragent import config as config_module
    from careeragent import storage

    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
    yield tmp_path
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
