"""People at a company, and what may be believed about how to reach them.

The rule that shapes every type here: **a contact detail cannot exist without a source.**
Not "should not" — cannot. `ContactDetail` has no constructor that omits provenance, so
there is nowhere in this codebase to put an email address that nobody published. The
temptation this forecloses is specific and well known: given "Rahul Kumar" at
`acme.com`, `rahul.kumar@acme.com` is a plausible guess, and a plausible guess presented
beside real data is indistinguishable from it. Guessing is not implemented rather than
merely discouraged, because a discouraged thing eventually gets implemented.

The second rule is that **a person's usefulness depends on the job being applied for.** A
CEO is not automatically the best contact. For an entry-level ML opening, an engineering
manager or a technical recruiter is a far better one, and a finance manager is nobody at
all. So relevance is computed against the candidate's target role rather than read off a
seniority ladder.

What this deliberately does not do is compile personal information. Everything here is a
professional fact a company published about somebody's role at that company — the sort of
thing that appears on a leadership page. There is no home address, no personal email, no
phone number, and no private profile.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any


class Verification(StrEnum):
    """How much a contact detail may be trusted.

    `INFERRED` exists to be *refused*, not used. Nothing in this project produces one —
    the state is here so that a detail arriving from an external provider that admits to
    guessing can be labelled honestly and then declined for sending.
    """

    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    INFERRED = "inferred"
    UNKNOWN = "unknown"

    @property
    def safe_to_contact(self) -> bool:
        """Only a detail somebody actually published is sent to automatically."""
        return self is Verification.VERIFIED


class SourceKind(StrEnum):
    """Where a fact about a person came from. Anything not on this list is not a source."""

    COMPANY_WEBSITE = "company_website"
    LEADERSHIP_PAGE = "leadership_page"
    TEAM_PAGE = "team_page"
    CAREERS_PAGE = "careers_page"
    JOB_POSTING = "job_posting"
    PRESS_PAGE = "press_page"
    PUBLIC_DIRECTORY = "public_directory"
    ENRICHMENT_PROVIDER = "enrichment_provider"
    USER_ENTERED = "user_entered"

    @property
    def is_first_party(self) -> bool:
        """Published by the company itself — the strongest kind of source there is."""
        return self in {
            SourceKind.COMPANY_WEBSITE,
            SourceKind.LEADERSHIP_PAGE,
            SourceKind.TEAM_PAGE,
            SourceKind.CAREERS_PAGE,
            SourceKind.PRESS_PAGE,
        }


class EmailKind(StrEnum):
    """Whose inbox this is. The distinction changes how a message should be written."""

    # A named individual's work address, as published.
    PERSONAL_WORK = "personal_work"
    # careers@, hr@, jobs@ — a department, not a person.
    GENERIC_COMPANY = "generic_company"
    UNKNOWN = "unknown"


# Local parts that mean "a team reads this", not "a person reads this".
_GENERIC_LOCAL_PARTS = frozenset(
    {
        "careers",
        "career",
        "jobs",
        "job",
        "hr",
        "hiring",
        "recruiting",
        "recruitment",
        "talent",
        "info",
        "hello",
        "contact",
        "support",
        "admin",
        "office",
        "enquiries",
        "inquiries",
        "team",
        "people",
        "work",
        "apply",
        "resume",
        "cv",
    }
)

_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LINKEDIN_PERSON = re.compile(r"^https?://([a-z]{2,3}\.)?linkedin\.com/in/[^/\s?]+", re.I)


def classify_email(address: str) -> EmailKind:
    """Is this a person's address or a department's?"""
    clean = (address or "").strip().lower()
    if not _EMAIL.match(clean):
        return EmailKind.UNKNOWN
    local = clean.split("@", 1)[0]
    # `careers-india@` and `hr.team@` are still departments.
    head = re.split(r"[.\-_+]", local)[0]
    if local in _GENERIC_LOCAL_PARTS or head in _GENERIC_LOCAL_PARTS:
        return EmailKind.GENERIC_COMPANY
    return EmailKind.PERSONAL_WORK


def is_person_profile(url: str) -> bool:
    """A LinkedIn URL that points at a person, not a company page or a post."""
    return bool(_LINKEDIN_PERSON.match((url or "").strip()))


@dataclass(frozen=True)
class ContactDetail:
    """One way to reach someone, and the evidence that it is real.

    Frozen and source-required. There is deliberately no way to construct one of these
    without saying where it came from — which is what makes fabricating an address
    impossible rather than merely forbidden.
    """

    value: str
    kind: SourceKind
    source_url: str
    verification: Verification = Verification.UNVERIFIED
    confidence: float = 0.5
    found_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if not (self.value or "").strip():
            raise ValueError("a contact detail needs a value")
        if not (self.source_url or "").strip() and self.kind is not SourceKind.USER_ENTERED:
            raise ValueError(
                f"a {self.kind.value} contact detail needs the URL it was found at — "
                "a detail with no source cannot be distinguished from a guess"
            )

    @property
    def safe_to_contact(self) -> bool:
        return self.verification.safe_to_contact

    def to_dict(self) -> dict[str, Any]:
        return {
            "value": self.value,
            "source": self.kind.value,
            "source_url": self.source_url,
            "verification": self.verification.value,
            "confidence": round(self.confidence, 2),
            "found_at": self.found_at.isoformat(),
            "safe_to_contact": self.safe_to_contact,
        }


@dataclass
class Person:
    """Somebody who works at a company, as that company describes them publicly."""

    name: str
    title: str = ""
    company: str = ""
    company_id: str = ""
    department: str = ""
    location: str = ""
    # A short professional description, as published. Never assembled from inference.
    bio: str = ""

    email: ContactDetail | None = None
    linkedin: ContactDetail | None = None
    profile_url: ContactDetail | None = None

    sources: list[dict[str, Any]] = field(default_factory=list)
    # True when something a product name cannot fake was found beside them — a personal
    # LinkedIn URL, or an email whose local part matches their name. False means the record
    # rests on a name sitting next to a job title, which is real evidence but weaker.
    corroborated: bool = False
    first_seen_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    last_verified_at: datetime | None = None

    @property
    def email_kind(self) -> EmailKind:
        return classify_email(self.email.value) if self.email else EmailKind.UNKNOWN

    @property
    def reachable(self) -> bool:
        """Is there any channel at all, verified or not?"""
        return bool(self.email or self.linkedin)

    @property
    def contactable_now(self) -> bool:
        """Is there a channel we would actually send to without asking again?

        A LinkedIn profile is a reference, not a transport — this project does not send
        LinkedIn messages automatically — so only a verified email counts here.
        """
        return bool(self.email and self.email.safe_to_contact)

    def key(self) -> str:
        """An identity for deduplication.

        LinkedIn first, because a profile URL identifies one human being. Falling back to
        name-and-company is weaker and is only used where nothing better exists — two
        people with the same name at the same company is rare but not impossible, which is
        why `entity_key` is never the sole basis for a merge.
        """
        if self.linkedin and self.linkedin.value:
            return f"linkedin:{self.linkedin.value.rstrip('/').lower()}"
        if self.email and self.email.value:
            return f"email:{self.email.value.strip().lower()}"
        return f"name:{normalize_name(self.name)}@{(self.company or '').strip().lower()}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "title": self.title,
            "company": self.company,
            "company_id": self.company_id,
            "department": self.department,
            "location": self.location,
            "bio": self.bio[:400],
            "email": self.email.to_dict() if self.email else None,
            "email_kind": self.email_kind.value,
            "linkedin": self.linkedin.to_dict() if self.linkedin else None,
            "profile_url": self.profile_url.to_dict() if self.profile_url else None,
            "reachable": self.reachable,
            "contactable_now": self.contactable_now,
            "corroborated": self.corroborated,
            "sources": self.sources[:6],
            "first_seen_at": self.first_seen_at.isoformat(),
            "last_verified_at": (
                self.last_verified_at.isoformat() if self.last_verified_at else None
            ),
        }


_NAME_NOISE = re.compile(r"\b(mr|mrs|ms|dr|prof|sri|shri|jr|sr|phd|mba)\b\.?", re.I)
_NON_NAME = re.compile(r"[^a-z\s]")


def normalize_name(name: str) -> str:
    """A comparison key for a person's name. Never displayed."""
    text = _NAME_NOISE.sub(" ", (name or "").lower())
    text = _NON_NAME.sub(" ", text)
    return " ".join(text.split())


def _same_job(a: str, b: str) -> bool:
    """Do these two titles name the same role at the same level?

    Uses the function and seniority vocabulary the ranker already has, so the two cannot
    disagree about what a title means.
    """
    from ..people.relevance import function_of, seniority_of

    function_a, function_b = function_of(a), function_of(b)
    if not function_a or function_a != function_b:
        return False
    return seniority_of(a) == seniority_of(b)


def same_person(a: Person, b: Person) -> bool:
    """Are these two records the same human being?

    Never on name alone. Two people called Rahul Kumar can work at the same company, and a
    wrong merge means a message addressed to one of them describing the other's job. A
    matching LinkedIn URL or work email is identity; a matching name needs the company and
    something else — a title or a location — to agree as well.
    """
    if a.linkedin and b.linkedin:
        return a.linkedin.value.rstrip("/").lower() == b.linkedin.value.rstrip("/").lower()
    if a.email and b.email:
        return a.email.value.strip().lower() == b.email.value.strip().lower()

    name_a, name_b = normalize_name(a.name), normalize_name(b.name)
    if not name_a or name_a != name_b:
        return False
    if (a.company or "").strip().lower() != (b.company or "").strip().lower():
        return False

    # Same name, same company. Needs one more agreement before we call it one person.
    #
    # Titles agree when one contains the other *or* when they describe the same job at the
    # same level: "CTO and Co-Founder" and "Chief Technology Officer and Co-Founder" are
    # one man with two spellings, and string containment split him into two records.
    titles_agree = bool(a.title and b.title) and (
        a.title.lower() in b.title.lower()
        or b.title.lower() in a.title.lower()
        or _same_job(a.title, b.title)
    )
    locations_agree = bool(a.location and b.location) and (a.location.lower() == b.location.lower())
    return titles_agree or locations_agree


def merge(primary: Person, other: Person) -> Person:
    """Fold a second record into the first, keeping the better-sourced detail.

    "Better" means verification first, then a first-party source, then confidence. A
    company's own leadership page beats an enrichment provider on the same fact, because
    the company is the authority on who works there.
    """

    def rank(detail: ContactDetail) -> tuple[bool, bool, float]:
        # Verification first, then whether the company published it themselves, then
        # confidence. A leadership page beats an enrichment provider on the same fact:
        # the company is the authority on who works there.
        return (
            detail.verification is Verification.VERIFIED,
            detail.kind.is_first_party,
            detail.confidence,
        )

    def better(a: ContactDetail | None, b: ContactDetail | None) -> ContactDetail | None:
        if a is None or b is None:
            return a or b
        return a if rank(a) >= rank(b) else b

    primary.email = better(primary.email, other.email)
    primary.linkedin = better(primary.linkedin, other.linkedin)
    primary.profile_url = better(primary.profile_url, other.profile_url)

    primary.title = primary.title or other.title
    primary.department = primary.department or other.department
    primary.location = primary.location or other.location
    primary.bio = primary.bio or other.bio
    primary.company_id = primary.company_id or other.company_id

    seen = {s.get("url") for s in primary.sources}
    for source in other.sources:
        if source.get("url") not in seen:
            primary.sources.append(source)
    primary.first_seen_at = min(primary.first_seen_at, other.first_seen_at)
    return primary
