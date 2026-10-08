"""Outreach endpoints.

Three separate calls to get a message out — draft, approve, send — and each is a
distinct request. There is no endpoint that drafts and sends, and there is no batch
send. That is the brief's §24 constraint expressed as an API surface rather than as a
warning in a docstring.

`/send` records an approved message as sent; it does not itself deliver mail. See the
module docstring in `applications/outreach.py` for why that is the default.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import select

from ..applications import outreach as ox
from ..applications.outreach import OutreachError
from ..logging import get_logger
from ..storage import repository as repo
from ..storage import session_scope
from ..storage.models import Company, Outreach

log = get_logger("routes.outreach")

router = APIRouter(prefix="/api/v1/outreach", tags=["outreach"])


class DraftRequest(BaseModel):
    company_id: str
    contact_id: str | None = None
    channel: str = "email"
    target_roles: list[str] = Field(default_factory=list)
    queue: bool = Field(
        default=True, description="Queue it for approval. False returns the draft only."
    )


class ContactRequest(BaseModel):
    company_id: str
    channel: str = "email"
    address: str
    source: str = Field(
        description=(
            "Where this came from: careers_page | job_posting | company_contact_page "
            "| press_page | user_entered. Required, and checked — a contact with no "
            "recorded provenance is refused."
        )
    )
    name: str = ""
    role: str = ""


class ReplyRequest(BaseModel):
    replied: bool = True
    note: str = ""


@router.get("")
async def board() -> dict[str, Any]:
    """Drafted, waiting, sent, replied — plus the current rate-limit headroom."""
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        return await ox.board(session, row.id)


@router.get("/opportunities")
async def opportunities(
    min_fit: float = Query(0.7, ge=0.0, le=1.0), limit: int = Query(20, ge=1, le=50)
) -> dict[str, Any]:
    """Companies worth contacting: good fit, researched, no live application.

    A company with an application already in its ATS is excluded — contacting them
    separately while that sits there is noise, not initiative.
    """
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        companies = await ox.candidates_for_outreach(
            session, row.id, min_company_fit=min_fit, limit=limit
        )
        contactable = sum(1 for c in companies if c["contactable"])
        return {
            "companies": companies,
            "total": len(companies),
            "contactable": contactable,
            "note": (
                f"{len(companies) - contactable} of these have no recorded contact channel. "
                "Add one from a page the company publishes, or from your own knowledge — "
                "this agent does not guess email addresses."
            )
            if contactable < len(companies)
            else "",
        }


@router.post("/contacts", status_code=201)
async def add_contact(body: ContactRequest) -> dict[str, Any]:
    """Record a contact channel. Refuses anything without a permitted source."""
    async with session_scope() as session:
        try:
            contact = await ox.add_contact(
                session,
                body.company_id,
                channel=body.channel,
                address=body.address,
                source=body.source,
                name=body.name,
                role=body.role,
                confidence=0.9 if body.source.startswith("user_entered") else 0.6,
            )
        except OutreachError as exc:
            raise HTTPException(422, str(exc)) from exc
        return {
            "id": contact.id,
            "company_id": contact.company_id,
            "channel": contact.channel,
            "address": contact.address,
            "source": contact.source,
        }


@router.get("/contacts/{company_id}")
async def contacts(company_id: str) -> list[dict[str, Any]]:
    async with session_scope() as session:
        return await ox.list_contacts(session, company_id)


@router.post("/draft")
async def draft(body: DraftRequest) -> dict[str, Any]:
    """Draft a message. Grounded in the Fact Bank, or returned as not usable."""
    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            raise HTTPException(409, "Upload your resume first")
        row = await repo.get_or_create_candidate(session)

        company = await repo.get_company(session, body.company_id)
        if company is None:
            raise HTTPException(404, "company not found")

        result = ox.draft(candidate, company, target_roles=body.target_roles)

        from ..llm.router import get_router

        await repo.record_llm_calls(session, get_router().drain_calls())
        payload: dict[str, Any] = {"draft": result.to_dict(), "queued": False}

        if not result.ok:
            payload["note"] = (
                "Not queued. A draft that cannot be grounded in your facts is not one "
                f"this agent will send: {result.reason}"
            )
            return payload

        if body.queue:
            try:
                queued = await ox.create(
                    session,
                    row.id,
                    body.company_id,
                    result,
                    contact_id=body.contact_id,
                    channel=body.channel,
                )
            except OutreachError as exc:
                raise HTTPException(409, str(exc)) from exc
            payload["queued"] = True
            payload["outreach_id"] = queued.id
            payload["status"] = queued.status
        return payload


def _deliverable(contact: Any, blocked: Any) -> dict[str, Any]:
    """Can this actually be sent, and if not, what is missing?

    Three distinct answers, because they need three different actions from the reviewer.
    A draft with no recipient needs a contact found; a suppressed one needs cancelling;
    a ready one needs reading.
    """
    if blocked is not None:
        return {
            "ready": False,
            "why": f"this person is on your do-not-contact list ({blocked.reason.value})",
            "fix": "cancel this draft",
        }
    if contact is None or not (contact.address or "").strip():
        return {
            "ready": False,
            "why": "no recipient — this was drafted for the company, not for a person",
            "fix": "find somebody to send it to, or cancel",
        }
    return {"ready": True, "why": "", "fix": ""}


@router.get("/review")
async def review_queue(limit: int = Query(20, ge=1, le=100)) -> dict[str, Any]:
    """Everything awaiting a decision, with what is needed to make one.

    Assembled server-side rather than left to the client to join. A screen that fetches
    six endpoints and stitches them together is a screen where one failed request quietly
    removes the prior-contact warning, and the reviewer never learns it was missing.

    A suppressed recipient is returned **with the reason**, not filtered out. Filtering
    would leave a draft sitting in a queue nobody can see, forever; showing it with
    `blocked` set is what lets somebody cancel it.
    """
    from ..outreach import suppression
    from ..storage.models import OutreachContact, PersonRecord

    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)

        drafts = (
            (
                await session.execute(
                    select(Outreach)
                    .where(
                        Outreach.candidate_id == row.id,
                        Outreach.status.in_(["drafted", "awaiting_approval", "approved"]),
                    )
                    .order_by(Outreach.created_at.desc())
                    .limit(limit)
                )
            )
            .scalars()
            .all()
        )

        companies = {
            str(cid): name
            for cid, name in (
                await session.execute(
                    select(Company.id, Company.name).where(
                        Company.id.in_([d.company_id for d in drafts] or [""])
                    )
                )
            ).all()
        }

        out: list[dict[str, Any]] = []
        for draft in drafts:
            contact = (
                await session.get(OutreachContact, draft.contact_id) if draft.contact_id else None
            )

            person = None
            if contact is not None and contact.name:
                person = (
                    (
                        await session.execute(
                            select(PersonRecord).where(
                                PersonRecord.company_id == draft.company_id,
                                PersonRecord.name == contact.name,
                            )
                        )
                    )
                    .scalars()
                    .first()
                )

            history = await ox.history_for(
                session,
                email=contact.address if contact else "",
                person_id=person.id if person else "",
                person_name=contact.name if contact else "",
                exclude_id=draft.id,
            )
            blocked = await suppression.find(
                session,
                email=contact.address if contact else "",
                person_id=person.id if person else "",
            )

            words = len((draft.body or "").split())
            out.append(
                {
                    "id": draft.id,
                    "status": draft.status,
                    "channel": draft.channel,
                    "company": companies.get(str(draft.company_id), ""),
                    "created_at": draft.created_at.isoformat() if draft.created_at else None,
                    "recipient": {
                        "name": contact.name if contact else "",
                        "role": contact.role if contact else "",
                        "address": contact.address if contact else "",
                        # The URL it was found at. A contact detail in this system cannot
                        # exist without one, and showing it is what lets a reviewer check
                        # rather than trust.
                        "source": contact.source if contact else "",
                        "relevance": person.relevance if person else None,
                        "relevance_reasons": list(person.relevance_reasons or []) if person else [],
                    },
                    "message": {
                        "subject": draft.subject,
                        "body": draft.body,
                        "words": words,
                        "fact_ids": list(draft.fact_ids or []),
                        "resume_render_id": draft.resume_render_id,
                    },
                    "prior_contact": {
                        "count": len(history.events),
                        "replied": history.replied,
                        "last_at": history.last_at.isoformat() if history.last_at else None,
                        "channels": history.channels_used,
                    },
                    # Not a warning. Approval must be impossible here, and the transport
                    # refuses independently — this is so the reviewer knows why.
                    "blocked": (
                        {"reason": blocked.reason.value, "note": blocked.note} if blocked else None
                    ),
                    # Whether this could go at all, and if not, what is missing. A
                    # company-level draft written before anybody was found has no
                    # recipient — which is a legitimate state, and one that renders as a
                    # row of empty fields unless it is said out loud.
                    "deliverable": _deliverable(contact, blocked),
                }
            )

    return {
        "queue": out,
        "count": len(out),
        "blocked": sum(1 for item in out if item["blocked"]),
        "note": (
            "Nothing is sent by approving. Approval marks a draft ready; sending is a "
            "separate, explicit call."
        ),
    }


@router.post("/{outreach_id}/approve")
async def approve(outreach_id: str) -> dict[str, Any]:
    """Approve a draft. Still does not send it — that is a separate call."""
    async with session_scope() as session:
        try:
            row = await ox.approve(session, outreach_id)
        except OutreachError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "id": row.id,
            "status": row.status,
            "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        }


@router.post("/{outreach_id}/send")
async def send(outreach_id: str, confirm: bool = Query(False)) -> dict[str, Any]:
    """Record an approved message as sent, within the rate limits."""
    if not confirm:
        raise HTTPException(400, "pass confirm=true")
    async with session_scope() as session:
        try:
            row = await ox.send(session, outreach_id)
        except OutreachError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "id": row.id,
            "status": row.status,
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
            "follow_up_date": row.follow_up_date.isoformat() if row.follow_up_date else None,
            "subject": row.subject,
            "body": row.body,
            "note": (
                "Marked as sent and tracked. This endpoint does not deliver mail — send "
                "the text above from your own mailbox. Wiring a transport is your call, "
                "not a default."
            ),
        }


@router.post("/{outreach_id}/reply")
async def reply(outreach_id: str, body: ReplyRequest) -> dict[str, Any]:
    async with session_scope() as session:
        try:
            row = await ox.record_reply(session, outreach_id, replied=body.replied, note=body.note)
        except OutreachError as exc:
            raise HTTPException(404, str(exc)) from exc
        return {"id": row.id, "status": row.status}


@router.post("/{outreach_id}/cancel")
async def cancel(outreach_id: str, reason: str = Query("")) -> dict[str, Any]:
    async with session_scope() as session:
        try:
            row = await ox.cancel(session, outreach_id, reason)
        except OutreachError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {"id": row.id, "status": row.status}


# ─── Gmail ──────────────────────────────────────────────────────────────────
#
# Consent is the user's and is interactive: there is no way to authorise this without a
# person visiting Google's consent screen. The scope requested is `gmail.send` alone — it
# cannot read the mailbox — and no endpoint here sends anything without an explicit
# confirm on an already-approved message.


@router.get("/gmail/status")
async def gmail_status() -> dict[str, Any]:
    """Whether Gmail is configured, authorised, and answering."""
    from ..outreach import gmail

    return await gmail.status()


@router.get("/gmail/authorize")
async def gmail_authorize() -> dict[str, Any]:
    """The consent link to open. Returns it rather than redirecting, so the user sees
    where they are being sent before they go."""
    from ..outreach import gmail

    try:
        return {
            "url": gmail.authorization_url(),
            "scopes": list(gmail.SCOPES),
            "note": (
                "This asks only for permission to send mail as you. It cannot read, "
                "search or delete anything in your mailbox."
            ),
        }
    except gmail.GmailError as exc:
        raise HTTPException(422, str(exc)) from exc


@router.get("/gmail/callback")
async def gmail_callback(code: str = Query(""), error: str = Query("")) -> dict[str, Any]:
    """Where Google sends the user back after consent."""
    from ..outreach import gmail

    if error:
        return {"authorised": False, "note": f"Google reported: {error}"}
    try:
        result = await gmail.exchange_code(code)
    except gmail.GmailError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {**result, "note": "Gmail is connected. You can now send approved messages."}


@router.post("/gmail/disconnect")
async def gmail_disconnect() -> dict[str, Any]:
    """Delete the stored token. Revoking at Google's end is the user's to do."""
    from ..outreach import gmail

    removed = gmail.forget()
    return {
        "disconnected": removed,
        "note": (
            "Token deleted. To revoke access entirely, remove this app at "
            "https://myaccount.google.com/permissions."
        ),
    }


@router.post("/{outreach_id}/deliver")
async def deliver(outreach_id: str, confirm: bool = Query(False)) -> dict[str, Any]:
    """Actually send an approved message through the user's Gmail.

    Separate from `/send`, which only marks a record as sent by hand. This one delivers,
    so it needs the same explicit confirm and refuses anything that is not approved,
    within the rate limits, and addressed to a contact with a permitted source.
    """
    if not confirm:
        raise HTTPException(400, "pass confirm=true — this sends a real email from your account")
    async with session_scope() as session:
        try:
            row = await ox.deliver(session, outreach_id)
        except OutreachError as exc:
            raise HTTPException(409, str(exc)) from exc
        return {
            "id": row.id,
            "status": row.status,
            "sent_at": row.sent_at.isoformat() if row.sent_at else None,
            "transport": row.transport,
            "message_id": row.transport_message_id,
            "thread_id": row.transport_thread_id,
            "follow_up_date": row.follow_up_date.isoformat() if row.follow_up_date else None,
            "note": "Sent, and Gmail returned a message id — that id is the evidence.",
        }


# ─── Writing to one person ──────────────────────────────────────────────────


class ComposeRequest(BaseModel):
    person_id: str
    channel: str = "email"
    job_id: str = ""


@router.post("/compose")
async def compose_message(body: ComposeRequest) -> dict[str, Any]:
    """Draft a message to one person, for one opening. Sends nothing.

    The draft changes with who is reading — a recruiter is matching a requisition, an
    engineering manager is imagining somebody on their team — and with the channel, because
    an email carrying an attachment and a LinkedIn message read on a phone are not the same
    medium. Sending the same text down both marks the sender as automated.
    """
    from ..outreach import compose, suppression
    from ..people import store as people_store
    from ..storage.models import Job, PersonRecord

    try:
        channel = compose.Channel(body.channel)
    except ValueError as exc:
        allowed = ", ".join(c.value for c in compose.Channel)
        raise HTTPException(422, f"unknown channel; use one of {allowed}") from exc

    async with session_scope() as session:
        row = await session.get(PersonRecord, body.person_id)
        if row is None:
            raise HTTPException(404, "person not found")

        candidate = await repo.load_candidate(session)
        if candidate is None:
            raise HTTPException(422, "no candidate profile loaded — import a resume first")

        company = await repo.get_company(session, row.company_id)
        if company is None:
            raise HTTPException(404, "the company for this person is missing")

        # Said before anything is written, not after. Drafting a message to somebody who
        # asked not to be contacted wastes the user's attention on a decision already made.
        if blocked := await suppression.find(session, email=row.email or "", person_id=row.id):
            raise HTTPException(409, f"not writing to them: {blocked.explain()}")

        job = await session.get(Job, body.job_id) if body.job_id else None
        person = people_store.to_person(row)
        person.company = company.name
        employees = (company.signals or {}).get("apollo", {}).get("employees")
        company_name = company.name

    message = compose.draft_to_person(
        candidate,
        person,
        company,
        channel=channel,
        job_title=job.title if job else "",
        job_note=f"{job.location}" if job and job.location else "",
        company_size=employees if isinstance(employees, int) else None,
    )

    return {
        **message.to_dict(),
        "to": {
            "name": row.name,
            "title": row.title,
            "company": company_name,
            "email": row.email,
            "email_verification": row.email_verification,
            "linkedin_url": row.linkedin_url,
            "relevance": row.relevance,
        },
        "note": (
            "Nothing has been sent. Review it, edit it, then approve — and note that an "
            "edited message is checked by exactly the same rules as a generated one."
        ),
    }


@router.post("/compose/check")
async def check_message(
    channel: str = Query("email"), body: str = Query(..., min_length=1)
) -> dict[str, Any]:
    """Check a message the user wrote or edited, by the same rules a draft is checked by."""
    from ..outreach import compose

    try:
        selected = compose.Channel(channel)
    except ValueError as exc:
        raise HTTPException(422, "unknown channel") from exc

    warnings = compose.check(body, selected)
    return {
        "channel": selected.value,
        "word_count": len(body.split()),
        "word_limit": selected.word_limit,
        "warnings": warnings,
        "ok": not warnings,
    }


# ─── Who must not be contacted ──────────────────────────────────────────────
#
# The list that has to be right. Every other outreach failure can be walked back; writing
# to somebody who asked you to stop cannot. It is checked at the transport boundary rather
# than trusted to callers, so these endpoints manage it rather than enforce it.


class SuppressRequest(BaseModel):
    email: str = ""
    person_id: str = ""
    reason: str = "do_not_contact"
    note: str = ""


@router.get("/suppressed")
async def suppressed(limit: int = Query(200, ge=1, le=1000)) -> dict[str, Any]:
    """Everyone on the list, newest first, with why and whether you can lift it."""
    from ..outreach import suppression

    async with session_scope() as session:
        rows = await suppression.listing(session, limit=limit)
    return {
        "suppressed": rows,
        "count": len(rows),
        "note": (
            "Checked before anything is sent, and before a message can even be marked "
            "sent by hand. An opt-out needs a deliberate act to lift; the rest do not."
        ),
    }


@router.post("/suppressed", status_code=201)
async def add_suppression(body: SuppressRequest) -> dict[str, Any]:
    from ..outreach import suppression

    try:
        reason = suppression.Reason(body.reason)
    except ValueError as exc:
        allowed = ", ".join(r.value for r in suppression.Reason)
        raise HTTPException(422, f"unknown reason {body.reason!r}; use one of {allowed}") from exc

    async with session_scope() as session:
        try:
            await suppression.suppress(
                session,
                reason=reason,
                email=body.email,
                person_id=body.person_id,
                note=body.note,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    return {"suppressed": True, "reason": reason.value, "note": reason.explain()}


@router.delete("/suppressed")
async def lift_suppression(
    email: str = Query(""),
    person_id: str = Query(""),
    force: bool = Query(False, description="Required to lift an opt-out."),
) -> dict[str, Any]:
    """Take somebody off the list.

    An opt-out needs `force`, because clearing one should never be the same gesture as
    clearing a bounce. It is possible — people do change their minds and say so — but it
    has to be meant.
    """
    from ..outreach import suppression

    async with session_scope() as session:
        try:
            removed = await suppression.lift(session, email=email, person_id=person_id, force=force)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    return {
        "lifted": removed,
        "note": "Removed." if removed else "They were not on the list.",
    }


@router.get("/limits")
async def limits() -> dict[str, Any]:
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        return await ox.rate_limit_status(session, row.id)
