"""Remembering people, and not writing to the same person twice.

The interesting work here is not the CRUD. It is that a person discovered today must be
recognised as the same person discovered last month, even though the page they came from
has been redesigned and their title now says "Senior" — and that a record whose contact
detail was better sourced last time must not be downgraded by a weaker sighting today.

Relevance is stored as the *last computed* value rather than as a property of the person,
because it is not one: the same engineering manager is a 96 for an ML candidate and a 12
for someone applying to finance. The job it was scored against is stored alongside it, so a
number on screen can always be traced to the question it answered.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..models.person import (
    ContactDetail,
    Person,
    SourceKind,
    Verification,
    classify_email,
    normalize_name,
)
from ..storage.models import PersonRecord, PersonSource
from .relevance import PersonRelevance, function_of, has_hiring_authority, seniority_of

log = get_logger("people.store")


def _detail_from(
    value: str | None, source: str | None, verification: str, confidence: float = 0.0
) -> ContactDetail | None:
    """Rebuild a contact detail from storage, or None if there is nothing stored.

    A row with a value and no source cannot happen — the writer refuses it — but this
    returns None rather than raising if one is ever found, so a corrupt row degrades to
    "no contact" instead of breaking the page that lists people.
    """
    if not value:
        return None
    try:
        return ContactDetail(
            value=value,
            kind=SourceKind.COMPANY_WEBSITE,
            source_url=source or "",
            verification=Verification(verification or "unknown"),
            confidence=confidence,
        )
    except ValueError:
        log.warning("stored_contact_without_source", value_length=len(value))
        return None


def to_person(row: PersonRecord) -> Person:
    """A stored row as the domain object the ranker understands."""
    person = Person(
        name=row.name,
        title=row.title or "",
        company_id=row.company_id,
        department=row.department or "",
        location=row.location or "",
        bio=row.bio or "",
        email=_detail_from(
            row.email, row.email_source, row.email_verification, row.email_confidence
        ),
        linkedin=_detail_from(row.linkedin_url, row.linkedin_source, row.linkedin_verification),
        sources=[{"url": s.url, "kind": s.kind} for s in row.sources],
        corroborated=row.corroborated,
        first_seen_at=row.first_seen_at,
        last_verified_at=row.last_verified_at,
    )
    return person


async def save(
    session: AsyncSession, company_id: str, company_name: str, people: list[Person]
) -> tuple[int, int]:
    """Store or update these people. Returns (added, updated).

    Matching is on the normalised name within the company, which is what the unique
    constraint enforces. That is deliberately narrower than `same_person`: the constraint
    has to be decidable by the database, and a name collision inside one company is rare
    enough to be worth the simplicity. Where a LinkedIn URL is available it is written and
    later sightings agree through it.
    """
    added = updated = 0

    for person in people:
        key = normalize_name(person.name)
        if not key:
            continue

        row = (
            await session.execute(
                select(PersonRecord).where(
                    PersonRecord.company_id == company_id,
                    PersonRecord.normalized_name == key,
                )
            )
        ).scalar_one_or_none()

        if row is None:
            row = PersonRecord(company_id=company_id, name=person.name, normalized_name=key)
            session.add(row)
            await session.flush()
            added += 1
            # A row created a moment ago has no sources by definition, and asking the
            # relationship would trigger a lazy load inside an async session — which is a
            # MissingGreenlet, not an empty list.
            known: set[str] = set()
        else:
            updated += 1
            known = {
                row_url
                for (row_url,) in (
                    await session.execute(
                        select(PersonSource.url).where(PersonSource.person_id == row.id)
                    )
                ).all()
            }

        # Fill gaps rather than overwrite: a later sighting from a thinner page must not
        # erase a title or a department an earlier, better one supplied.
        row.title = person.title or row.title
        row.department = person.department or row.department
        row.location = person.location or row.location
        row.bio = person.bio or row.bio

        row.function = function_of(row.title)
        row.seniority = seniority_of(row.title)
        row.hiring_authority = has_hiring_authority(row.title)

        _write_contact(row, person)
        row.corroborated = row.corroborated or person.corroborated
        row.last_verified_at = datetime.now(UTC)

        for source in person.sources:
            url = source.get("url") or ""
            if url and url not in known:
                session.add(PersonSource(person_id=row.id, kind=source.get("kind", ""), url=url))
                known.add(url)

    await session.flush()
    log.info("people_saved", company=company_name, added=added, updated=updated)
    return added, updated


def _write_contact(row: PersonRecord, person: Person) -> None:
    """Write contact details, keeping the better-sourced one.

    "Better" is verification first, then confidence. A verified address from a leadership
    page must survive a later unverified sighting of a different address — otherwise a
    weaker crawl silently downgrades a good record.
    """
    if person.email:
        incoming_better = (
            not row.email
            or (
                person.email.verification is Verification.VERIFIED
                and row.email_verification != Verification.VERIFIED.value
            )
            or person.email.confidence > (row.email_confidence or 0.0)
        )
        if incoming_better:
            row.email = person.email.value
            row.email_kind = classify_email(person.email.value).value
            row.email_source = person.email.source_url
            row.email_verification = person.email.verification.value
            row.email_confidence = person.email.confidence
            row.email_verified_at = datetime.now(UTC)

    if person.linkedin and (
        not row.linkedin_url
        or (
            person.linkedin.verification is Verification.VERIFIED
            and row.linkedin_verification != Verification.VERIFIED.value
        )
    ):
        row.linkedin_url = person.linkedin.value
        row.linkedin_source = person.linkedin.source_url
        row.linkedin_verification = person.linkedin.verification.value


async def record_relevance(
    session: AsyncSession,
    company_id: str,
    ranked: list[PersonRelevance],
    job_id: str = "",
    candidate_id: str = "",
) -> int:
    """Store what each person scored, for the candidate it was scored for.

    Written to two places during the transition. `person_relevance` is the owner of the
    number — keyed on (person, candidate), so two users ranking the same person no longer
    overwrite each other. The columns on `person` are updated in step as a mirror, because
    every read path in the system still uses them and switching storage and readers in one
    commit is how a migration becomes an outage.

    The job id travels with the number because relevance is not a property of a person even
    within one candidate. Without it a 96 on screen is unattributable, and a 12 for the
    same person next week looks like a contradiction rather than a different question.
    """
    from datetime import UTC, datetime

    from ..auth import tenancy
    from ..storage.models import PersonRelevance as RelevanceRow

    owner = candidate_id or tenancy.current_candidate_id() or ""

    written = 0
    for result in ranked:
        key = normalize_name(result.person.name)
        row = (
            await session.execute(
                select(PersonRecord).where(
                    PersonRecord.company_id == company_id,
                    PersonRecord.normalized_name == key,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            continue

        row.relevance = result.out_of_100
        row.relevance_confidence = result.scored.confidence
        row.recommended_channel = result.recommended_channel
        row.relevance_reasons = result.reasons[:6]
        row.scored_for_job_id = job_id or None

        if owner:
            existing = (
                await session.execute(
                    select(RelevanceRow).where(
                        RelevanceRow.person_id == row.id,
                        RelevanceRow.candidate_id == owner,
                    )
                )
            ).scalar_one_or_none()
            if existing is None:
                existing = RelevanceRow(person_id=row.id, candidate_id=owner)
                session.add(existing)
            existing.relevance = result.out_of_100
            existing.confidence = result.scored.confidence
            existing.recommended_channel = result.recommended_channel
            existing.reasons = result.reasons[:6]
            existing.scored_for_job_id = job_id or None
            existing.scored_at = datetime.now(UTC)

        written += 1

    await session.flush()
    return written


async def relevance_for(
    session: AsyncSession, person_id: str, candidate_id: str = ""
) -> dict[str, Any] | None:
    """This candidate's own score for one person, or None if they never ranked them.

    None rather than a zero. "Nobody has ranked this person for you" and "this person is
    irrelevant to you" are different facts, and the whole system is built on not confusing
    them.
    """
    from ..auth import tenancy
    from ..storage.models import PersonRelevance as RelevanceRow

    owner = candidate_id or tenancy.current_candidate_id() or ""
    if not owner:
        return None

    row = (
        await session.execute(
            select(RelevanceRow).where(
                RelevanceRow.person_id == person_id,
                RelevanceRow.candidate_id == owner,
            )
        )
    ).scalar_one_or_none()
    if row is None:
        return None
    return {
        "relevance": row.relevance,
        "confidence": round(row.confidence or 0.0, 2),
        "recommended_channel": row.recommended_channel,
        "reasons": list(row.reasons or []),
        "scored_for_job_id": row.scored_for_job_id,
        "scored_at": row.scored_at.isoformat() if row.scored_at else None,
    }


async def for_company(session: AsyncSession, company_id: str) -> list[PersonRecord]:
    rows = (
        (
            await session.execute(
                select(PersonRecord)
                .where(PersonRecord.company_id == company_id)
                .order_by(PersonRecord.relevance.desc())
            )
        )
        .scalars()
        .all()
    )
    return list(rows)


async def search(
    session: AsyncSession,
    *,
    company_id: str = "",
    function: str = "",
    min_relevance: int = 0,
    min_seniority: int | None = None,
    has_email: bool | None = None,
    has_linkedin: bool | None = None,
    hiring_authority: bool | None = None,
    contactable_only: bool = False,
    limit: int = 100,
) -> list[PersonRecord]:
    """People matching a filter, best first.

    `contactable_only` means an address we would actually send to — published and verified.
    An unverified address is shown but never counted as contactable, which is the whole
    point of keeping the two states apart.
    """
    stmt = select(PersonRecord)

    if company_id:
        stmt = stmt.where(PersonRecord.company_id == company_id)
    if function:
        stmt = stmt.where(PersonRecord.function == function)
    if min_relevance:
        stmt = stmt.where(PersonRecord.relevance >= min_relevance)
    if min_seniority is not None:
        stmt = stmt.where(PersonRecord.seniority >= min_seniority)
    if has_email is not None:
        stmt = stmt.where(
            PersonRecord.email.isnot(None) if has_email else PersonRecord.email.is_(None)
        )
    if has_linkedin is not None:
        stmt = stmt.where(
            PersonRecord.linkedin_url.isnot(None)
            if has_linkedin
            else PersonRecord.linkedin_url.is_(None)
        )
    if hiring_authority is not None:
        stmt = stmt.where(PersonRecord.hiring_authority.is_(hiring_authority))
    if contactable_only:
        stmt = stmt.where(
            PersonRecord.email.isnot(None),
            PersonRecord.email_verification == Verification.VERIFIED.value,
        )

    stmt = stmt.order_by(PersonRecord.relevance.desc(), PersonRecord.name).limit(limit)
    return list((await session.execute(stmt)).scalars().all())


def serialize(row: PersonRecord, company_name: str = "") -> dict[str, Any]:
    """A person as the dashboard shows them, provenance included.

    Every contact detail carries its source and verification beside it, because a page
    that shows an address without saying where it came from is a page that invites somebody
    to trust a guess.
    """
    return {
        "id": row.id,
        "company_id": row.company_id,
        "company": company_name,
        "name": row.name,
        "title": row.title,
        "department": row.department,
        "location": row.location,
        "function": row.function,
        "seniority": row.seniority,
        "hiring_authority": row.hiring_authority,
        "relevance": row.relevance,
        "relevance_confidence": round(row.relevance_confidence or 0.0, 2),
        "relevance_reasons": list(row.relevance_reasons or []),
        "recommended_channel": row.recommended_channel,
        "scored_for_job_id": row.scored_for_job_id,
        "email": row.email,
        "email_kind": row.email_kind,
        "email_source": row.email_source,
        "email_verification": row.email_verification,
        "email_confidence": round(row.email_confidence or 0.0, 2),
        "linkedin_url": row.linkedin_url,
        "linkedin_source": row.linkedin_source,
        "linkedin_verification": row.linkedin_verification,
        "corroborated": row.corroborated,
        # The only field that decides whether anything is sent without asking again.
        "contactable_now": bool(
            row.email and row.email_verification == Verification.VERIFIED.value
        ),
        "sources": [{"url": s.url, "kind": s.kind} for s in row.sources][:6],
        "first_seen_at": row.first_seen_at.isoformat() if row.first_seen_at else None,
        "last_verified_at": (row.last_verified_at.isoformat() if row.last_verified_at else None),
    }
