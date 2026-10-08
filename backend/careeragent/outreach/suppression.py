"""Who must not be contacted, and why.

This is the list that has to be right. Everything else in outreach degrades gracefully when
it is wrong — a badly personalised email is embarrassing, a duplicate is annoying — but
writing to somebody who asked you to stop is the one failure that cannot be walked back.
So the rules here are absolute rather than weighted, and they are checked at the transport
boundary rather than trusted to callers.

Four things put somebody on the list, and they mean different things:

    OPTED_OUT        they asked to stop. Permanent until the user personally lifts it.
    BOUNCED          the address does not work. About the address, not the person.
    DO_NOT_CONTACT   the user decided. Their call, no reason required.
    WRONG_PERSON     we had the wrong human. The record is bad, not the human.

The distinction matters when something changes. A bounced address can be superseded by a
better one; an opt-out cannot be superseded by anything. Collapsing them into a single
`suppressed` boolean would lose that, and the first time somebody found a new address for an
opted-out contact the system would happily write to them again.

**Suppression is checked last and separately.** Not folded into a relevance score, not
weighted against how good a match somebody is. A 96/100 contact who opted out is not a
96 with an asterisk; they are somebody who is not to be written to.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..storage.models import Suppression

log = get_logger("outreach.suppression")


class Reason(StrEnum):
    OPTED_OUT = "opted_out"
    BOUNCED = "bounced"
    DO_NOT_CONTACT = "do_not_contact"
    WRONG_PERSON = "wrong_person"

    @property
    def about_the_address(self) -> bool:
        """Is this about the address rather than the human being?

        A bounce says an inbox does not exist. It says nothing about whether the person
        wants to hear from us, so a better address for the same person is allowed. An
        opt-out is the opposite and travels with the person wherever they are reachable.
        """
        return self is Reason.BOUNCED

    @property
    def user_may_lift(self) -> bool:
        """Can the user remove this themselves?

        Everything except an opt-out. Lifting an opt-out is possible — people do change
        their minds and say so — but it is deliberately a separate, explicit act rather
        than the same button that clears a bounce.
        """
        return self is not Reason.OPTED_OUT

    def explain(self) -> str:
        return {
            Reason.OPTED_OUT: "they asked not to be contacted",
            Reason.BOUNCED: "mail to this address bounced",
            Reason.DO_NOT_CONTACT: "you marked them do-not-contact",
            Reason.WRONG_PERSON: "this record turned out to be the wrong person",
        }[self]


# Phrases that mean "stop". Matched against an inbound reply, and deliberately narrow: a
# false positive here silently ends a real conversation, which is worse than missing one
# and asking the user.
OPT_OUT_PHRASES = (
    "unsubscribe",
    "opt out",
    "opt-out",
    "remove me",
    "take me off",
    "stop contacting",
    "stop emailing",
    "do not contact",
    "don't contact",
    "do not email",
    "don't email",
    "no longer wish to receive",
    # "not interested in receiving" was here and is deliberately gone: it matched
    # "not interested in receiving a discount, but tell me about the role", which is
    # somebody continuing the conversation, not ending it.
)


@dataclass
class Suppressed:
    """Why somebody is not to be written to."""

    reason: Reason
    target: str
    note: str = ""
    at: datetime | None = None

    def explain(self) -> str:
        when = f" on {self.at:%d %b %Y}" if self.at else ""
        detail = f" — {self.note}" if self.note else ""
        return f"{self.reason.explain()}{when}{detail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "reason": self.reason.value,
            "target": self.target,
            "note": self.note,
            "at": self.at.isoformat() if self.at else None,
            "explanation": self.explain(),
            "user_may_lift": self.reason.user_may_lift,
        }


def _normalize(value: str) -> str:
    return (value or "").strip().lower()


def _owner_clause() -> Any:
    """Restrict to this candidate's list, plus any row that predates ownership.

    An unowned row stays visible to everybody deliberately. These are the rows that existed
    before the column did, and the two possible mistakes are not symmetric: showing one to
    an extra user costs an unsent message, hiding one from its real owner costs a message to
    somebody who asked not to receive any.
    """
    from sqlalchemy import or_

    from ..auth import tenancy

    candidate_id = tenancy.current_candidate_id()
    if not candidate_id:
        return Suppression.id.isnot(None)  # single-user: the whole table is yours
    return or_(Suppression.candidate_id == candidate_id, Suppression.candidate_id.is_(None))


async def suppress(
    session: AsyncSession,
    *,
    reason: Reason,
    email: str = "",
    person_id: str = "",
    note: str = "",
) -> Suppression:
    """Add somebody to the list. Idempotent.

    At least one of `email` or `person_id` is required, and both are stored when both are
    known — an opt-out should follow the person even if a different address for them turns
    up later, and a bounce should stick to the address even if the person record changes.
    """
    email = _normalize(email)
    if not email and not person_id:
        raise ValueError("suppression needs an email address or a person id")

    existing = await _row_for(session, email=email, person_id=person_id)
    if existing is not None:
        # Already on the list. A later reason does not overwrite an earlier one: an opt-out
        # that arrives after a bounce is the stronger fact and must not be downgraded, and
        # the reverse is not a downgrade worth making either.
        return existing

    from ..auth import tenancy

    row = Suppression(
        candidate_id=tenancy.current_candidate_id(),
        reason=reason.value,
        email=email or None,
        person_id=person_id or None,
        note=note[:500],
        suppressed_at=datetime.now(UTC),
    )
    session.add(row)
    await session.flush()
    # The address is deliberately absent from the log line.
    log.info("contact_suppressed", reason=reason.value, has_email=bool(email))
    return row


async def _row_for(
    session: AsyncSession, *, email: str = "", person_id: str = ""
) -> Suppression | None:
    """The stored row, if there is one. `find` returns the readable form; this the record."""
    from sqlalchemy import or_

    clauses = []
    if email:
        clauses.append(Suppression.email == email)
    if person_id:
        clauses.append(Suppression.person_id == person_id)
    if not clauses:
        return None
    return (
        (
            await session.execute(
                select(Suppression)
                .where(or_(*clauses), _owner_clause())
                .order_by(Suppression.suppressed_at.desc())
            )
        )
        .scalars()
        .first()
    )


async def find(session: AsyncSession, *, email: str = "", person_id: str = "") -> Suppressed | None:
    """Is this person or address suppressed?

    Checks both keys independently. A person suppressed by id is suppressed at any address;
    an address suppressed by a bounce is suppressed for whoever holds it.
    """
    email = _normalize(email)
    if not email and not person_id:
        return None

    row = await _row_for(session, email=email, person_id=person_id)
    if row is None:
        return None

    return Suppressed(
        reason=Reason(row.reason),
        target=row.email or row.person_id or "",
        note=row.note or "",
        at=row.suppressed_at,
    )


async def is_suppressed(session: AsyncSession, *, email: str = "", person_id: str = "") -> bool:
    return await find(session, email=email, person_id=person_id) is not None


async def lift(
    session: AsyncSession, *, email: str = "", person_id: str = "", force: bool = False
) -> bool:
    """Remove a suppression. Returns whether anything was removed.

    An opt-out needs `force`, because clearing one should never be the same gesture as
    clearing a bounce. The user can do it — people do change their minds and say so — but
    they have to mean it.
    """
    found = await find(session, email=email, person_id=person_id)
    if found is None:
        return False
    if not found.reason.user_may_lift and not force:
        raise ValueError(
            "this contact opted out. Lifting that is possible but is a deliberate act — "
            "pass force=true, and only if they have told you they changed their mind."
        )

    from sqlalchemy import delete, or_

    clauses = []
    if _normalize(email):
        clauses.append(Suppression.email == _normalize(email))
    if person_id:
        clauses.append(Suppression.person_id == person_id)
    await session.execute(delete(Suppression).where(or_(*clauses)))
    await session.flush()
    log.warning("suppression_lifted", reason=found.reason.value, forced=force)
    return True


def reads_as_opt_out(text: str) -> bool:
    """Does this reply ask us to stop?

    Narrow on purpose. A false positive here silently ends a real conversation — somebody
    writing "I'm not interested in receiving a discount, but tell me about the role" must
    not be suppressed — so this matches explicit requests only, and anything ambiguous is
    left for the user to read.
    """
    lowered = f" {' '.join((text or '').lower().split())} "
    return any(phrase in lowered for phrase in OPT_OUT_PHRASES)


async def listing(session: AsyncSession, limit: int = 200) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(Suppression)
                .where(_owner_clause())
                .order_by(Suppression.suppressed_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [
        Suppressed(
            reason=Reason(r.reason),
            target=r.email or r.person_id or "",
            note=r.note or "",
            at=r.suppressed_at,
        ).to_dict()
        for r in rows
    ]
