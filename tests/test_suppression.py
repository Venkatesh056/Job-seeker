"""Who must not be contacted.

This is the list that has to be right. Every other outreach failure can be walked back — a
badly personalised email is embarrassing, a duplicate is annoying — but writing to somebody
who asked you to stop cannot be undone. So the tests here are about absolutes, not degrees.

The two that matter most: a suppressed contact cannot be reached through `deliver`, and
cannot be reached through `send` either — because marking a message sent by hand must not
be a way around the list.
"""

from __future__ import annotations

import pytest

from careeragent.applications import outreach as ox
from careeragent.outreach import gmail, suppression
from careeragent.outreach.suppression import Reason

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def suppression_db(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_ENV", "test")
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
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


# ─── What the reasons mean ──────────────────────────────────────────────────


def test_a_bounce_is_about_the_address_and_an_opt_out_is_about_the_person():
    """Collapsing these into one flag loses the difference, and the first time a new
    address is found for an opted-out contact the system writes to them again."""
    assert Reason.BOUNCED.about_the_address
    assert not Reason.OPTED_OUT.about_the_address


def test_only_an_opt_out_needs_a_deliberate_act_to_lift():
    assert not Reason.OPTED_OUT.user_may_lift
    for reason in (Reason.BOUNCED, Reason.DO_NOT_CONTACT, Reason.WRONG_PERSON):
        assert reason.user_may_lift


@pytest.mark.parametrize("reason", list(Reason))
def test_every_reason_explains_itself_in_a_sentence(reason):
    """The explanation is what a user reads when a send is refused, so it has to be a
    sentence rather than a slug."""
    explanation = reason.explain()
    assert explanation
    assert " " in explanation
    assert "_" not in explanation


# ─── Reading a reply ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "reply",
    [
        "Please unsubscribe me.",
        "Remove me from your list",
        "Stop contacting me",
        "Do not contact me again",
        "I no longer wish to receive these",
        "take me off this list please",
    ],
)
def test_a_request_to_stop_is_recognised(reply):
    assert suppression.reads_as_opt_out(reply)


@pytest.mark.parametrize(
    "reply",
    [
        "Thanks, not interested in this role but keep in touch.",
        "We have no openings right now.",
        "Could you send your resume?",
        "I'm not interested in receiving a discount, but tell me about the role",
        "",
    ],
)
def test_an_ordinary_reply_is_not_read_as_an_opt_out(reply):
    """A false positive here silently ends a real conversation, which is worse than
    missing one and leaving it to the user."""
    assert not suppression.reads_as_opt_out(reply)


# ─── The list itself ────────────────────────────────────────────────────────


async def _fresh():
    from careeragent import storage

    await storage.init_db()


async def test_a_suppressed_address_is_found(suppression_db):
    from careeragent.storage import session_scope

    await _fresh()
    async with session_scope() as session:
        await suppression.suppress(
            session, reason=Reason.OPTED_OUT, email="someone@acme.com", note="asked to stop"
        )

    async with session_scope() as session:
        found = await suppression.find(session, email="someone@acme.com")
        assert found is not None
        assert found.reason is Reason.OPTED_OUT
        assert "asked not to be contacted" in found.explain()


async def test_the_lookup_ignores_case_and_whitespace(suppression_db):
    from careeragent.storage import session_scope

    await _fresh()
    async with session_scope() as session:
        await suppression.suppress(session, reason=Reason.BOUNCED, email="Someone@Acme.com")

    async with session_scope() as session:
        assert await suppression.is_suppressed(session, email="  someone@acme.com  ")


async def test_a_person_is_suppressed_at_any_address(suppression_db):
    """An opt-out follows the person. A better address for them is not a way back in."""
    from careeragent.storage import session_scope

    await _fresh()
    async with session_scope() as session:
        await suppression.suppress(session, reason=Reason.OPTED_OUT, person_id="person-1")

    async with session_scope() as session:
        assert await suppression.is_suppressed(session, person_id="person-1")


async def test_suppressing_needs_something_to_suppress(suppression_db):
    from careeragent.storage import session_scope

    await _fresh()
    async with session_scope() as session:
        with pytest.raises(ValueError, match="email address or a person id"):
            await suppression.suppress(session, reason=Reason.BOUNCED)


async def test_suppressing_twice_does_not_duplicate(suppression_db):
    from careeragent.storage import session_scope

    await _fresh()
    async with session_scope() as session:
        await suppression.suppress(session, reason=Reason.BOUNCED, email="a@acme.com")
        await suppression.suppress(session, reason=Reason.BOUNCED, email="a@acme.com")

    async with session_scope() as session:
        assert len(await suppression.listing(session)) == 1


async def test_a_bounce_can_be_lifted_and_an_opt_out_cannot(suppression_db):
    from careeragent.storage import session_scope

    await _fresh()
    async with session_scope() as session:
        await suppression.suppress(session, reason=Reason.BOUNCED, email="bounced@acme.com")
        await suppression.suppress(session, reason=Reason.OPTED_OUT, email="gone@acme.com")

    async with session_scope() as session:
        assert await suppression.lift(session, email="bounced@acme.com")

    async with session_scope() as session:
        with pytest.raises(ValueError, match="deliberate act"):
            await suppression.lift(session, email="gone@acme.com")


async def test_an_opt_out_can_be_lifted_when_it_is_meant(suppression_db):
    """People do change their minds and say so. It is a separate gesture, not the same
    button that clears a bounce."""
    from careeragent.storage import session_scope

    await _fresh()
    async with session_scope() as session:
        await suppression.suppress(session, reason=Reason.OPTED_OUT, email="gone@acme.com")

    async with session_scope() as session:
        assert await suppression.lift(session, email="gone@acme.com", force=True)

    async with session_scope() as session:
        assert not await suppression.is_suppressed(session, email="gone@acme.com")


async def test_lifting_somebody_who_is_not_on_the_list_is_not_an_error(suppression_db):
    from careeragent.storage import session_scope

    await _fresh()
    async with session_scope() as session:
        assert not await suppression.lift(session, email="nobody@acme.com")


# ─── The boundary ───────────────────────────────────────────────────────────


async def _seed_outreach(status: str = "approved") -> tuple[str, str]:
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
        return outreach.id, contact.address


async def test_a_suppressed_contact_is_never_delivered_to(suppression_db, monkeypatch):
    """The whole point. Checked at the boundary, not trusted to the caller."""
    from careeragent.storage import session_scope

    outreach_id, address = await _seed_outreach()

    async def must_not_send(**kwargs):
        raise AssertionError("a suppressed contact must never reach the transport")

    monkeypatch.setattr(gmail, "send_message", must_not_send)

    async with session_scope() as session:
        await suppression.suppress(session, reason=Reason.OPTED_OUT, email=address)

    async with session_scope() as session:
        with pytest.raises(ox.OutreachError, match="asked not to be contacted"):
            await ox.deliver(session, outreach_id)


async def test_a_suppressed_contact_cannot_be_marked_sent_by_hand(suppression_db):
    """Marking a message sent by hand must not be a way around the list."""
    from careeragent.storage import session_scope

    outreach_id, address = await _seed_outreach()
    async with session_scope() as session:
        await suppression.suppress(session, reason=Reason.DO_NOT_CONTACT, email=address)

    async with session_scope() as session:
        with pytest.raises(ox.OutreachError, match="do-not-contact"):
            await ox.send(session, outreach_id)


async def test_an_unsuppressed_contact_still_goes_through(suppression_db, monkeypatch):
    """The guard must not become a wall."""
    from datetime import UTC, datetime

    from careeragent.storage import session_scope
    from careeragent.storage.models import Outreach

    outreach_id, _address = await _seed_outreach()

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


async def test_a_reply_asking_to_stop_suppresses_automatically(suppression_db):
    from careeragent.storage import session_scope
    from careeragent.storage.models import Outreach

    outreach_id, address = await _seed_outreach(status="sent")

    async with session_scope() as session:
        await ox.record_reply(
            session, outreach_id, replied=True, note="Please unsubscribe me, thanks."
        )

    async with session_scope() as session:
        assert await suppression.is_suppressed(session, email=address)
        row = await session.get(Outreach, outreach_id)
        assert row.status == "declined"


async def test_an_ordinary_reply_does_not_suppress(suppression_db):
    from careeragent.storage import session_scope
    from careeragent.storage.models import Outreach

    outreach_id, address = await _seed_outreach(status="sent")

    async with session_scope() as session:
        await ox.record_reply(
            session, outreach_id, replied=True, note="Thanks — no openings right now."
        )

    async with session_scope() as session:
        assert not await suppression.is_suppressed(session, email=address)
        row = await session.get(Outreach, outreach_id)
        assert row.status == "replied"
