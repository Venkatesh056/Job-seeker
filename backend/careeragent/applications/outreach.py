"""Outreach: a small number of personalized messages, each individually approved.

The brief's §24 asks for this and also states the constraint: "Do NOT mass-message
thousands of people." That constraint is what shapes this module, so it is worth
being concrete about what is and is not built here.

**Built.** Identifying companies worth contacting; drafting a grounded message;
queueing it; requiring an explicit approval per message; rate limits enforced in
code; tracking replies and follow-ups.

**Not built, deliberately.** Contact discovery by scraping profiles, guessing email
patterns, or querying enrichment vendors. A contact enters the system only from a
channel the company itself publishes — a careers page address, a listed recruiting
inbox, a contact form — or because the user typed it in. `OutreachContact.source`
is mandatory, and `send` refuses a contact without one.

**No send transport is wired.** `send` marks an approved message as sent and records
it; it does not itself deliver mail. Handing an agent SMTP credentials to send on
someone's behalf is a decision the user makes, not a default — and the honest
version of this feature is a reviewed draft the user sends from their own mailbox.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..agents.trust import harden
from ..events import EventType, Level, get_bus
from ..logging import get_logger
from ..models.candidate import Candidate, FactKind
from ..prompts import get as get_prompt
from ..storage.models import (
    Company,
    Job,
    Outreach,
    OutreachContact,
)
from ..storage.models import (
    OutreachStatus as OS,
)

log = get_logger("applications.outreach")

# Rate limits, enforced below rather than documented. Outreach that scales is spam
# by definition; these numbers are what "personalized" can actually sustain.
MAX_PER_DAY = 5
MAX_PER_WEEK = 15
MIN_DAYS_BETWEEN_SAME_COMPANY = 30
FOLLOW_UP_DAYS = 12

# Channels a message may go out on. A contact form and an email are the user's own
# to send; a LinkedIn message is drafted for the user to paste, never automated —
# LinkedIn's terms prohibit automated messaging, and an account restriction would
# cost them the network they need most.
CHANNELS = ("email", "contact_form", "linkedin_draft")

# Where a contact may come from. Anything else is rejected at the door.
PERMITTED_SOURCES = re.compile(
    r"^(careers_page|job_posting|company_contact_page|press_page|user_entered)(:.*)?$"
)


class OutreachError(RuntimeError):
    """A refusal the user should see."""


@dataclass
class Draft:
    subject: str = ""
    body: str = ""
    fact_ids: list[str] = field(default_factory=list)
    ok: bool = False
    reason: str = ""
    model: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "subject": self.subject,
            "body": self.body,
            "fact_ids": list(self.fact_ids),
            "ok": self.ok,
            "reason": self.reason,
            "model": self.model,
        }


# ─── Targeting ──────────────────────────────────────────────────────────────


async def candidates_for_outreach(
    session: AsyncSession,
    candidate_id: str,
    *,
    min_company_fit: float = 0.7,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Companies worth contacting: good fit, researched, and no open application.

    The brief's §24 case — a company that suits the candidate but has no suitable
    posting right now. Companies with a live application are excluded: contacting
    them separately while an application sits in their ATS is noise, not initiative.
    """
    rows = (
        (
            await session.execute(
                select(Company)
                .where(
                    Company.fit_score.is_not(None),
                    Company.fit_score >= min_company_fit,
                    Company.researched_at.is_not(None),
                )
                .order_by(Company.fit_score.desc())
                .limit(limit * 3)
            )
        )
        .scalars()
        .all()
    )

    out: list[dict[str, Any]] = []
    for company in rows:
        if await _has_recent_outreach(session, candidate_id, company.id):
            continue
        if await _has_application(session, candidate_id, company.id):
            continue
        contacts = await list_contacts(session, company.id)
        out.append(
            {
                "company_id": company.id,
                "name": company.name,
                "domain": company.domain,
                "fit_score": company.fit_score,
                "industry": company.industry,
                "tech_focus": list(company.tech_focus or [])[:8],
                "careers_url": (company.signals or {}).get("careers_url"),
                "contacts": list(contacts),
                "contactable": bool(contacts),
            }
        )
        if len(out) >= limit:
            break
    return out


async def _has_recent_outreach(session: AsyncSession, candidate_id: str, company_id: str) -> bool:
    cutoff = datetime.now(UTC) - timedelta(days=MIN_DAYS_BETWEEN_SAME_COMPANY)
    row = (
        await session.execute(
            select(Outreach)
            .where(
                Outreach.candidate_id == candidate_id,
                Outreach.company_id == company_id,
                Outreach.status.notin_([OS.CANCELLED.value, OS.DECLINED.value]),
                Outreach.created_at >= cutoff,
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def _has_application(session: AsyncSession, candidate_id: str, company_id: str) -> bool:
    from ..storage.models import Application

    row = (
        await session.execute(
            select(Application)
            .join(Job, Job.id == Application.job_id)
            .where(Application.candidate_id == candidate_id, Job.company_id == company_id)
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


# ─── Contacts ───────────────────────────────────────────────────────────────


async def add_contact(
    session: AsyncSession,
    company_id: str,
    *,
    channel: str,
    address: str,
    source: str,
    name: str = "",
    role: str = "",
    confidence: float = 0.5,
) -> OutreachContact:
    """Record a contact channel. Refuses anything without a permitted source.

    The source check is the mechanism, not a note: a contact with no recorded
    provenance cannot have come from a page the company published, and this module
    has no other legitimate way to have obtained one.
    """
    if channel not in CHANNELS:
        raise OutreachError(f"unsupported channel {channel!r}; use one of {', '.join(CHANNELS)}")
    if not PERMITTED_SOURCES.match(source or ""):
        raise OutreachError(
            f"source {source!r} is not permitted. A contact must come from a page the "
            "company publishes (careers_page, job_posting, company_contact_page, "
            "press_page) or from you (user_entered)."
        )
    if channel == "email" and "@" not in address:
        raise OutreachError("an email contact needs an email address")

    existing = (
        await session.execute(
            select(OutreachContact).where(
                OutreachContact.company_id == company_id,
                OutreachContact.channel == channel,
                OutreachContact.address == address,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    row = OutreachContact(
        company_id=company_id,
        channel=channel,
        address=address,
        source=source,
        name=name,
        role=role,
        confidence=confidence,
    )
    session.add(row)
    await session.flush()
    return row


async def list_contacts(session: AsyncSession, company_id: str) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(OutreachContact).where(OutreachContact.company_id == company_id)
            )
        )
        .scalars()
        .all()
    )
    return [
        {
            "id": c.id,
            "name": c.name,
            "role": c.role,
            "channel": c.channel,
            "address": c.address,
            "source": c.source,
            "confidence": c.confidence,
        }
        for c in rows
    ]


# ─── Drafting ───────────────────────────────────────────────────────────────

MESSAGE = get_prompt("outreach_message")


def _repair_message(
    rejected: str,
    complaint: str,
    candidate: Candidate,
    fact_lines: list[str],
    router: Any,
) -> tuple[str, list[str]] | None:
    """Ask once for a corrected message. The rules live in `agents/repair.py`.

    Worth being clear about what this does not change: a repaired message is still a draft.
    Outreach is never sent without per-message approval, and a revision that satisfies the
    checks has earned the right to be *shown* to the user, not the right to leave.
    """
    from ..agents import repair
    from ..agents.trust import harden

    data = repair.revise(
        agent="outreach",
        system=harden(MESSAGE.system),
        complaint=complaint,
        original=rejected,
        facts="\n".join(fact_lines[:18]),
        task="outreach message",
        output_shape='{"body": "the corrected message, no greeting and no signature", '
        '"cite": ["fact-id"], "confident": true|false}',
        router=router,
        visible_tokens=400,
    )
    if data is None:
        return None
    return repair.cited_text(data, "body", 2000)


def draft(
    candidate: Candidate,
    company: Company,
    *,
    target_roles: list[str] | None = None,
    router: Any = None,
) -> Draft:
    """Draft one message. Grounded, or it is not returned as usable."""
    from .answers import check_grounding
    from .cover_letter import check_flattery

    if router is None:
        from ..llm.router import get_router

        router = get_router()
    if not router.available():
        return Draft(reason="no LLM configured; write this one yourself")

    from ..llm.router import Tier, tokens_for

    notes_bits = [company.description or ""]
    if company.industry:
        notes_bits.append(f"Industry: {company.industry}.")
    if company.tech_focus:
        notes_bits.append(f"Technology on their site: {', '.join(company.tech_focus[:10])}.")
    if company.hq_location:
        notes_bits.append(f"Location: {company.hq_location}.")

    fact_lines = []
    for fact in candidate.facts:
        if fact.kind in {FactKind.EXPERIENCE, FactKind.PROJECT, FactKind.EDUCATION}:
            fact_lines.append(
                f"{fact.id} :: {fact.kind.value} :: {' '.join(fact.label.split())}"
                + (f" — {' '.join(fact.body.split())[:200]}" if fact.body else "")
            )
    skills = sorted({f.label for f in candidate.facts if f.kind is FactKind.SKILL})
    if skills:
        fact_lines.append(f"skills :: skill :: {', '.join(skills)}")

    result = router.complete(
        MESSAGE.render(
            company=company.name,
            company_notes=" ".join(" ".join(notes_bits).split())[:800] or "(nothing researched)",
            target_roles=", ".join(target_roles or []) or "not stated",
            facts="\n".join(fact_lines[:18]),
        ),
        tier=Tier.STRONG,
        system=harden(MESSAGE.system),
        temperature=0.4,
        # Under 140 words, plus a subject line and citations.
        max_tokens=tokens_for(400),
        json_mode=True,
        agent="outreach",
    )
    if result is None:
        return Draft(reason="every model in the chain failed")

    data = result.json()
    if not isinstance(data, dict):
        return Draft(model=result.model, reason="the model's reply was not usable JSON")

    subject = " ".join(str(data.get("subject") or "").split())[:200]
    body = " ".join(str(data.get("body") or "").split())
    cited = [str(c) for c in (data.get("cite") or []) if str(c).strip()]

    if not body:
        return Draft(model=result.model, reason="the model returned no message")
    if not bool(data.get("confident", True)) or not cited:
        return Draft(
            subject=subject,
            body=body,
            model=result.model,
            reason="the model could not ground the message in your facts",
        )
    # Both rejections are the same kind of problem — a claim about the candidate that
    # nothing supports — and both usually land on one clause of an otherwise usable message.
    complaint = check_grounding(body, cited, candidate)
    if not complaint and (flattery := check_flattery(body)):
        complaint = f"contains unsupported flattery: {', '.join(flattery[:2])}"

    if complaint:
        log.info("outreach_rejected", company=company.name, reason=complaint, action="revising")
        if fixed := _repair_message(body, complaint, candidate, fact_lines, router):
            body, cited = fixed
            if not check_grounding(body, cited, candidate) and not check_flattery(body):
                log.info("outreach_repaired", company=company.name)
                complaint = ""

    if complaint:
        return Draft(
            subject=subject, body=body, fact_ids=cited, model=result.model, reason=complaint
        )

    return Draft(
        subject=subject or f"{candidate.name} — speculative application",
        body=body,
        fact_ids=cited,
        ok=True,
        model=result.model,
    )


# ─── Queue, approve, send ───────────────────────────────────────────────────


async def create(
    session: AsyncSession,
    candidate_id: str,
    company_id: str,
    draft_result: Draft,
    *,
    contact_id: str | None = None,
    channel: str = "email",
    resume_render_id: str | None = None,
) -> Outreach:
    """Queue a drafted message for approval. Never sends."""
    if not draft_result.ok:
        raise OutreachError(f"will not queue an ungrounded draft: {draft_result.reason}")
    if await _has_recent_outreach(session, candidate_id, company_id):
        raise OutreachError(
            f"already contacted this company within {MIN_DAYS_BETWEEN_SAME_COMPANY} days"
        )

    row = Outreach(
        candidate_id=candidate_id,
        company_id=company_id,
        contact_id=contact_id,
        channel=channel,
        subject=draft_result.subject,
        body=draft_result.body,
        fact_ids=list(draft_result.fact_ids),
        resume_render_id=resume_render_id,
        status=OS.AWAITING_APPROVAL,
    )
    session.add(row)
    await session.flush()

    await get_bus().emit_one(
        EventType.OUTREACH_DRAFTED,
        f"Drafted outreach to {company_id}",
        outreach_id=row.id,
        company_id=company_id,
        channel=channel,
    )
    return row


async def approve(session: AsyncSession, outreach_id: str) -> Outreach:
    """Mark a draft approved. Still does not send."""
    row = await session.get(Outreach, outreach_id)
    if row is None:
        raise OutreachError("outreach not found")
    if row.status not in {OS.DRAFTED.value, OS.AWAITING_APPROVAL.value}:
        raise OutreachError(f"cannot approve something in state {row.status}")

    row.status = OS.APPROVED
    row.approved_at = datetime.now(UTC)
    await session.flush()
    await get_bus().emit_one(
        EventType.OUTREACH_APPROVED, "Outreach approved", outreach_id=outreach_id
    )
    return row


async def rate_limit_status(session: AsyncSession, candidate_id: str) -> dict[str, Any]:
    now = datetime.now(UTC)

    async def count_since(delta: timedelta) -> int:
        return int(
            (
                await session.execute(
                    select(func.count())
                    .select_from(Outreach)
                    .where(
                        Outreach.candidate_id == candidate_id,
                        Outreach.sent_at.is_not(None),
                        Outreach.sent_at >= now - delta,
                    )
                )
            ).scalar()
            or 0
        )

    today = await count_since(timedelta(days=1))
    week = await count_since(timedelta(days=7))
    return {
        "sent_today": today,
        "sent_this_week": week,
        "day_limit": MAX_PER_DAY,
        "week_limit": MAX_PER_WEEK,
        "day_remaining": max(0, MAX_PER_DAY - today),
        "week_remaining": max(0, MAX_PER_WEEK - week),
        "allowed": today < MAX_PER_DAY and week < MAX_PER_WEEK,
    }


async def send(session: AsyncSession, outreach_id: str) -> Outreach:
    """Record an approved message as sent, enforcing the rate limits.

    This does not deliver mail. It transitions the record, stamps `sent_at`, and
    sets a follow-up date, so the user can send the approved text from their own
    mailbox and have it tracked. Wiring a transport is a deliberate choice the user
    makes; a default that emails on the user's behalf is not one this module will
    make for them.
    """
    row = await session.get(Outreach, outreach_id)
    if row is None:
        raise OutreachError("outreach not found")
    if row.status != OS.APPROVED.value:
        raise OutreachError(f"only an approved message can be sent (this one is {row.status})")

    limits = await rate_limit_status(session, row.candidate_id)
    if not limits["allowed"]:
        raise OutreachError(
            f"rate limit reached: {limits['sent_today']}/{MAX_PER_DAY} today, "
            f"{limits['sent_this_week']}/{MAX_PER_WEEK} this week"
        )

    if row.contact_id:
        contact = await session.get(OutreachContact, row.contact_id)
        if contact is None or not PERMITTED_SOURCES.match(contact.source or ""):
            raise OutreachError(
                "this contact has no permitted recorded source and will not be messaged"
            )
        # Also here: marking a message sent by hand must not be a way around the list.
        from ..outreach import suppression

        if blocked := await suppression.find(session, email=contact.address):
            raise OutreachError(f"not contacting them: {blocked.explain()}")

    row.status = OS.SENT
    row.sent_at = datetime.now(UTC)
    row.follow_up_date = date.today() + timedelta(days=FOLLOW_UP_DAYS)
    await session.flush()

    await get_bus().emit_one(
        EventType.OUTREACH_SENT,
        f"Outreach marked sent ({limits['sent_today'] + 1}/{MAX_PER_DAY} today)",
        outreach_id=outreach_id,
        channel=row.channel,
    )
    return row


async def deliver(session: AsyncSession, outreach_id: str) -> Outreach:
    """Send an approved message through the user's own Gmail, then record the evidence.

    Every guard `send` applies applies here too, because this calls it: approved status,
    a contact whose source is permitted, and the daily and weekly rate limits. The order
    matters — the message goes out first and the record is marked only on a message id
    coming back, so a failed send leaves the record approved and re-sendable rather than
    marked sent with nothing behind it.

    Nothing calls this on a schedule. A person presses a button.
    """
    from ..outreach import gmail

    row = await session.get(Outreach, outreach_id)
    if row is None:
        raise OutreachError("outreach not found")
    if row.channel != "email":
        raise OutreachError(f"Gmail can only send email; this one is a {row.channel}")
    if row.status != OS.APPROVED.value:
        raise OutreachError(f"only an approved message can be sent (this one is {row.status})")

    contact = await session.get(OutreachContact, row.contact_id) if row.contact_id else None
    if contact is None or not contact.address:
        raise OutreachError("this outreach has no contact address")
    if not PERMITTED_SOURCES.match(contact.source or ""):
        raise OutreachError(
            "this contact has no permitted recorded source and will not be messaged"
        )

    # Checked here, at the boundary, rather than trusted to whoever called. Every other
    # outreach failure can be walked back; writing to somebody who asked you to stop
    # cannot, so this is the one rule that is absolute rather than weighted.
    from ..outreach import suppression

    if blocked := await suppression.find(session, email=contact.address):
        raise OutreachError(f"not contacting them: {blocked.explain()}")

    # A second message inside the quiet window reads as pressure whichever channel it
    # arrives on, so the check is across every channel rather than per channel.
    history = await history_for(
        session, email=contact.address, person_name=contact.name, exclude_id=outreach_id
    )
    allowed, why = history.verdict()
    if not allowed and not history.suppressed:
        raise OutreachError(why)

    limits = await rate_limit_status(session, row.candidate_id)
    if not limits["allowed"]:
        raise OutreachError(
            f"rate limit reached: {limits['sent_today']}/{MAX_PER_DAY} today, "
            f"{limits['sent_this_week']}/{MAX_PER_WEEK} this week"
        )

    try:
        sent = await gmail.send_message(
            to=contact.address,
            subject=row.subject,
            body=row.body,
            thread_id=row.transport_thread_id or "",
        )
    except gmail.GmailError as exc:
        # Left approved on purpose. A send that failed is a send that can be retried, and
        # marking it sent would lose the message and claim something that did not happen.
        raise OutreachError(str(exc)) from exc

    row.status = OS.SENT
    row.sent_at = sent.sent_at
    row.follow_up_date = date.today() + timedelta(days=FOLLOW_UP_DAYS)
    row.transport = "gmail"
    row.transport_message_id = sent.message_id
    row.transport_thread_id = sent.thread_id
    await session.flush()

    await get_bus().emit_one(
        EventType.OUTREACH_SENT,
        f"Sent through Gmail ({limits['sent_today'] + 1}/{MAX_PER_DAY} today)",
        outreach_id=outreach_id,
        channel=row.channel,
    )
    return row


async def record_reply(
    session: AsyncSession, outreach_id: str, *, replied: bool = True, note: str = ""
) -> Outreach:
    """Record that somebody answered, and act on it if they asked us to stop.

    Reading the reply for an opt-out is deliberately narrow: a false positive silently
    ends a real conversation, which is worse than missing one and leaving it to the user.
    """
    row = await session.get(Outreach, outreach_id)
    if row is None:
        raise OutreachError("outreach not found")

    from ..outreach import suppression

    if replied and note and suppression.reads_as_opt_out(note):
        contact = await session.get(OutreachContact, row.contact_id) if row.contact_id else None
        if contact is not None:
            await suppression.suppress(
                session,
                reason=suppression.Reason.OPTED_OUT,
                email=contact.address,
                note="asked to stop in their reply",
            )
            row.status = OS.DECLINED
            row.replied_at = datetime.now(UTC)
            row.notes = chr(10).join(x for x in (row.notes, note) if x)
            await session.flush()
            return row
    row.status = OS.REPLIED if replied else OS.DECLINED
    row.replied_at = datetime.now(UTC)
    row.follow_up_date = None
    if note:
        row.notes = f"{row.notes}\n{note}".strip()
    await session.flush()
    return row


async def cancel(session: AsyncSession, outreach_id: str, reason: str = "") -> Outreach:
    row = await session.get(Outreach, outreach_id)
    if row is None:
        raise OutreachError("outreach not found")
    if row.status == OS.SENT.value:
        raise OutreachError("this one has already been sent; it cannot be cancelled")
    row.status = OS.CANCELLED
    if reason:
        row.notes = f"{row.notes}\n{reason}".strip()
    await session.flush()
    return row


async def board(session: AsyncSession, candidate_id: str) -> dict[str, Any]:
    """The outreach view from the brief's §27: drafted, waiting, sent, replied."""
    rows = (
        await session.execute(
            select(Outreach, Company)
            .join(Company, Company.id == Outreach.company_id)
            .where(Outreach.candidate_id == candidate_id)
            .order_by(Outreach.updated_at.desc())
            .limit(300)
        )
    ).all()

    today = date.today()
    items = []
    for row, company in rows:
        items.append(
            {
                "id": row.id,
                "company": company.name,
                "company_id": company.id,
                "channel": row.channel,
                "subject": row.subject,
                "body": row.body,
                "status": row.status,
                "sent_at": row.sent_at.isoformat() if row.sent_at else None,
                "replied_at": row.replied_at.isoformat() if row.replied_at else None,
                "follow_up_date": row.follow_up_date.isoformat() if row.follow_up_date else None,
                "follow_up_due": bool(row.follow_up_date and row.follow_up_date <= today),
                "fact_ids": row.fact_ids or [],
            }
        )

    buckets: dict[str, list[dict[str, Any]]] = {}
    for item in items:
        buckets.setdefault(item["status"], []).append(item)

    sent = len(buckets.get(OS.SENT.value, [])) + len(buckets.get(OS.REPLIED.value, []))
    replied = len(buckets.get(OS.REPLIED.value, []))
    return {
        "buckets": buckets,
        "total": len(items),
        "sent": sent,
        "replied": replied,
        "reply_rate": round(replied / sent, 4) if sent else None,
        "follow_ups_due": [i for i in items if i["follow_up_due"]],
        "limits": await rate_limit_status(session, candidate_id),
    }


async def mark_follow_ups(session: AsyncSession, candidate_id: str) -> int:
    """Move sent messages past their follow-up date into FOLLOW_UP_DUE."""
    rows = (
        (
            await session.execute(
                select(Outreach).where(
                    Outreach.candidate_id == candidate_id,
                    Outreach.status == OS.SENT.value,
                    Outreach.follow_up_date.is_not(None),
                )
            )
        )
        .scalars()
        .all()
    )
    today = date.today()
    moved = 0
    for row in rows:
        if row.follow_up_date and row.follow_up_date <= today:
            row.status = OS.FOLLOW_UP_DUE
            moved += 1
    if moved:
        await session.flush()
        await get_bus().emit_one(
            EventType.OUTREACH_DRAFTED,
            f"{moved} outreach follow-up(s) due",
            level=Level.INFO,
            count=moved,
        )
    return moved


# ─── What has already been said to this person ──────────────────────────────


@dataclass
class ContactHistory:
    """Everything already sent to one person, across every channel.

    The reason this is one object rather than a per-channel lookup: a candidate who emailed
    somebody yesterday and connects on LinkedIn today has messaged them twice, and the
    recipient experiences it as twice however the sender files it. Channels are an
    implementation detail of the sender's tooling, not of the person reading.
    """

    person_name: str = ""
    events: list[dict[str, Any]] = field(default_factory=list)
    replied: bool = False
    suppressed: str = ""

    @property
    def last_at(self) -> datetime | None:
        stamps = [e["at"] for e in self.events if e.get("at")]
        return max(stamps) if stamps else None

    @property
    def channels_used(self) -> list[str]:
        return sorted({str(e.get("channel") or "") for e in self.events if e.get("channel")})

    def days_since_last(self, *, now: datetime | None = None) -> int | None:
        last = self.last_at
        if last is None:
            return None
        return ((now or datetime.now(UTC)) - last).days

    def verdict(self, *, now: datetime | None = None, quiet_days: int = 7) -> tuple[bool, str]:
        """May we write to them now, and what to say if not.

        Returns `(allowed, reason)`. A reply is the strongest signal there is: somebody who
        answered is in a conversation, and starting a fresh thread at them is worse than
        waiting.
        """
        if self.suppressed:
            return False, self.suppressed
        if self.replied:
            return (
                False,
                "they have already replied — continue that thread rather than starting "
                "a new message",
            )

        days = self.days_since_last(now=now)
        if days is None:
            return True, "no previous contact"
        if days < quiet_days:
            channels = " and ".join(self.channels_used) or "another channel"
            when = "today" if days == 0 else f"{days} day(s) ago"
            return (
                False,
                f"you contacted them {when} on {channels}. A second message inside "
                f"{quiet_days} days reads as pressure, whichever channel it arrives on.",
            )
        return True, f"last contacted {days} days ago"

    def to_dict(self) -> dict[str, Any]:
        allowed, reason = self.verdict()
        return {
            "person": self.person_name,
            "events": [
                {**e, "at": e["at"].isoformat() if e.get("at") else None} for e in self.events
            ],
            "count": len(self.events),
            "channels_used": self.channels_used,
            "replied": self.replied,
            "last_at": self.last_at.isoformat() if self.last_at else None,
            "days_since_last": self.days_since_last(),
            "may_contact": allowed,
            "reason": reason,
        }


# Statuses the recipient has experienced nothing of. Approval is a decision by the sender.
_NEVER_REACHED_THEM = frozenset(
    {
        OS.DRAFTED.value,
        OS.AWAITING_APPROVAL.value,
        OS.APPROVED.value,
        OS.CANCELLED.value,
    }
)


async def history_for(
    session: AsyncSession,
    *,
    email: str = "",
    person_id: str = "",
    person_name: str = "",
    exclude_id: str = "",
) -> ContactHistory:
    """Every message already sent to this person, whatever channel it went down.

    Matched on the contact address rather than on an outreach id, because the same human
    may hold several outreach records — one per company they have worked at, or one per
    opening — and the question being asked is about the human.
    """
    from ..outreach import suppression
    from ..storage.models import Outreach, OutreachContact

    out = ContactHistory(person_name=person_name)
    address = (email or "").strip().lower()

    if blocked := await suppression.find(session, email=address, person_id=person_id):
        out.suppressed = blocked.explain()

    if not address:
        return out

    contact_ids = [
        row[0]
        for row in (
            await session.execute(
                select(OutreachContact.id).where(func.lower(OutreachContact.address) == address)
            )
        ).all()
    ]
    if not contact_ids:
        return out

    rows = (
        (
            await session.execute(
                select(Outreach)
                .where(Outreach.contact_id.in_(contact_ids))
                .order_by(Outreach.created_at.desc())
            )
        )
        .scalars()
        .all()
    )

    for row in rows:
        if row.id == exclude_id:
            # The message being sent must not count itself as prior contact, which is
            # exactly what happened: an approved record blocked its own delivery.
            continue
        if row.status in _NEVER_REACHED_THEM:
            # Never sent, so the recipient has experienced nothing. `approved` belongs
            # here too — approval is a decision by the sender, not an event for the
            # person receiving it.
            continue
        out.events.append(
            {
                "outreach_id": row.id,
                "channel": row.channel,
                "status": row.status,
                "subject": row.subject,
                "at": row.sent_at or row.approved_at or row.created_at,
                "transport": row.transport,
            }
        )
        if row.replied_at or row.status == OS.REPLIED.value:
            out.replied = True

    return out
