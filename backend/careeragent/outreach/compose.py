"""Writing a message to one person, for one job, at one company.

The existing outreach prompt writes a good speculative note to a *company*. This writes to
a *person*, and the difference is the whole point of the phase: a message that could have
been sent to anyone reads as a template, and a template is the thing recruiters delete.

Three inputs change the message, and they change it differently.

**Who they are.** A technical recruiter wants the role, the skills, availability and a
resume — they are matching a requisition. An engineering manager wants the projects and the
technology, because they are imagining the person on their team. A founder or CTO at a small
company wants the domain and the product, because they are thinking about the business. The
same facts, ordered and framed differently, not one message with the salutation swapped.

**Which channel.** Email carries an attachment and can afford five sentences. A LinkedIn
message has no attachment, is read on a phone, and dies past about eighty words. Sending the
same text down both is worse than sending one: it makes the second one redundant and marks
the sender as automated.

**What is true.** Every claim traces to a Fact Bank entry, and the same repair loop the
resume tailoring uses removes anything uncited rather than arguing with it. The company
detail is the researched profile and nothing else — which is what stops a message praising a
product the company never shipped.

Nothing here sends. It writes, and hands the result to a human.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from ..logging import get_logger
from ..models.candidate import Candidate
from ..models.person import Person
from ..people.relevance import function_of, seniority_of

log = get_logger("outreach.compose")


class Channel(StrEnum):
    EMAIL = "email"
    LINKEDIN = "linkedin"

    @property
    def word_limit(self) -> int:
        """Email can afford a paragraph. LinkedIn is read on a phone."""
        return 150 if self is Channel.EMAIL else 80

    @property
    def carries_attachment(self) -> bool:
        return self is Channel.EMAIL

    @property
    def has_subject(self) -> bool:
        """A LinkedIn message has no subject line. Offering one invites the user to
        write something that is then silently discarded."""
        return self is Channel.EMAIL


class Audience(StrEnum):
    """Who is reading, which decides what the message leads with."""

    RECRUITER = "recruiter"
    ENGINEERING_LEADER = "engineering_leader"
    EXECUTIVE = "executive"
    PEER = "peer"

    def emphasis(self) -> str:
        return {
            Audience.RECRUITER: (
                "the role being applied for, the two or three skills it names, when the "
                "candidate is available, and that a resume is attached"
            ),
            Audience.ENGINEERING_LEADER: (
                "one concrete project and what was built with it, the technologies that "
                "overlap with their team's work, and why this role in particular"
            ),
            Audience.EXECUTIVE: (
                "the candidate's strongest technical result, the domain it was in, and "
                "why this company's product is the interesting place to do that work"
            ),
            Audience.PEER: (
                "the shared technical ground — what the candidate has built that this "
                "person would recognise — and a specific question about the team"
            ),
        }[self]

    def opening_rule(self) -> str:
        return {
            Audience.RECRUITER: "Name the role in the first sentence.",
            Audience.ENGINEERING_LEADER: "Lead with what was built, not with wanting a job.",
            Audience.EXECUTIVE: "Lead with the result, in one clause.",
            Audience.PEER: "Lead with the shared technical ground.",
        }[self]


def audience_for(person: Person, *, company_size: int | None = None) -> Audience:
    """Which kind of reader this person is.

    A founder at a twelve-person company is not an `EXECUTIVE` in this sense — they are the
    engineering leader, because that is the hat they will be wearing when they read it.
    """
    function = function_of(person.title or "")
    seniority = seniority_of(person.title or "")
    small = company_size is not None and company_size < 60

    if function == "recruiting":
        return Audience.RECRUITER
    if seniority >= 5:
        # At twelve people the founder *is* the engineering lead, and a message pitched at
        # a distant executive would be the wrong register entirely.
        return Audience.ENGINEERING_LEADER if small else Audience.EXECUTIVE
    if function in {"ai_ml", "data", "engineering", "product"}:
        return Audience.ENGINEERING_LEADER if seniority >= 2 else Audience.PEER
    if seniority >= 4:
        return Audience.EXECUTIVE
    return Audience.PEER


@dataclass
class Message:
    """One drafted message, with everything needed to judge it before it is sent."""

    channel: Channel
    audience: Audience
    subject: str = ""
    body: str = ""
    subject_options: list[str] = field(default_factory=list)
    cited_fact_ids: list[str] = field(default_factory=list)
    attach_resume: bool = False
    word_count: int = 0
    repaired: bool = False
    warnings: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return bool(self.body.strip()) and not self.warnings

    def to_dict(self) -> dict[str, Any]:
        return {
            "channel": self.channel.value,
            "audience": self.audience.value,
            "subject": self.subject,
            "subject_options": self.subject_options,
            "body": self.body,
            "cited_fact_ids": self.cited_fact_ids,
            "attach_resume": self.attach_resume,
            "word_count": self.word_count,
            "repaired": self.repaired,
            "warnings": self.warnings,
        }


# Openings that mark a message as automated before the second sentence. Every one of these
# appears in the templates this is trying not to sound like.
BANNED_OPENINGS = (
    "i hope this finds you well",
    "i hope this email finds you",
    "i came across your company",
    "i came across your profile",
    "i stumbled upon",
    "i am reaching out to you today",
    "my name is",
    "allow me to introduce",
    "to whom it may concern",
    "dear sir or madam",
    "i trust this message finds",
)

# Claims about a relationship that does not exist. These are the ones a language model
# reaches for when it has nothing specific to say, and each is a lie.
FABRICATED_RAPPORT = (
    "we met",
    "we spoke",
    "as we discussed",
    "following our conversation",
    "our mutual connection",
    "a mutual friend",
    "was referred to",
    "referred me to you",
    "i was told by",
    "we have worked together",
    "i have long admired",
    "i have been following your work",
    "your recent post",
)


def check(body: str, channel: Channel) -> list[str]:
    """What is wrong with this draft, in the words the user should see.

    Separate from generation so that a hand-edited message is checked by exactly the same
    rules as a generated one — the user's edit is not more trustworthy than the model's
    draft simply because a human made it.
    """
    warnings: list[str] = []
    lowered = " ".join((body or "").lower().split())

    if not lowered:
        return ["the message is empty"]

    for phrase in BANNED_OPENINGS:
        if phrase in lowered[:200]:
            warnings.append(f"opens with {phrase!r}, which reads as a template")
            break

    for phrase in FABRICATED_RAPPORT:
        if phrase in lowered:
            warnings.append(
                f"claims a relationship that does not exist ({phrase!r}) — this must be "
                "removed, not softened"
            )
            break

    words = len(body.split())
    if words > channel.word_limit:
        warnings.append(f"{words} words; {channel.value} should be under {channel.word_limit}")

    if channel is Channel.LINKEDIN and ("attached" in lowered or "attachment" in lowered):
        warnings.append("mentions an attachment, and LinkedIn messages carry none")

    return warnings


def subject_candidates(
    *, candidate_name: str, role: str, company: str, audience: Audience
) -> list[str]:
    """Several subjects, best first, so the user picks rather than accepts.

    A subject line is the one part of a message that is read by everyone who receives it and
    rewritten by nobody, so offering one is offering too few.
    """
    name = candidate_name.strip()
    role = (role or "").strip()
    company = (company or "").strip()

    if audience is Audience.RECRUITER and role:
        options = [
            f"Application — {role} — {name}",
            f"{role} at {company} — {name}",
            f"{name} — applying for {role}",
        ]
    elif role and company:
        options = [
            f"{role} at {company}",
            f"Interested in {role} work at {company}",
            f"{name} — {role}",
        ]
    else:
        options = [
            f"{name} — {role or 'engineering'} background",
            f"Interested in {company or 'your team'}",
        ]

    seen: set[str] = set()
    out: list[str] = []
    for option in options:
        trimmed = " ".join(option.split())[:78]
        if trimmed and trimmed.lower() not in seen:
            seen.add(trimmed.lower())
            out.append(trimmed)
    return out


def signature(candidate: Candidate) -> str:
    """The contact details the candidate actually has. No placeholders.

    A signature with an empty line where a phone number should be is worse than a shorter
    signature, and a fabricated one is worse than either.
    """
    lines: list[str] = [candidate.name.strip()]
    if candidate.email:
        lines.append(candidate.email)
    if getattr(candidate, "phone", ""):
        lines.append(str(candidate.phone))

    for fact in candidate.facts:
        if getattr(fact.kind, "value", "") != "link":
            continue
        label = (fact.label or "").strip()
        if label and label.lower() not in " ".join(lines).lower():
            lines.append(label)

    return "\n".join(line for line in lines if line.strip())


_WHITESPACE = re.compile(r"[ \t]+")


def tidy(body: str) -> str:
    """Normalise spacing without touching the words.

    Generated text arrives with doubled spaces and stray blank lines, and a message that
    looks carelessly formatted undercuts one that reads carefully written.
    """
    lines = [_WHITESPACE.sub(" ", line).strip() for line in (body or "").split("\n")]
    out: list[str] = []
    for line in lines:
        if not line and out and not out[-1]:
            continue
        out.append(line)
    return "\n".join(out).strip()


# ─── Drafting ───────────────────────────────────────────────────────────────


def _company_notes(company: Any) -> str:
    """The only permitted source of company detail.

    Assembled from the researched profile rather than from model recall. This is what stops
    a message praising a product the company never shipped — the model cannot mention what
    it was not given.
    """
    bits: list[str] = []
    if getattr(company, "description", ""):
        bits.append(" ".join(str(company.description).split())[:400])
    if getattr(company, "industry", ""):
        bits.append(f"Industry: {company.industry}.")
    tech = list(getattr(company, "tech_focus", None) or [])
    if tech:
        bits.append(f"Technology on record: {', '.join(tech[:8])}.")
    if getattr(company, "hq_location", ""):
        bits.append(f"Location: {company.hq_location}.")
    return " ".join(b for b in bits if b) or "Nothing researched about them yet."


def _fact_lines(candidate: Candidate, *, audience: Audience) -> list[str]:
    """The candidate facts worth offering, ordered for this reader.

    A recruiter matching a requisition wants the skills first; an engineering leader
    imagining somebody on their team wants the projects. Giving the model the same list in
    a different order is a cheap and honest way to change what it reaches for.
    """
    from ..models.candidate import FactKind

    projects = [f for f in candidate.facts if f.kind is FactKind.PROJECT]
    experience = [f for f in candidate.facts if f.kind is FactKind.EXPERIENCE]
    education = [f for f in candidate.facts if f.kind is FactKind.EDUCATION]
    skills = sorted({f.label for f in candidate.facts if f.kind is FactKind.SKILL})

    if audience is Audience.RECRUITER:
        ordered = [*experience, *education, *projects]
    elif audience is Audience.EXECUTIVE:
        ordered = [*projects, *experience]
    else:
        ordered = [*projects, *experience, *education]

    lines = [
        f"{fact.id} :: {fact.kind.value} :: {' '.join(fact.label.split())}"
        + (f" — {' '.join(fact.body.split())[:200]}" if fact.body else "")
        for fact in ordered[:10]
    ]
    if skills:
        lines.append(f"skills :: skill :: {', '.join(skills[:24])}")
    return lines


def draft_to_person(
    candidate: Candidate,
    person: Person,
    company: Any,
    *,
    channel: Channel = Channel.EMAIL,
    job_title: str = "",
    job_note: str = "",
    company_size: int | None = None,
    router: Any = None,
) -> Message:
    """Write one message to one person. Never sends; returns something a human judges.

    The draft is checked by exactly the rules a hand-written one would be, and a failure
    is returned as a warning on a readable draft rather than as an exception — the user
    can nearly always fix a message faster than they can re-run a generator.
    """
    audience = audience_for(person, company_size=company_size)
    message = Message(
        channel=channel,
        audience=audience,
        attach_resume=channel.carries_attachment,
        subject_options=subject_candidates(
            candidate_name=candidate.name,
            role=job_title,
            company=getattr(company, "name", "") or person.company,
            audience=audience,
        )
        if channel.has_subject
        else [],
    )
    message.subject = message.subject_options[0] if message.subject_options else ""

    if router is None:
        from ..llm.router import get_router

        router = get_router()
    if not router.available():
        message.warnings.append("no language model is configured; write this one yourself")
        return message

    from ..agents.trust import harden
    from ..llm.router import Tier, tokens_for
    from ..prompts import get as get_prompt

    prompt = get_prompt("person_message")
    rendered = prompt.render(
        recipient_name=person.name,
        recipient_title=person.title or "role unknown",
        company=getattr(company, "name", "") or person.company,
        audience_description=audience.value.replace("_", " "),
        emphasis=audience.emphasis(),
        opening_rule=audience.opening_rule(),
        channel=channel.value,
        word_limit=channel.word_limit,
        channel_note=(
            "A tailored resume is attached; mention it once."
            if channel.carries_attachment
            else "There is no attachment. Do not mention one."
        ),
        job_title=job_title or "no specific opening — this is speculative",
        job_note=job_note or "",
        company_notes=_company_notes(company),
        facts="\n".join(_fact_lines(candidate, audience=audience)),
    )

    data = router.complete_json(
        rendered,
        tier=Tier.CHEAP,
        system=harden(prompt.system),
        temperature=0.4,
        max_tokens=tokens_for(channel.word_limit * 3),
        agent="outreach_compose",
    )
    if not isinstance(data, dict) or not str(data.get("body") or "").strip():
        message.warnings.append("the model returned nothing usable; write this one yourself")
        return message

    message.body = tidy(str(data["body"]))
    message.cited_fact_ids = [str(c) for c in (data.get("cite") or [])][:12]
    message.word_count = len(message.body.split())
    message.warnings = check(message.body, channel)

    if message.warnings:
        log.info(
            "message_needs_attention",
            channel=channel.value,
            audience=audience.value,
            warnings=len(message.warnings),
        )
    return message
