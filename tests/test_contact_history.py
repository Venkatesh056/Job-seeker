"""What has already been said to this person, across every channel.

The rule this encodes: a candidate who emailed somebody yesterday and connects on LinkedIn
today has messaged them twice, and the recipient experiences it as twice however the sender
files it. Channels are an implementation detail of the sender's tooling, not of the person
reading — so the duplicate check is one question across all of them, not one per channel.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from careeragent.applications import outreach as ox
from careeragent.applications.outreach import ContactHistory
from careeragent.outreach import gmail, suppression

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def history_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_ENV", "test")
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'h.db'}")
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


# ─── The verdict, as pure logic ─────────────────────────────────────────────


def _event(channel: str, days_ago: int, status: str = "sent") -> dict:
    return {
        "channel": channel,
        "status": status,
        "at": datetime.now(UTC) - timedelta(days=days_ago),
    }


def test_somebody_never_contacted_may_be_contacted():
    allowed, reason = ContactHistory(person_name="Priya").verdict()
    assert allowed
    assert "no previous contact" in reason


def test_an_email_yesterday_blocks_a_linkedin_message_today():
    """The case the whole module exists for."""
    history = ContactHistory(person_name="Priya", events=[_event("email", days_ago=1)])

    allowed, reason = history.verdict()

    assert not allowed
    assert "email" in reason
    assert "whichever channel" in reason


def test_the_block_names_every_channel_already_used():
    history = ContactHistory(events=[_event("email", days_ago=2), _event("linkedin", days_ago=1)])
    _allowed, reason = history.verdict()
    assert "email" in reason and "linkedin" in reason


def test_the_quiet_window_expires():
    history = ContactHistory(events=[_event("email", days_ago=30)])
    allowed, reason = history.verdict()
    assert allowed
    assert "30 days ago" in reason


def test_the_window_is_configurable():
    history = ContactHistory(events=[_event("email", days_ago=10)])
    assert history.verdict(quiet_days=7)[0]
    assert not history.verdict(quiet_days=14)[0]


def test_a_reply_stops_new_messages_entirely():
    """Somebody who answered is in a conversation. Starting a fresh thread at them is
    worse than waiting, however long ago it was."""
    history = ContactHistory(events=[_event("email", days_ago=90)], replied=True)

    allowed, reason = history.verdict()

    assert not allowed
    assert "continue that thread" in reason


def test_suppression_outranks_everything():
    history = ContactHistory(
        events=[_event("email", days_ago=400)], suppressed="they asked not to be contacted"
    )
    allowed, reason = history.verdict()
    assert not allowed
    assert "asked not to be contacted" in reason


def test_the_most_recent_event_is_the_one_that_counts():
    history = ContactHistory(events=[_event("email", days_ago=100), _event("linkedin", days_ago=1)])
    assert history.days_since_last() == 1
    assert not history.verdict()[0]


def test_the_serialised_form_says_whether_it_is_allowed():
    payload = ContactHistory(events=[_event("email", days_ago=1)]).to_dict()
    assert payload["may_contact"] is False
    assert payload["reason"]
    assert payload["channels_used"] == ["email"]
    assert payload["count"] == 1


# ─── Against a real database ────────────────────────────────────────────────


async def _seed(status: str = "approved", sent_days_ago: int | None = None) -> tuple[str, str]:
    from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo
    from careeragent.storage.models import Outreach

    await init_db()
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
            name="Priya Sharma",
        )
        outreach = Outreach(
            candidate_id=row.id,
            company_id=company.id,
            contact_id=contact.id,
            channel="email",
            subject="Interest in your data roles",
            body="A short, specific note.",
            status=status,
            sent_at=(
                datetime.now(UTC) - timedelta(days=sent_days_ago)
                if sent_days_ago is not None
                else None
            ),
        )
        session.add(outreach)
        await session.flush()
        return outreach.id, contact.address


async def test_a_draft_that_was_never_sent_is_not_history(history_db):
    """The recipient has experienced nothing, so it must not block a real message."""
    from careeragent.storage import session_scope

    _outreach_id, address = await _seed(status="drafted")

    async with session_scope() as session:
        history = await ox.history_for(session, email=address)

    assert history.events == []
    assert history.verdict()[0]


async def test_a_sent_message_is_history(history_db):
    from careeragent.storage import session_scope

    _outreach_id, address = await _seed(status="sent", sent_days_ago=1)

    async with session_scope() as session:
        history = await ox.history_for(session, email=address)

    assert len(history.events) == 1
    assert history.channels_used == ["email"]
    assert not history.verdict()[0]


async def test_a_suppressed_person_shows_it_in_their_history(history_db):
    from careeragent.storage import session_scope

    _outreach_id, address = await _seed(status="sent", sent_days_ago=400)
    async with session_scope() as session:
        await suppression.suppress(session, reason=suppression.Reason.OPTED_OUT, email=address)

    async with session_scope() as session:
        history = await ox.history_for(session, email=address)

    assert history.suppressed
    assert not history.verdict()[0]


async def test_a_second_message_inside_the_window_is_refused_at_the_boundary(
    history_db, monkeypatch
):
    """Enforced where sending happens, not left to whoever called."""
    from careeragent.storage import session_scope
    from careeragent.storage.models import Outreach

    _first_id, address = await _seed(status="sent", sent_days_ago=1)

    async def must_not_send(**kwargs):
        raise AssertionError("a second message inside the window must not be sent")

    monkeypatch.setattr(gmail, "send_message", must_not_send)

    # A second, approved message to the same person.
    async with session_scope() as session:
        from sqlalchemy import select

        from careeragent.storage.models import OutreachContact

        contact = (
            await session.execute(select(OutreachContact).where(OutreachContact.address == address))
        ).scalar_one()
        existing = (await session.execute(select(Outreach))).scalars().first()
        second = Outreach(
            candidate_id=existing.candidate_id,
            company_id=existing.company_id,
            contact_id=contact.id,
            # Email, so it reaches the history check rather than being turned away
            # earlier for being a channel Gmail cannot send.
            channel="email",
            subject="Following up",
            body="A second note.",
            status="approved",
        )
        session.add(second)
        await session.flush()
        second_id = second.id

    async with session_scope() as session:
        with pytest.raises(ox.OutreachError, match="whichever channel"):
            await ox.deliver(session, second_id)


async def test_a_first_message_still_goes_through(history_db, monkeypatch):
    """The guard must not become a wall."""
    from careeragent.storage import session_scope
    from careeragent.storage.models import Outreach

    outreach_id, _address = await _seed(status="approved")

    async def accept(**kwargs):
        return gmail.SentMessage(
            message_id="msg-1",
            thread_id="t",
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


async def test_somebody_with_no_address_has_no_history(history_db):
    from careeragent.storage import session_scope

    await _seed()
    async with session_scope() as session:
        history = await ox.history_for(session, email="")

    assert history.events == []
    assert history.verdict()[0]
