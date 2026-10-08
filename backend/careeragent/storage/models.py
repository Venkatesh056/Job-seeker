"""SQLAlchemy ORM models.

Implements the schema from docs/01-architecture.md §5. Three decisions carried
over from that document, restated here because they are easy to undo by accident:

  * `job` / `job_listing` are separate tables. One opening seen on three boards
    is one job row and three listing rows, so "which listing did we apply
    through?" and "was this posting removed?" stay answerable.
  * `resume_render.fact_ids` stores the evidence link rather than recomputing
    it. When a recruiter asks about a bullet, the facts behind it are
    retrievable.
  * `job_score.weights_version` records which weight set produced a score.
    Without it the learning engine cannot tell "this job scored badly" from "our
    weights were different then".
"""

from __future__ import annotations

import enum
import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    Boolean,
    Date,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from .types import EmbeddingType, JSONType, UTCDateTime


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, onupdate=_now)


# ─── Identity ───────────────────────────────────────────────────────────────


class User(Base, TimestampMixin):
    """An account. One person, one login, one candidate profile.

    Separate from `candidate` rather than merged into it, because they answer different
    questions and change at different rates. `candidate` is *who you are professionally* —
    name, location, work authorization, the things a resume states and an employer reads.
    `user` is *who is holding the session* — an email that must be unique, a password hash,
    and whether the account is still allowed in.

    Merging them would mean a password hash sits on the record the resume parser rewrites
    every time a CV is uploaded, and an account would be deletable by re-parsing a file.

    `password_hash` is nullable on purpose: an account authenticated by an external provider
    (Supabase) has no local password, and storing an empty string for one would be
    indistinguishable from a password that hashes to nothing.
    """

    __tablename__ = "app_user"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Stored lowercased and stripped. The unique constraint is over this column, so
    # "Me@Example.com" cannot register a second account against "me@example.com".
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True)
    display_name: Mapped[str] = mapped_column(String(200), default="")

    # scrypt, parameters carried inside the string. Never logged, never serialised by any
    # route: there is no response model in this system with a field to put it in.
    password_hash: Mapped[str | None] = mapped_column(String(512))
    # "local" or "supabase". Which authority vouched for this person.
    auth_provider: Mapped[str] = mapped_column(String(20), default="local")
    external_id: Mapped[str | None] = mapped_column(String(128), index=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    # Not a role system. One flag, meaning "may read across tenants for support", and it is
    # never granted by any code path — only by editing the row by hand. A role table would
    # invite a second, subtler set of permissions nobody tests.
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

    last_login_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # Revocation, twice over, because the two mechanisms cover different tokens.
    #
    # `token_serial` is the one that works: every token this system issues carries the
    # serial it was minted under, revoking increments it, and a token whose serial is stale
    # is refused. Exact, and no clock is involved.
    #
    # A timestamp alone was the first attempt and it was wrong in a way only a fast test
    # caught: `iat` is whole seconds, so a token issued in the *same second* as the logout
    # compared equal and survived it. Anyone logging out immediately after logging in would
    # have kept their session, and the failure would have been invisible on a slow machine.
    token_serial: Mapped[int] = mapped_column(Integer, default=0)
    # Kept for tokens minted elsewhere — a Supabase session carries no serial of ours — and
    # because "when were sessions last revoked" is worth being able to answer.
    tokens_valid_from: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)


# ─── Candidate knowledge base ───────────────────────────────────────────────


class Candidate(Base, TimestampMixin):
    """A professional profile. The tenancy boundary for everything per-person.

    `user_id` is nullable so the column could be added to a database that already held a
    profile without inventing an account for it. A row with no owner is reachable only in
    single-user mode; multi-user resolution matches on the owner and finds nothing.
    """

    __tablename__ = "candidate"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    user_id: Mapped[str | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    location: Mapped[str] = mapped_column(String(200), default="")
    preferred_locations: Mapped[list] = mapped_column(JSONType, default=list)
    links: Mapped[dict] = mapped_column(JSONType, default=dict)
    work_authorization: Mapped[dict] = mapped_column(JSONType, default=dict)
    salary_preferences: Mapped[dict] = mapped_column(JSONType, default=dict)
    notice_period: Mapped[str] = mapped_column(String(100), default="")
    availability: Mapped[str] = mapped_column(String(100), default="")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    facts: Mapped[list[Fact]] = relationship(
        back_populates="candidate", cascade="all, delete-orphan"
    )


class Fact(Base, TimestampMixin):
    """The Fact Bank. Nothing reaches a resume or an answer without a row here."""

    __tablename__ = "candidate_fact"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30), index=True)
    label: Mapped[str] = mapped_column(String(300))
    body: Mapped[str] = mapped_column(Text, default="")
    evidence_source: Mapped[str] = mapped_column(String(40))
    evidence_locator: Mapped[str] = mapped_column(String(500))
    first_used_at: Mapped[date | None] = mapped_column(Date)
    last_used_at: Mapped[date | None] = mapped_column(Date)
    strength: Mapped[int] = mapped_column(Integer, default=3)
    # Flipped by human review, never by the parser. Unverified facts are
    # deliberately usable for matching but NOT for generated resume claims.
    verified: Mapped[bool] = mapped_column(Boolean, default=False)
    aliases: Mapped[list] = mapped_column(JSONType, default=list)
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingType(384))

    candidate: Mapped[Candidate] = relationship(back_populates="facts")

    __table_args__ = (Index("ix_fact_candidate_kind", "candidate_id", "kind"),)


class Gap(Base, TimestampMixin):
    """A skill jobs keep asking for that the candidate has no evidence for.

    Recorded instead of invented. Also the most useful career signal the system
    produces: what to learn next, ranked by how often it costs an application.
    """

    __tablename__ = "candidate_gap"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"), index=True
    )
    skill: Mapped[str] = mapped_column(String(200))
    seen_in_job_count: Mapped[int] = mapped_column(Integer, default=1)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    __table_args__ = (UniqueConstraint("candidate_id", "skill", name="uq_gap_candidate_skill"),)


# ─── Companies and the ATS registry ─────────────────────────────────────────


class Company(Base, TimestampMixin):
    __tablename__ = "company"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300), index=True)
    domain: Mapped[str | None] = mapped_column(String(255), index=True)
    website: Mapped[str | None] = mapped_column(String(500))
    industry: Mapped[str | None] = mapped_column(String(200))
    size_band: Mapped[str | None] = mapped_column(String(50))
    hq_location: Mapped[str | None] = mapped_column(String(300))
    description: Mapped[str] = mapped_column(Text, default="")
    tech_focus: Mapped[list] = mapped_column(JSONType, default=list)
    ai_relevance: Mapped[int | None] = mapped_column(Integer)
    signals: Mapped[dict] = mapped_column(JSONType, default=dict)
    fit_score: Mapped[float | None] = mapped_column(Float)
    researched_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    __table_args__ = (UniqueConstraint("normalized_name", name="uq_company_normalized_name"),)


class ATSBoard(Base, TimestampMixin):
    """Company → ATS + board slug. The asset that makes discovery work.

    Resolving this is the real cost in job discovery (docs/00-research.md §3), so
    it is resolved once and reused, with failure counts to spot a board that has
    gone quiet versus one that never existed.
    """

    __tablename__ = "ats_board"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str | None] = mapped_column(ForeignKey("company.id"), index=True)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    slug: Mapped[str] = mapped_column(String(300))
    discovered_via: Mapped[str] = mapped_column(String(40), default="unknown")
    confidence: Mapped[float] = mapped_column(Float, default=1.0)
    last_ok_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    last_job_count: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)

    # ─── Monitoring ─────────────────────────────────────────────────────────
    # When this board is next worth reading, and how long the gap should be.
    # Both are adaptive: a board that keeps producing new roles earns a shorter
    # interval, one that has been static for a fortnight earns a longer one. A
    # fixed schedule would either hammer quiet boards or miss busy ones, and the
    # difference between a company posting weekly and yearly is large.
    next_crawl_at: Mapped[datetime | None] = mapped_column(UTCDateTime, index=True)
    crawl_interval_hours: Mapped[int] = mapped_column(Integer, default=24)
    last_new_job_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Cumulative, so a board's usefulness is visible without reading the job table.
    total_new_jobs_seen: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("provider", "slug", name="uq_ats_board_provider_slug"),)


# ─── Jobs ───────────────────────────────────────────────────────────────────


class Job(Base, TimestampMixin):
    """The deduplicated logical opening."""

    __tablename__ = "job"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)  # = fingerprint
    company_id: Mapped[str | None] = mapped_column(ForeignKey("company.id"), index=True)
    company: Mapped[str] = mapped_column(String(300), index=True)
    company_domain: Mapped[str | None] = mapped_column(String(255))
    title: Mapped[str] = mapped_column(String(500))
    normalized_title: Mapped[str] = mapped_column(String(500), index=True)
    location: Mapped[str] = mapped_column(String(300), default="")
    work_mode: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    employment_type: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    seniority: Mapped[str] = mapped_column(String(20), default="unknown", index=True)
    salary: Mapped[dict | None] = mapped_column(JSONType)
    description: Mapped[str] = mapped_column(Text, default="")

    requirements: Mapped[list] = mapped_column(JSONType, default=list)
    preferred_skills: Mapped[list] = mapped_column(JSONType, default=list)
    education_requirements: Mapped[list] = mapped_column(JSONType, default=list)
    min_years_experience: Mapped[float | None] = mapped_column(Float)
    max_years_experience: Mapped[float | None] = mapped_column(Float)

    posted_date: Mapped[date | None] = mapped_column(Date, index=True)
    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    last_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    status: Mapped[str] = mapped_column(String(20), default="open", index=True)
    ats: Mapped[str | None] = mapped_column(String(40))
    embedding: Mapped[list[float] | None] = mapped_column(EmbeddingType(384))

    listings: Mapped[list[JobListing]] = relationship(
        back_populates="job", cascade="all, delete-orphan", lazy="selectin"
    )
    scores: Mapped[list[JobScore]] = relationship(
        back_populates="job", cascade="all, delete-orphan"
    )


class JobListing(Base, TimestampMixin):
    """One place a logical job was seen."""

    __tablename__ = "job_listing"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(String(40), index=True)
    source_job_id: Mapped[str] = mapped_column(String(200))
    url: Mapped[str] = mapped_column(String(1000))
    application_url: Mapped[str | None] = mapped_column(String(1000))
    ats: Mapped[str | None] = mapped_column(String(40))
    scraped_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    is_alive: Mapped[bool] = mapped_column(Boolean, default=True)

    job: Mapped[Job] = relationship(back_populates="listings")

    __table_args__ = (UniqueConstraint("source", "source_job_id", name="uq_listing_source_id"),)


class JobScore(Base, TimestampMixin):
    __tablename__ = "job_score"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate.id"), index=True)

    deterministic: Mapped[dict] = mapped_column(JSONType, default=dict)
    semantic: Mapped[dict | None] = mapped_column(JSONType)
    llm: Mapped[dict | None] = mapped_column(JSONType)

    job_fit: Mapped[float] = mapped_column(Float, default=0.0)
    company_fit: Mapped[float] = mapped_column(Float, default=0.0)
    final_score: Mapped[float] = mapped_column(Float, default=0.0, index=True)
    priority: Mapped[str] = mapped_column(String(20), default="reject", index=True)
    weights_version: Mapped[int] = mapped_column(Integer, default=1)

    job: Mapped[Job] = relationship(back_populates="scores")

    __table_args__ = (UniqueConstraint("job_id", "candidate_id", name="uq_score_job_candidate"),)


# ─── People at a company ────────────────────────────────────────────────────


class PersonRecord(Base, TimestampMixin):
    """Somebody who works at a company, as that company publishes them.

    Deliberately narrow. Everything here is a professional fact about a role at a company —
    the sort of thing that appears on a leadership page. There is no home address, no
    personal email and no private profile, and the columns give nowhere to put one.

    Contact details carry their source and verification state rather than sitting as bare
    strings, because an address with no provenance cannot be distinguished from a guess.
    """

    __tablename__ = "person"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )

    name: Mapped[str] = mapped_column(String(200), index=True)
    # Lowercased, punctuation stripped. For matching only; never displayed.
    normalized_name: Mapped[str] = mapped_column(String(200), index=True)
    title: Mapped[str] = mapped_column(String(300), default="")
    department: Mapped[str] = mapped_column(String(120), default="")
    location: Mapped[str] = mapped_column(String(200), default="")
    bio: Mapped[str] = mapped_column(Text, default="")

    # What the title means, as the ranker reads it.
    function: Mapped[str] = mapped_column(String(40), default="", index=True)
    seniority: Mapped[int] = mapped_column(Integer, default=0, index=True)
    hiring_authority: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    email: Mapped[str | None] = mapped_column(String(320))
    email_kind: Mapped[str] = mapped_column(String(24), default="unknown")
    email_source: Mapped[str | None] = mapped_column(String(1000))
    email_verification: Mapped[str] = mapped_column(String(16), default="unknown")
    email_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    email_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    linkedin_url: Mapped[str | None] = mapped_column(String(500))
    linkedin_source: Mapped[str | None] = mapped_column(String(1000))
    linkedin_verification: Mapped[str] = mapped_column(String(16), default="unknown")

    # True when something a product name cannot fake was found beside them — a personal
    # LinkedIn URL, or an email whose local part matches their name.
    corroborated: Mapped[bool] = mapped_column(Boolean, default=False, index=True)

    # DEPRECATED — superseded by the `person_relevance` table, which keys these on the
    # candidate they were computed for. Kept as a mirror of the current owner's score so
    # existing readers keep working; moving the storage and every reader in one change is
    # how a migration becomes an outage. Read `person_relevance` in new code.
    #
    # In multi-user mode these are whatever the last ranking wrote, which is why they are
    # no longer the source of truth.
    relevance: Mapped[int] = mapped_column(Integer, default=0, index=True)
    relevance_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    recommended_channel: Mapped[str] = mapped_column(String(40), default="")
    relevance_reasons: Mapped[list] = mapped_column(JSONType, default=list)
    scored_for_job_id: Mapped[str | None] = mapped_column(String(64))

    first_seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    last_verified_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    sources: Mapped[list[PersonSource]] = relationship(
        back_populates="person", cascade="all, delete-orphan", lazy="selectin"
    )

    __table_args__ = (
        # One record per person per company. A person who moves company is a new record,
        # because their relationship to the candidate changed with them.
        UniqueConstraint("company_id", "normalized_name", name="uq_person_company_name"),
    )


class PersonRelevance(Base, TimestampMixin):
    """How relevant one person is *to one candidate*, and what for.

    The thing `PersonRecord.relevance` could never be. That column is a single slot on a
    shared row, so the last candidate to run a ranking overwrites everybody else's answer —
    which with one user is untidy and with two is one person reading a score computed for
    another person's career.

    `scored_for_job_id` travels with the number because relevance is not a property of a
    person even within one candidate. An engineering manager is a 96 for an entry-level ML
    role and a 30 for a finance one; without the job id, a score on screen is
    unattributable and the next one looks like a contradiction rather than a different
    question.
    """

    __tablename__ = "person_relevance"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    candidate_id: Mapped[str] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"), index=True
    )

    relevance: Mapped[int] = mapped_column(Integer, default=0, index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    recommended_channel: Mapped[str] = mapped_column(String(40), default="")
    reasons: Mapped[list] = mapped_column(JSONType, default=list)
    scored_for_job_id: Mapped[str | None] = mapped_column(String(64))
    scored_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    __table_args__ = (
        # One score per person per candidate. A second row would mean two answers to one
        # question with nothing to choose between them.
        UniqueConstraint("person_id", "candidate_id", name="uq_person_relevance_owner"),
    )


class PersonSource(Base, TimestampMixin):
    """One page a person was seen on. Provenance, kept rather than summarised."""

    __tablename__ = "person_source"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    person_id: Mapped[str] = mapped_column(ForeignKey("person.id", ondelete="CASCADE"), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    url: Mapped[str] = mapped_column(String(1000))
    seen_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    person: Mapped[PersonRecord] = relationship(back_populates="sources")

    __table_args__ = (UniqueConstraint("person_id", "url", name="uq_person_source_url"),)


class Suppression(Base, TimestampMixin):
    """Somebody who must not be contacted, and why.

    Keyed on *either* an address or a person, and stored with both when both are known. An
    opt-out follows the person to whatever address turns up later; a bounce sticks to the
    address whoever holds it. Collapsing the two into one `suppressed` flag would lose that
    distinction, and the first time a new address was found for an opted-out contact the
    system would happily write to them again.
    """

    __tablename__ = "suppression"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    # Per candidate, not global. A do-not-contact list is a record of what somebody said to
    # *you*; it is not a fact about the person that other users inherit. Sharing it would
    # also leak the shape of one user's outreach to every other — who they wrote to, and who
    # told them to stop.
    #
    # Nullable so the column could be added to an existing table. A row with no owner is
    # treated as belonging to everyone, which is the safe direction: it over-suppresses.
    candidate_id: Mapped[str | None] = mapped_column(
        ForeignKey("candidate.id", ondelete="CASCADE"), index=True
    )
    # opted_out | bounced | do_not_contact | wrong_person
    reason: Mapped[str] = mapped_column(String(24), index=True)
    email: Mapped[str | None] = mapped_column(String(320), index=True)
    person_id: Mapped[str | None] = mapped_column(String(36), index=True)
    note: Mapped[str] = mapped_column(Text, default="")
    suppressed_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)


# ─── Applications ───────────────────────────────────────────────────────────


class ApplicationStatus(enum.StrEnum):
    DISCOVERED = "discovered"
    SHORTLISTED = "shortlisted"
    RESUME_TAILORED = "resume_tailored"
    READY = "ready"
    AWAITING_APPROVAL = "awaiting_approval"
    HUMAN_REQUIRED = "human_required"
    APPLYING = "applying"
    APPLIED = "applied"
    SUBMISSION_UNVERIFIED = "submission_unverified"
    FAILED = "failed"
    FAILED_TERMINAL = "failed_terminal"
    FOLLOW_UP_DUE = "follow_up_due"
    INTERVIEW = "interview"
    OFFER = "offer"
    REJECTED = "rejected"
    REJECTED_BY_COMPANY = "rejected_by_company"
    WITHDRAWN = "withdrawn"


class Application(Base, TimestampMixin):
    __tablename__ = "application"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    job_id: Mapped[str] = mapped_column(ForeignKey("job.id"), index=True)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate.id"), index=True)
    listing_id: Mapped[str | None] = mapped_column(ForeignKey("job_listing.id"))

    strategy: Mapped[str] = mapped_column(String(30), default="ats_direct")
    status: Mapped[str] = mapped_column(
        String(30), default=ApplicationStatus.DISCOVERED, index=True
    )
    ats: Mapped[str | None] = mapped_column(String(40))
    application_url: Mapped[str | None] = mapped_column(String(1000))
    external_reference: Mapped[str | None] = mapped_column(String(200))

    resume_render_id: Mapped[str | None] = mapped_column(String(36))
    cover_letter_id: Mapped[str | None] = mapped_column(String(36))

    submitted_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    # Proof of submission. APPLIED is not set without something in here —
    # docs/01-architecture.md §8.
    evidence: Mapped[dict] = mapped_column(JSONType, default=dict)

    match_score: Mapped[float | None] = mapped_column(Float)
    company_score: Mapped[float | None] = mapped_column(Float)
    final_score: Mapped[float | None] = mapped_column(Float)
    notes: Mapped[str] = mapped_column(Text, default="")
    follow_up_date: Mapped[date | None] = mapped_column(Date)

    # What the approval screen renders: filled fields, unanswered fields, the
    # pre-submit verdict and screenshot paths. Stored rather than recomputed
    # because the browser session that produced it is gone by the time a human
    # looks, and re-opening the form would show a different page.
    preview: Mapped[dict] = mapped_column(JSONType, default=dict)
    # Bounded retries. FAILED becomes FAILED_TERMINAL at the cap rather than
    # looping forever against a form that changed shape.
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str] = mapped_column(Text, default="")

    events: Mapped[list[ApplicationEvent]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )
    answers: Mapped[list[ApplicationAnswer]] = relationship(
        back_populates="application", cascade="all, delete-orphan"
    )

    # The duplicate guard, enforced by the database rather than by convention.
    __table_args__ = (
        UniqueConstraint("job_id", "candidate_id", name="uq_application_job_candidate"),
    )


class ApplicationEvent(Base):
    __tablename__ = "application_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("application.id", ondelete="CASCADE"), index=True
    )
    type: Mapped[str] = mapped_column(String(60))
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)

    application: Mapped[Application] = relationship(back_populates="events")


class ApplicationAnswer(Base, TimestampMixin):
    __tablename__ = "application_answer"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    application_id: Mapped[str] = mapped_column(
        ForeignKey("application.id", ondelete="CASCADE"), index=True
    )
    canonical_field: Mapped[str] = mapped_column(String(120))
    question_text: Mapped[str] = mapped_column(Text, default="")
    answer: Mapped[str] = mapped_column(Text, default="")
    fact_ids: Mapped[list] = mapped_column(JSONType, default=list)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    source: Mapped[str] = mapped_column(String(20), default="generated")

    application: Mapped[Application] = relationship(back_populates="answers")


class QuestionMemory(Base, TimestampMixin):
    """Answers reused across applications. Verified before reuse, never blindly."""

    __tablename__ = "question_memory"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate.id"), index=True)
    canonical_field: Mapped[str] = mapped_column(String(120), index=True)
    question_norm: Mapped[str] = mapped_column(String(500))
    answer: Mapped[str] = mapped_column(Text)
    times_used: Mapped[int] = mapped_column(Integer, default=0)
    last_used_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    requires_recheck: Mapped[bool] = mapped_column(Boolean, default=False)

    __table_args__ = (
        UniqueConstraint("candidate_id", "question_norm", name="uq_qmem_candidate_question"),
    )


# ─── Resumes ────────────────────────────────────────────────────────────────


class ResumeMaster(Base, TimestampMixin):
    __tablename__ = "resume_master"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate.id"), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    storage_key: Mapped[str] = mapped_column(String(500))
    content_hash: Mapped[str] = mapped_column(String(64), index=True)
    raw_text: Mapped[str] = mapped_column(Text, default="")
    parsed: Mapped[dict] = mapped_column(JSONType, default=dict)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)


class ResumeRender(Base, TimestampMixin):
    """One tailored resume for one job, with the evidence it was built from."""

    __tablename__ = "resume_render"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("job.id"), index=True)
    archetype: Mapped[str] = mapped_column(String(60), default="custom")
    storage_key: Mapped[str | None] = mapped_column(String(500))
    content: Mapped[dict] = mapped_column(JSONType, default=dict)
    fact_ids: Mapped[list] = mapped_column(JSONType, default=list)
    ats_score: Mapped[float | None] = mapped_column(Float)
    evidence_integrity: Mapped[float | None] = mapped_column(Float)
    keyword_match: Mapped[float | None] = mapped_column(Float)
    validation: Mapped[dict] = mapped_column(JSONType, default=dict)


# ─── Runs and observability ─────────────────────────────────────────────────


class AgentRun(Base, TimestampMixin):
    __tablename__ = "agent_run"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    kind: Mapped[str] = mapped_column(String(40), index=True)
    trigger: Mapped[str] = mapped_column(String(40), default="manual")
    search_config: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="running", index=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now)
    ended_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    stats: Mapped[dict] = mapped_column(JSONType, default=dict)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)


class AgentEventRow(Base):
    __tablename__ = "agent_event"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    type: Mapped[str] = mapped_column(String(60), index=True)
    level: Mapped[str] = mapped_column(String(10), default="info")
    message: Mapped[str] = mapped_column(Text, default="")
    job_id: Mapped[str | None] = mapped_column(String(64), index=True)
    application_id: Mapped[str | None] = mapped_column(String(36), index=True)
    payload: Mapped[dict] = mapped_column(JSONType, default=dict)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)


class LLMCall(Base):
    __tablename__ = "llm_call"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    agent: Mapped[str] = mapped_column(String(60), index=True)
    provider: Mapped[str] = mapped_column(String(40))
    model: Mapped[str] = mapped_column(String(120))
    prompt_tokens: Mapped[int] = mapped_column(Integer, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    cache_hit: Mapped[bool] = mapped_column(Boolean, default=False)
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)


# ─── Configuration ──────────────────────────────────────────────────────────


class SearchConfigRow(Base, TimestampMixin):
    __tablename__ = "search_config"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate.id"), index=True)
    name: Mapped[str] = mapped_column(String(120), default="default")
    config: Mapped[dict] = mapped_column(JSONType, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class RankingWeights(Base, TimestampMixin):
    """Versioned so a score can be traced to the weights that produced it, and so
    a learning-engine proposal can be diffed and reverted."""

    __tablename__ = "ranking_weights"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate.id"), index=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    weights: Mapped[dict] = mapped_column(JSONType, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    derived_from_run_id: Mapped[str | None] = mapped_column(String(36))
    rationale: Mapped[str] = mapped_column(Text, default="")


# ─── Cover letters ──────────────────────────────────────────────────────────


class CoverLetter(Base, TimestampMixin):
    """A generated cover letter, with the facts it was built from.

    `fact_ids` is not decoration: a cover letter makes stronger claims in prose
    than a resume does in bullets, so the evidence link is the only thing
    standing between a tailored letter and an invented one.
    """

    __tablename__ = "cover_letter"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate.id"), index=True)
    job_id: Mapped[str | None] = mapped_column(ForeignKey("job.id"), index=True)
    body: Mapped[str] = mapped_column(Text, default="")
    fact_ids: Mapped[list] = mapped_column(JSONType, default=list)
    storage_key: Mapped[str | None] = mapped_column(String(500))
    validation: Mapped[dict] = mapped_column(JSONType, default=dict)
    model: Mapped[str | None] = mapped_column(String(120))
    generated_by: Mapped[str] = mapped_column(String(20), default="llm")


# ─── Outreach ───────────────────────────────────────────────────────────────


class OutreachContact(Base, TimestampMixin):
    """A professional contact channel, recorded only from sources that publish it.

    `source` is mandatory and carries *where* the channel came from. A contact
    with no recorded source cannot be approved for sending — which is the
    mechanical form of the brief's §24 rule against harvesting private data.
    """

    __tablename__ = "outreach_contact"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    company_id: Mapped[str] = mapped_column(
        ForeignKey("company.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(200), default="")
    role: Mapped[str] = mapped_column(String(200), default="")
    channel: Mapped[str] = mapped_column(String(20), default="email")
    address: Mapped[str] = mapped_column(String(500), default="")
    source: Mapped[str] = mapped_column(String(300))
    confidence: Mapped[float] = mapped_column(Float, default=0.5)

    __table_args__ = (
        UniqueConstraint("company_id", "channel", "address", name="uq_contact_company_address"),
    )


class OutreachStatus(enum.StrEnum):
    DRAFTED = "drafted"
    AWAITING_APPROVAL = "awaiting_approval"
    APPROVED = "approved"
    SENT = "sent"
    REPLIED = "replied"
    BOUNCED = "bounced"
    DECLINED = "declined"
    FOLLOW_UP_DUE = "follow_up_due"
    CANCELLED = "cancelled"


class Outreach(Base, TimestampMixin):
    """One personalized message to one contact.

    `sent_at` is set only by the send path, and the send path refuses anything
    not in APPROVED. There is no code path from draft to sent that skips a human.
    """

    __tablename__ = "outreach"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate.id"), index=True)
    company_id: Mapped[str] = mapped_column(ForeignKey("company.id"), index=True)
    contact_id: Mapped[str | None] = mapped_column(ForeignKey("outreach_contact.id"))
    job_id: Mapped[str | None] = mapped_column(ForeignKey("job.id"))

    channel: Mapped[str] = mapped_column(String(20), default="email")
    subject: Mapped[str] = mapped_column(String(300), default="")
    body: Mapped[str] = mapped_column(Text, default="")
    fact_ids: Mapped[list] = mapped_column(JSONType, default=list)
    resume_render_id: Mapped[str | None] = mapped_column(String(36))

    status: Mapped[str] = mapped_column(String(30), default=OutreachStatus.DRAFTED, index=True)
    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    sent_at: Mapped[datetime | None] = mapped_column(UTCDateTime)

    # How it went out, and the provider's own receipt for it. "manual" means the user sent
    # the text themselves and told us; "gmail" means the API accepted it and returned an
    # id. The id is the evidence: without one, nothing here may claim a message was sent.
    transport: Mapped[str] = mapped_column(String(20), default="manual")
    transport_message_id: Mapped[str | None] = mapped_column(String(120))
    transport_thread_id: Mapped[str | None] = mapped_column(String(120))
    replied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)
    follow_up_date: Mapped[date | None] = mapped_column(Date)
    notes: Mapped[str] = mapped_column(Text, default="")

    # One live thread per company per candidate. Re-contacting is a deliberate
    # act (cancel the old one first), not an accident of running discovery twice.
    __table_args__ = (Index("ix_outreach_candidate_status", "candidate_id", "status"),)


# ─── Learning ───────────────────────────────────────────────────────────────


class LearningProposal(Base, TimestampMixin):
    """A proposed change to ranking weights, with the evidence behind it.

    Proposals are never auto-applied (docs/02-roadmap.md §4.6): with 50-200
    applications there is not enough data to fit weights, so the engine's job is
    to surface a pattern, state its sample size, and let a human decide.
    """

    __tablename__ = "learning_proposal"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    candidate_id: Mapped[str] = mapped_column(ForeignKey("candidate.id"), index=True)
    kind: Mapped[str] = mapped_column(String(40), default="ranking_weights")
    observation: Mapped[str] = mapped_column(Text, default="")
    sample_size: Mapped[int] = mapped_column(Integer, default=0)
    confidence: Mapped[str] = mapped_column(String(20), default="low")
    current: Mapped[dict] = mapped_column(JSONType, default=dict)
    proposed: Mapped[dict] = mapped_column(JSONType, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending", index=True)
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime)


# ─── Agent decision audit ───────────────────────────────────────────────────


class ProviderCall(Base):
    """How many calls this installation made to each external provider, per day.

    Only for providers that publish no usage endpoint of their own. It is a floor rather
    than a balance — it knows nothing about calls made elsewhere with the same key — and
    the integrations page labels it `local` for exactly that reason.

    One row per provider per day rather than one per call: the question is "how close am I
    to the monthly cap", and a row per call would be a million rows answering it.
    """

    __tablename__ = "provider_call"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    provider: Mapped[str] = mapped_column(String(40), index=True)
    day: Mapped[date] = mapped_column(Date, index=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    errors: Mapped[int] = mapped_column(Integer, default=0)

    __table_args__ = (UniqueConstraint("provider", "day", name="uq_provider_call_day"),)


class AgentDecision(Base):
    """Why an agent did what it did — spec §50, §77.

    The event log already records *what happened* ("resume tailored", "submitted").
    This records *why*: the inputs the agent saw, the evidence it had, what it chose,
    and how sure it was. Those are different questions, and only the second one answers
    "why did it apply to that?" three weeks later.

    Append-only and never updated. A decision is a historical fact about a moment; if the
    agent decides differently later, that is a second row, and the pair is the
    interesting record.

    Deliberately not foreign-keyed to `application` or `job`: decisions are made *before*
    those rows exist — a ranking decision precedes any application — and a constraint
    would force either a null column or a fake parent.
    """

    __tablename__ = "agent_decision"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    run_id: Mapped[str | None] = mapped_column(String(36), index=True)
    candidate_id: Mapped[str | None] = mapped_column(String(36), index=True)
    # No single-column index on `agent`: every read filters it alongside `at`, which the
    # composite `ix_agent_decision_agent_at` already covers as a leftmost prefix. This
    # table takes a row per decision on a hot path, so an index nothing reads is a pure
    # write cost.
    agent: Mapped[str] = mapped_column(String(60))
    task: Mapped[str] = mapped_column(String(200), default="")

    # What it was looking at. Ids rather than copies: the full job description in every
    # decision row would dominate the database and go stale against the job itself.
    subject_type: Mapped[str] = mapped_column(String(40), default="")
    # Same reasoning: `for_subject()` always supplies both halves.
    subject_id: Mapped[str | None] = mapped_column(String(64))

    decision: Mapped[str] = mapped_column(String(120))
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    # The facts, scores and thresholds behind it, and which tools were consulted.
    evidence: Mapped[dict] = mapped_column(JSONType, default=dict)
    tools_used: Mapped[list] = mapped_column(JSONType, default=list)
    inputs: Mapped[dict] = mapped_column(JSONType, default=dict)
    outcome: Mapped[str] = mapped_column(Text, default="")

    # Which prompt produced it, when a model was involved. Spec §48 asks that a result
    # be traceable to the prompt version that made it.
    model: Mapped[str | None] = mapped_column(String(120))
    prompt_version: Mapped[str | None] = mapped_column(String(60))

    at: Mapped[datetime] = mapped_column(UTCDateTime, default=_now, index=True)

    __table_args__ = (
        Index("ix_agent_decision_agent_at", "agent", "at"),
        Index("ix_agent_decision_subject", "subject_type", "subject_id"),
    )
