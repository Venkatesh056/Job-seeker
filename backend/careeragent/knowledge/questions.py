"""The traversals worth asking by name — addendum §18.

A graph earns its place when a question that takes five joins becomes one walk. These are
those questions. Each one was chosen because a person actually asks it while job hunting and
the relational answer is awkward enough that nobody writes it twice.

Every function returns plain rows rather than a `Graph`. A graph is for looking at; these
are for deciding with, and a table is the honest shape for a decision.

**Nothing here writes.** These inform the supervisor and the dashboard; acting on them is a
separate, explicit step somewhere a person can see it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..resume.taxonomy import mentions
from ..storage.models import (
    Application,
    ApplicationStatus,
    Company,
    Fact,
    Job,
    JobScore,
    Outreach,
)

log = get_logger("knowledge.questions")

DEFAULT_LIMIT = 20

# Below this many submitted applications a "rate" is noise wearing a percentage sign.
# One response out of one application is not a 100% response rate, and reporting it as
# one would make the weakest evidence in the system look like the strongest.
_ENOUGH_TO_RATE = 5


async def _candidate_skills(session: AsyncSession, candidate_id: str) -> list[tuple[str, int]]:
    rows = (
        await session.execute(
            select(Fact.label, Fact.strength)
            .where(Fact.candidate_id == candidate_id, Fact.kind == "skill")
            .order_by(Fact.strength.desc())
        )
    ).all()
    return [(str(label).lower(), int(strength or 3)) for label, strength in rows if label]


# ─── "Who should I be talking to?" ──────────────────────────────────────────


async def untouched_companies(
    session: AsyncSession, candidate_id: str, *, limit: int = DEFAULT_LIMIT
) -> list[dict[str, Any]]:
    """Companies with open roles I score well on, that I have never contacted or applied to.

    The single most actionable question in the whole system, and the one the relational
    schema makes least convenient: it is an anti-join across applications *and* outreach,
    ranked by a score that lives in a third table.
    """
    applied = select(Job.company).join(
        Application,
        (Application.job_id == Job.id) & (Application.candidate_id == candidate_id),
    )
    contacted = select(Company.name).join(Outreach, Outreach.company_id == Company.id)

    rows = (
        await session.execute(
            select(
                Job.company,
                func.count(Job.id).label("openings"),
                func.max(JobScore.final_score).label("best_score"),
            )
            .join(JobScore, (JobScore.job_id == Job.id) & (JobScore.candidate_id == candidate_id))
            .where(
                Job.company.isnot(None),
                Job.company.notin_(applied),
                Job.company.notin_(contacted),
            )
            .group_by(Job.company)
            .order_by(func.max(JobScore.final_score).desc())
            .limit(limit)
        )
    ).all()

    return [
        {
            "company": company,
            "open_roles": int(openings or 0),
            "best_score": round(float(best or 0.0), 3),
            "why": f"{openings} open role(s), best match {float(best or 0):.0%}, never contacted",
        }
        for company, openings, best in rows
    ]


# ─── "What should I learn?" ─────────────────────────────────────────────────


async def bridging_skills(
    session: AsyncSession,
    candidate_id: str,
    *,
    limit: int = 10,
    min_score: float = 0.4,
) -> list[dict[str, Any]]:
    """Skills that appear in jobs I nearly match but do not have.

    A graph question in shape: walk from me → jobs I am close to → the skills those jobs
    want → subtract what I already have. What is left is ranked by how many doors it opens.

    Restricted to jobs already scoring above `min_score`, which is what makes the answer
    useful rather than a list of everything the market wants. A skill that would connect me
    to twenty jobs I have no other claim on is not a bridge; it is a career change.
    """
    from ..resume.taxonomy import ALIAS_TO_CANONICAL, AMBIGUOUS, SKILLS

    mine = {label for label, _ in await _candidate_skills(session, candidate_id)}
    mine |= {ALIAS_TO_CANONICAL.get(s, s) for s in mine}

    rows = (
        await session.execute(
            select(Job.id, Job.title, Job.company, Job.description, JobScore.final_score)
            .join(JobScore, (JobScore.job_id == Job.id) & (JobScore.candidate_id == candidate_id))
            .where(JobScore.final_score >= min_score)
            .order_by(JobScore.final_score.desc())
            .limit(200)
        )
    ).all()

    counts: dict[str, int] = {}
    examples: dict[str, list[str]] = {}
    for _job_id, title, company, description, _score in rows:
        haystack = f"{title} {description or ''}".lower()
        seen: set[str] = set()
        for skill in SKILLS:
            canonical = ALIAS_TO_CANONICAL.get(skill, skill)
            if canonical in mine or canonical in seen:
                continue
            # Names that are also ordinary English — "go", "c", "excel", "git", "lean" —
            # are excluded from a *recommendation*. Even with word boundaries, "we want you
            # to go beyond" counts as a Go requirement, and telling a data scientist to
            # learn C on the strength of that is worse than saying nothing. The taxonomy
            # already knows which 37 names these are.
            if canonical in AMBIGUOUS:
                continue
            # Boundary-aware. Substring matching put "c", "r", "go" and "pos" at the
            # top of this list, making it a list of letters rather than of skills.
            if mentions(haystack, skill):
                seen.add(canonical)
                counts[canonical] = counts.get(canonical, 0) + 1
                if len(examples.setdefault(canonical, [])) < 3:
                    examples[canonical].append(f"{title} at {company}")

    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    return [
        {
            "skill": skill,
            "would_connect_to": count,
            "examples": examples.get(skill, []),
            "why": f"wanted by {count} role(s) you already partly match",
            "ambiguous": False,
        }
        for skill, count in ranked
    ]


# ─── "What have I already done here?" ───────────────────────────────────────


async def history_with(session: AsyncSession, candidate_id: str, company: str) -> dict[str, Any]:
    """Everything this candidate has done involving one company.

    Applications, what was said, which resume went, and whether anyone was contacted — the
    thing you want before writing to someone, and four tables away in SQL.
    """
    rows = (
        await session.execute(
            select(Application, Job)
            .join(Job, Job.id == Application.job_id)
            .where(
                Application.candidate_id == candidate_id,
                Job.company.ilike(f"%{company.strip()}%"),
            )
            .order_by(Application.created_at.desc())
        )
    ).all()

    company_row = (
        (await session.execute(select(Company).where(Company.name.ilike(f"%{company.strip()}%"))))
        .scalars()
        .first()
    )

    outreach: list[dict[str, Any]] = []
    if company_row:
        sent = (
            (
                await session.execute(
                    select(Outreach)
                    .where(Outreach.company_id == company_row.id)
                    .order_by(Outreach.created_at.desc())
                )
            )
            .scalars()
            .all()
        )
        outreach = [
            {"status": str(o.status), "subject": o.subject, "at": o.created_at.isoformat()}
            for o in sent
        ]

    return {
        "company": company,
        "applications": [
            {
                "application_id": app.id,
                "title": job.title,
                "status": str(app.status),
                "submitted": app.submitted_at.isoformat() if app.submitted_at else None,
                "resume_render_id": app.resume_render_id,
                "score": round(app.final_score, 3) if app.final_score else None,
            }
            for app, job in rows
        ],
        "outreach": outreach,
        "researched": bool(company_row and (company_row.description or company_row.tech_focus)),
        # The thing you actually wanted to know.
        "has_any_contact": bool(rows or outreach),
    }


# ─── "Which of my skills is actually earning its place?" ────────────────────


async def skill_reach(
    session: AsyncSession, candidate_id: str, *, limit: int = 25
) -> list[dict[str, Any]]:
    """How many currently-known jobs each of my skills connects me to.

    Useful in both directions: a skill with high reach is what to lead a resume with, and a
    skill with none is either mis-named in the Fact Bank or genuinely not wanted in the
    market being searched. Both are worth seeing.
    """
    skills = await _candidate_skills(session, candidate_id)
    if not skills:
        return []

    jobs = (await session.execute(select(Job.title, Job.description).limit(2000))).all()
    corpus = [f"{title} {description or ''}".lower() for title, description in jobs]

    from ..resume.taxonomy import AMBIGUOUS

    out: list[dict[str, Any]] = []
    for label, strength in skills:
        # "excel" matched "excellent" and "git" matched "digital", which is why those
        # two topped this list ahead of Python for a data-science candidate.
        reach = sum(1 for text in corpus if mentions(text, label))
        out.append(
            {
                "skill": label,
                "reach": reach,
                "share": round(reach / len(corpus), 4) if corpus else 0.0,
                "strength": strength,
                # An unreached skill is a finding, not a blank. It usually means the Fact
                # Bank spells it differently from the market.
                # Kept rather than hidden, because this is the candidate's own skill list
                # and an inflated number they cannot see the reason for is worse than one
                # labelled unreliable.
                "ambiguous": label in AMBIGUOUS,
                "note": (
                    "no job in view mentions this, check how it is named"
                    if not reach
                    else "also an ordinary English word, so this count runs high"
                    if label in AMBIGUOUS
                    else ""
                ),
            }
        )
    out.sort(key=lambda row: int(row["reach"]), reverse=True)
    return out[:limit]


# ─── "Where do my strongest skills actually lead?" ──────────────────────────


async def companies_for_my_strongest_skills(
    session: AsyncSession,
    candidate_id: str,
    *,
    top_skills: int = 8,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """Companies whose open roles ask for what I am strongest at, with those roles attached.

    Two hops that are really one question — me → my strongest facts → jobs that name them →
    the companies posting them. Splitting it into "which companies" and "which openings"
    would answer twice from the same walk and let the two answers drift apart, so the
    openings come back inside the company rather than beside it.

    Ranked by how many of my strongest skills a company asks for, not by how many roles it
    has open. A company with one role wanting four of my skills is a better lead than one
    with nine roles wanting one.
    """
    from ..resume.taxonomy import ALIAS_TO_CANONICAL, AMBIGUOUS

    skills = await _candidate_skills(session, candidate_id)
    # Ordinary English words are excluded from the *matching* here for the same reason
    # bridging_skills excludes them from recommendations: "go" and "excel" match half the
    # corpus, and a company list built on them is a list of nothing.
    strongest = [s for s, _ in skills if ALIAS_TO_CANONICAL.get(s, s) not in AMBIGUOUS][:top_skills]
    if not strongest:
        return []

    rows = (
        await session.execute(
            select(Job.company, Job.id, Job.title, Job.description, JobScore.final_score)
            .join(JobScore, (JobScore.job_id == Job.id) & (JobScore.candidate_id == candidate_id))
            .where(Job.status == "open", Job.company.isnot(None))
            .order_by(JobScore.final_score.desc())
            .limit(600)
        )
    ).all()

    by_company: dict[str, dict[str, Any]] = {}
    for company, job_id, title, description, score in rows:
        haystack = f"{title} {description or ''}".lower()
        matched = [skill for skill in strongest if mentions(haystack, skill)]
        if not matched:
            continue
        entry = by_company.setdefault(
            str(company),
            {"company": company, "skills": set(), "openings": [], "best_score": 0.0},
        )
        entry["skills"].update(matched)
        entry["best_score"] = max(float(entry["best_score"]), float(score or 0.0))
        if len(entry["openings"]) < 5:
            entry["openings"].append(
                {
                    "job_id": job_id,
                    "title": title,
                    "score": round(float(score or 0.0), 3),
                    "asks_for": matched,
                }
            )

    out = []
    for entry in by_company.values():
        matched_skills = sorted(entry["skills"])
        out.append(
            {
                "company": entry["company"],
                "skills_matched": matched_skills,
                "skill_count": len(matched_skills),
                "openings": entry["openings"],
                "open_roles": len(entry["openings"]),
                "best_score": round(float(entry["best_score"]), 3),
                "why": (
                    f"asks for {len(matched_skills)} of your strongest skills "
                    f"({', '.join(matched_skills[:3])})"
                ),
            }
        )

    out.sort(key=lambda row: (row["skill_count"], row["best_score"]), reverse=True)
    return out[:limit]


# ─── "Who at those companies has not heard from me?" ────────────────────────


async def uncontacted_at_matched_companies(
    session: AsyncSession,
    candidate_id: str,
    *,
    min_job_score: float = 0.45,
    min_relevance: int = 45,
    per_company_cap: int = 3,
    limit: int = DEFAULT_LIMIT,
) -> dict[str, Any]:
    """People worth writing to at companies whose jobs I match, whom I have not written to.

    Three joins nobody writes twice: which companies have openings I score well on, who
    works there that matters for the role I am chasing, and which of those have already
    heard from me. The answer is only interesting because of the *gap* — somebody relevant
    and reachable who has heard nothing.

    Suppressed people are dropped rather than listed with a warning. Somebody who asked not
    to be contacted is not a lead with a caveat.
    """
    from ..outreach import suppression
    from ..storage.models import OutreachContact, PersonRecord

    matched = (
        await session.execute(
            select(Job.company_id, func.max(JobScore.final_score).label("best"))
            .join(JobScore, (JobScore.job_id == Job.id) & (JobScore.candidate_id == candidate_id))
            .where(
                JobScore.final_score >= min_job_score,
                Job.status == "open",
                Job.company_id.isnot(None),
            )
            .group_by(Job.company_id)
        )
    ).all()
    best_by_company = {str(cid): float(best or 0.0) for cid, best in matched if cid}
    if not best_by_company:
        return {
            "people": [],
            "count": 0,
            "reachable": 0,
            "note": (
                f"No company has an open role you score above {min_job_score:.0%} on, so "
                "there is nobody to write to about one."
            ),
        }

    # Everybody already written to, by address. Drafts and cancelled messages do not
    # count: the recipient has experienced nothing of them.
    contacted = {
        str(address).strip().lower()
        for (address,) in (
            await session.execute(
                select(OutreachContact.address)
                .join(Outreach, Outreach.contact_id == OutreachContact.id)
                .where(
                    Outreach.candidate_id == candidate_id,
                    Outreach.status.notin_(["drafted", "awaiting_approval", "cancelled"]),
                )
            )
        ).all()
        if address
    }
    contacted_names = {
        str(name).strip().lower()
        for (name,) in (
            await session.execute(
                select(OutreachContact.name)
                .join(Outreach, Outreach.contact_id == OutreachContact.id)
                .where(
                    Outreach.candidate_id == candidate_id,
                    Outreach.status.notin_(["drafted", "awaiting_approval", "cancelled"]),
                )
            )
        ).all()
        if name
    }

    rows = (
        (
            await session.execute(
                select(PersonRecord)
                .where(
                    PersonRecord.company_id.in_(list(best_by_company)),
                    PersonRecord.relevance >= min_relevance,
                )
                .order_by(PersonRecord.relevance.desc())
                .limit(limit * 8)
            )
        )
        .scalars()
        .all()
    )

    names = {
        str(cid): name
        for cid, name in (
            await session.execute(
                select(Company.id, Company.name).where(Company.id.in_(list(best_by_company)))
            )
        ).all()
    }

    people: list[dict[str, Any]] = []
    suppressed = 0
    # At most a few per company. One well-crawled employer with thirty-nine staff would
    # otherwise fill the whole list, and a to-do list that says "write to twelve people at
    # Karya" is worse advice than one naming three each at four companies — the twelve
    # arrive as a campaign, which is the thing this system is supposed not to do.
    per_company: dict[str, int] = {}
    for row in rows:
        if per_company.get(row.company_id, 0) >= per_company_cap:
            continue
        if (row.email or "").strip().lower() in contacted:
            continue
        if (row.name or "").strip().lower() in contacted_names:
            continue
        if await suppression.is_suppressed(session, email=row.email or "", person_id=row.id):
            suppressed += 1
            continue
        per_company[row.company_id] = per_company.get(row.company_id, 0) + 1
        people.append(
            {
                "person_id": row.id,
                "name": row.name,
                "title": row.title,
                "company": names.get(row.company_id, ""),
                "company_id": row.company_id,
                "relevance": row.relevance,
                "hiring_authority": bool(row.hiring_authority),
                "best_job_score": round(best_by_company.get(row.company_id, 0.0), 3),
                # Reachable and contactable are different claims. A LinkedIn profile means
                # a person can be reached by hand; only a verified published address means
                # the system itself can send anything.
                "reachable": bool(row.email or row.linkedin_url),
                "channel": row.recommended_channel,
                "why": (row.relevance_reasons or [""])[0],
            }
        )
        if len(people) >= limit:
            break

    reachable = sum(1 for p in people if p["reachable"])
    return {
        "people": people,
        "count": len(people),
        "reachable": reachable,
        "suppressed_skipped": suppressed,
        "companies": len(per_company),
        "note": (
            f"{len(people)} people across {len(per_company)} companies with open roles you "
            f"match, none of whom have heard from you. {reachable} have a channel already. "
            f"At most {per_company_cap} per company, best first."
        )
        if people
        else (
            "Either everyone relevant at your matched companies has already been contacted, "
            "or no people have been discovered at them yet."
        ),
    }


# ─── "What actually worked?" ────────────────────────────────────────────────


async def applications_that_went_somewhere(
    session: AsyncSession, candidate_id: str, *, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """Applications that drew a response, and what they had in common.

    The list matters less than its shape. A response rate by route — applied directly,
    applied after writing to somebody — is the only feedback a job hunt gets, and it arrives
    too slowly to notice without counting.

    A rejection counts as a response. It is not the outcome anybody wants, but it is
    evidence the application was read, which is exactly what silence is not.
    """
    from ..applications.states import RESPONDED_STATES, SUBMITTED_STATES

    rows = (
        (
            await session.execute(
                select(Application)
                .where(Application.candidate_id == candidate_id)
                .order_by(Application.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {
            "applications": [],
            "submitted": 0,
            "responded": 0,
            "interviews": 0,
            "response_rate": None,
            "note": "Nothing has been applied to yet, so there is nothing to learn from.",
        }

    responded = [r for r in rows if r.status in RESPONDED_STATES]
    interviewed = [
        r for r in rows if r.status in {ApplicationStatus.INTERVIEW, ApplicationStatus.OFFER}
    ]
    submitted = [r for r in rows if r.submitted_at or r.status in SUBMITTED_STATES]

    titles = {
        str(job_id): (title, company)
        for job_id, title, company in (
            await session.execute(
                select(Job.id, Job.title, Job.company).where(
                    Job.id.in_([r.job_id for r in responded] or [""])
                )
            )
        ).all()
    }

    # Was somebody written to at this company before the application went in? The
    # question the whole outreach half of the system exists to answer.
    contacted_companies = {
        str(name).strip().lower()
        for (name,) in (
            await session.execute(
                select(Company.name)
                .join(Outreach, Outreach.company_id == Company.id)
                .where(
                    Outreach.candidate_id == candidate_id,
                    Outreach.sent_at.isnot(None),
                )
            )
        ).all()
        if name
    }

    out = []
    for row in responded[:limit]:
        title, company = titles.get(str(row.job_id), ("", ""))
        out.append(
            {
                "application_id": row.id,
                "job": title,
                "company": company,
                "status": str(row.status),
                "strategy": row.strategy,
                "resume_render_id": row.resume_render_id,
                "score": round(row.final_score, 3) if row.final_score else None,
                "submitted_at": row.submitted_at.isoformat() if row.submitted_at else None,
                "outreach_first": str(company).strip().lower() in contacted_companies,
            }
        )

    n = len(submitted)
    rate = round(len(responded) / n, 3) if n >= _ENOUGH_TO_RATE else None
    return {
        "applications": out,
        "submitted": n,
        "responded": len(responded),
        "interviews": len(interviewed),
        "response_rate": rate,
        "note": (
            f"{len(responded)} of {n} submitted applications drew a response, "
            f"{len(interviewed)} of them an interview or better."
            if n
            else "Nothing has been submitted yet, so there is no rate to report."
        )
        + (
            ""
            if rate is not None or not n
            else f" Too few to quote a rate — {_ENOUGH_TO_RATE} would be the minimum."
        ),
    }


async def resume_versions_by_outcome(
    session: AsyncSession, candidate_id: str, *, limit: int = DEFAULT_LIMIT
) -> dict[str, Any]:
    """Which tailored resumes drew responses.

    Rates are withheld below a threshold rather than rounded, because "100% response rate"
    across a single application is a number that means nothing and reads as though it means
    everything. Under the threshold the counts are shown and the rate is `None` — the
    honest shape of not knowing yet.
    """
    from ..applications.states import RESPONDED_STATES, SUBMITTED_STATES

    rows = (
        (
            await session.execute(
                select(Application).where(
                    Application.candidate_id == candidate_id,
                    Application.resume_render_id.isnot(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return {
            "versions": [],
            "count": 0,
            "comparable": 0,
            "note": "No application has a tailored resume attached yet.",
        }

    by_version: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = str(row.resume_render_id)
        entry = by_version.setdefault(
            key,
            {
                "resume_render_id": key,
                "used": 0,
                "submitted": 0,
                "responded": 0,
                "interviews": 0,
                "companies": [],
            },
        )
        entry["used"] += 1
        if row.submitted_at or row.status in SUBMITTED_STATES:
            entry["submitted"] += 1
        if row.status in RESPONDED_STATES:
            entry["responded"] += 1
        if row.status in {ApplicationStatus.INTERVIEW, ApplicationStatus.OFFER}:
            entry["interviews"] += 1

    versions: list[dict[str, Any]] = []
    for entry in by_version.values():
        enough = int(entry["submitted"]) >= _ENOUGH_TO_RATE
        entry["rate"] = (
            round(int(entry["responded"]) / int(entry["submitted"]), 3)
            if enough and entry["submitted"]
            else None
        )
        entry["note"] = (
            f"{entry['responded']} of {entry['submitted']} drew a response"
            if enough
            else f"used {entry['submitted']} time(s) — too few for a rate to mean anything"
        )
        versions.append(entry)

    # Unrated versions sort below rated ones rather than above them. A version with no
    # rate is not a version with a perfect one.
    versions.sort(
        key=lambda v: (v["rate"] if v["rate"] is not None else -1.0, v["responded"], v["used"]),
        reverse=True,
    )
    comparable = sum(1 for v in versions if v["rate"] is not None)
    return {
        "versions": versions[:limit],
        "count": len(versions),
        "comparable": comparable,
        "note": (
            f"{comparable} of {len(versions)} resume version(s) have been used enough times "
            f"({_ENOUGH_TO_RATE}+) for a response rate to mean anything."
        ),
    }
