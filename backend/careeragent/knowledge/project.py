"""Building the graph from the tables that already hold the truth.

Every function here reads and returns; none writes. That is the whole premise — the graph
cannot go stale against the database because it *is* the database, arranged differently.

**Everything is scoped.** `around(node_id)` walks outward a bounded number of hops from one
node, which is both what keeps 1,169 jobs from rendering as a grey disc and how anyone
actually reads a graph view: you look at a thing, then at what it touches.

The expensive join is skills. A job's required skills are not a column — they are matched
out of its description by the same code the scorer uses, so the graph agrees with the
ranking rather than offering a second opinion about what a job wants.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..storage.models import Application, ATSBoard, Company, Fact, Job, JobScore
from .model import (
    Edge,
    EdgeKind,
    Graph,
    Node,
    NodeKind,
    company_node,
    department_node,
    location_node,
    person_node,
    role_node,
    skill_node,
    slug,
)

log = get_logger("knowledge.project")

# Per-node fan-out. A company with 300 openings would otherwise bury everything else on
# screen, and the twentieth job teaches nothing the first five did not.
FAN_OUT = 12


def _job_node(job: Job, score: float | None = None) -> Node:
    attrs: dict[str, Any] = {"company": job.company, "ats": job.ats or None}
    if job.posted_date:
        attrs["posted"] = str(job.posted_date)
    if score is not None:
        attrs["score"] = round(score, 3)
    if job.location:
        attrs["location"] = job.location
    return Node(NodeKind.JOB, job.id, job.title, attrs)


def _application_node(app: Application, title: str = "") -> Node:
    return Node(
        NodeKind.APPLICATION,
        app.id,
        title or f"application {app.id[:8]}",
        {
            "status": str(app.status),
            "submitted": app.submitted_at.isoformat() if app.submitted_at else None,
            "score": round(app.final_score, 3) if app.final_score else None,
        },
    )


async def _job_skills(session: AsyncSession, job: Job, candidate_skills: set[str]) -> list[str]:
    """Which of the candidate's skills this job's text actually asks for.

    Deliberately *not* every skill in the taxonomy. A graph edge from a job to a skill the
    candidate does not have is true but useless here — the question this graph answers is
    how the candidate connects to the work, and an unheld skill is a gap, which the gap
    analysis already reports.
    """
    from ..resume.taxonomy import mentioned_skills

    # The same matcher the scorer uses. Two subsystems disagreeing about what a job
    # asks for would be worse than either being wrong, because the disagreement is
    # invisible to anyone reading either one.
    return mentioned_skills(f"{job.title} {job.description or ''}", candidate_skills)


# ─── The candidate's own neighbourhood ──────────────────────────────────────


async def candidate_graph(
    session: AsyncSession,
    candidate_id: str,
    *,
    max_nodes: int = 400,
    job_limit: int = 40,
) -> Graph:
    """The candidate, their skills, and what those skills reach.

    The default view. Starts from the person rather than from the data, which is the only
    starting point that makes the layout mean anything.
    """
    graph = Graph(max_nodes=max_nodes)

    row = await session.get(
        __import__("careeragent.storage.models", fromlist=["Candidate"]).Candidate, candidate_id
    )
    if row is None:
        return graph

    me = graph.add(Node(NodeKind.CANDIDATE, candidate_id, row.name or "you", {"root": True}))

    # ── Skills, with the facts that evidence them ──────────────────────────
    facts = (
        (
            await session.execute(
                select(Fact).where(Fact.candidate_id == candidate_id).order_by(Fact.strength.desc())
            )
        )
        .scalars()
        .all()
    )

    skill_keys: dict[str, str] = {}
    for fact in facts:
        if fact.kind != "skill":
            continue
        node = skill_node(fact.label)
        skill_keys[node.label] = graph.add(node)
        graph.link(
            Edge(
                EdgeKind.HAS_SKILL,
                me,
                node.id,
                weight=float(fact.strength or 3) / 5.0,
                attrs={"verified": bool(fact.verified)},
            )
        )

    # Non-skill facts hang off the candidate as evidence. They are what makes a claim
    # traceable, and a graph that showed skills without them would show assertions.
    for fact in facts:
        if fact.kind == "skill":
            continue
        node = Node(
            NodeKind.FACT,
            fact.id,
            fact.label[:80],
            {"kind": fact.kind, "verified": bool(fact.verified)},
        )
        graph.add(node)
        graph.link(Edge(EdgeKind.EVIDENCED_BY, me, node.id))

    # ── The jobs those skills reach ────────────────────────────────────────
    scored = (
        await session.execute(
            select(Job, JobScore.final_score)
            .join(JobScore, JobScore.job_id == Job.id)
            .where(JobScore.candidate_id == candidate_id)
            .order_by(JobScore.final_score.desc())
            .limit(job_limit)
        )
    ).all()

    candidate_skills = set(skill_keys)
    for job, score in scored:
        job_id = graph.add(_job_node(job, score))
        _link_job(graph, job, job_id)
        for label in await _job_skills(session, job, candidate_skills):
            if target := skill_keys.get(label):
                graph.link(Edge(EdgeKind.REQUIRES_SKILL, job_id, target))

    await _add_applications(session, graph, candidate_id)
    await _add_outreach(session, graph, candidate_id, me)
    log.debug("graph_projected", root="candidate", nodes=len(graph), edges=len(graph.edges))
    return graph


def _link_job(graph: Graph, job: Job, job_id: str) -> None:
    """The structural edges every job has: its employer, its ATS, its city."""
    if job.company:
        company = company_node(job.company)
        graph.add(company)
        graph.link(Edge(EdgeKind.POSTED_BY, job_id, company.id))
        if job.ats:
            ats = Node(NodeKind.ATS, slug(job.ats), job.ats)
            graph.add(ats)
            graph.link(Edge(EdgeKind.USES_ATS, company.id, ats.id))
    if job.location:
        place = location_node(job.location)
        graph.add(place)
        graph.link(Edge(EdgeKind.LOCATED_IN, job_id, place.id))


async def _add_applications(session: AsyncSession, graph: Graph, candidate_id: str) -> None:
    """Applications, linked to the jobs they are for.

    Included even when the job is not otherwise in view: an application is a thing the user
    did, and a graph of their career that omitted their own actions would be strange.
    """
    rows = (
        await session.execute(
            select(Application, Job)
            .join(Job, Job.id == Application.job_id)
            .where(Application.candidate_id == candidate_id)
            .order_by(Application.created_at.desc())
            .limit(60)
        )
    ).all()

    for app, job in rows:
        job_id = graph.add(_job_node(job))
        _link_job(graph, job, job_id)
        app_id = graph.add(_application_node(app, job.title))
        graph.link(
            Edge(
                EdgeKind.APPLIED_TO,
                app_id,
                job_id,
                attrs={"status": str(app.status)},
            )
        )


# ─── Any node's neighbourhood ───────────────────────────────────────────────


async def around(
    session: AsyncSession,
    node_id: str,
    candidate_id: str,
    *,
    depth: int = 1,
    max_nodes: int = 200,
) -> Graph:
    """Walk outward from one node. The Obsidian-style local view.

    Depth is capped at two on purpose. Three hops from a skill reaches most of the database,
    which is a picture of everything and therefore of nothing.
    """
    graph = Graph(max_nodes=max_nodes)
    kind, _, key = node_id.partition(":")
    depth = max(1, min(depth, 2))

    if kind == NodeKind.COMPANY.value:
        await _company_neighbourhood(session, graph, key, candidate_id, depth)
    elif kind == NodeKind.JOB.value:
        await _job_neighbourhood(session, graph, key, candidate_id, depth)
    elif kind == NodeKind.SKILL.value:
        await _skill_neighbourhood(session, graph, key, candidate_id, depth)
    elif kind == NodeKind.CANDIDATE.value:
        return await candidate_graph(session, key, max_nodes=max_nodes)
    else:
        log.debug("graph_unsupported_root", kind=kind)

    return graph


async def _company_neighbourhood(
    session: AsyncSession, graph: Graph, key: str, candidate_id: str, depth: int
) -> None:
    """A company, its openings, its ATS, and what we have done about it."""
    row = (
        await session.execute(
            select(Company).where(Company.normalized_name == key.replace("-", " "))
        )
    ).scalar_one_or_none()
    name = row.name if row else key.replace("-", " ")

    company = company_node(
        name,
        domain=row.domain if row else None,
        fit_score=row.fit_score if row else None,
        root=True,
    )
    company_id = graph.add(company)

    if row:
        board = (
            await session.execute(select(ATSBoard).where(ATSBoard.company_id == row.id).limit(1))
        ).scalar_one_or_none()
        if board:
            ats = Node(NodeKind.ATS, slug(board.provider), board.provider, {"slug": board.slug})
            graph.add(ats)
            graph.link(Edge(EdgeKind.USES_ATS, company_id, ats.id))

    jobs = (
        (
            await session.execute(
                select(Job)
                .where(Job.company.ilike(f"%{name}%"))
                .order_by(Job.posted_date.desc())
                .limit(FAN_OUT)
            )
        )
        .scalars()
        .all()
    )
    for job in jobs:
        job_id = graph.add(_job_node(job))
        graph.link(Edge(EdgeKind.POSTED_BY, job_id, company_id))
        if depth > 1:
            await _attach_candidate_skills(session, graph, job, job_id, candidate_id)

    await _attach_applications_for_jobs(session, graph, [j.id for j in jobs], candidate_id)
    if row is not None:
        await _add_people(session, graph, row.id, company_id)


async def _job_neighbourhood(
    session: AsyncSession, graph: Graph, key: str, candidate_id: str, depth: int
) -> None:
    """One opening: who posted it, where, which of my skills it wants, what I did about it."""
    job = await session.get(Job, key)
    if job is None:
        return

    score = (
        await session.execute(
            select(JobScore.final_score).where(
                JobScore.job_id == job.id, JobScore.candidate_id == candidate_id
            )
        )
    ).scalar_one_or_none()

    job_id = graph.add(_job_node(job, score))
    # Marked as the subject so a viewer can centre on it. Rebuilt from the node's own attrs
    # rather than from scratch, so adding `root` never silently drops the score or location
    # that `_job_node` just put there.
    if (placed := graph.get(job_id)) is not None:
        graph._nodes[job_id] = Node(NodeKind.JOB, job.id, job.title, {**placed.attrs, "root": True})
    _link_job(graph, job, job_id)
    await _attach_candidate_skills(session, graph, job, job_id, candidate_id)
    await _attach_applications_for_jobs(session, graph, [job.id], candidate_id)

    if depth > 1 and job.company:
        siblings = (
            (
                await session.execute(
                    select(Job)
                    .where(Job.company == job.company, Job.id != job.id)
                    .order_by(Job.posted_date.desc())
                    .limit(FAN_OUT)
                )
            )
            .scalars()
            .all()
        )
        company = company_node(job.company)
        for sibling in siblings:
            sibling_id = graph.add(_job_node(sibling))
            graph.link(Edge(EdgeKind.POSTED_BY, sibling_id, company.id))


async def _skill_neighbourhood(
    session: AsyncSession, graph: Graph, key: str, candidate_id: str, depth: int
) -> None:
    """One skill, and the open jobs that ask for it.

    The question behind this view is "what does knowing this get me", so the jobs come back
    ranked by their score rather than by date.
    """
    label = key.replace("-", " ")
    skill = skill_node(label)
    graph.add(Node(skill.kind, skill.key, skill.label, {"root": True}))

    rows = (
        await session.execute(
            select(Job, JobScore.final_score)
            .outerjoin(
                JobScore,
                (JobScore.job_id == Job.id) & (JobScore.candidate_id == candidate_id),
            )
            .where(Job.description.ilike(f"%{label}%") | Job.title.ilike(f"%{label}%"))
            .order_by(JobScore.final_score.desc().nullslast())
            .limit(FAN_OUT * 2)
        )
    ).all()

    for job, score in rows:
        job_id = graph.add(_job_node(job, score))
        graph.link(Edge(EdgeKind.REQUIRES_SKILL, job_id, skill.id))
        if depth > 1:
            _link_job(graph, job, job_id)


async def _attach_candidate_skills(
    session: AsyncSession, graph: Graph, job: Job, job_id: str, candidate_id: str
) -> None:
    """Edges from a job to the candidate's skills it actually mentions."""
    labels = (
        (
            await session.execute(
                select(Fact.label).where(Fact.candidate_id == candidate_id, Fact.kind == "skill")
            )
        )
        .scalars()
        .all()
    )
    from ..resume.taxonomy import mentioned_skills

    text = f"{job.title} {job.description or ''}"
    for label in mentioned_skills(text, [str(x) for x in labels if x]):
        node = skill_node(label)
        graph.add(node)
        graph.link(Edge(EdgeKind.REQUIRES_SKILL, job_id, node.id))


async def _attach_applications_for_jobs(
    session: AsyncSession, graph: Graph, job_ids: list[str], candidate_id: str
) -> None:
    if not job_ids:
        return
    rows = (
        (
            await session.execute(
                select(Application).where(
                    Application.job_id.in_(job_ids),
                    Application.candidate_id == candidate_id,
                )
            )
        )
        .scalars()
        .all()
    )
    for app in rows:
        app_id = graph.add(_application_node(app))
        graph.link(
            Edge(
                EdgeKind.APPLIED_TO, app_id, f"job:{app.job_id}", attrs={"status": str(app.status)}
            )
        )


# ─── Summary ────────────────────────────────────────────────────────────────


async def stats(session: AsyncSession, candidate_id: str) -> dict[str, Any]:
    """How big the graph would be, without building it.

    Cheap counts, so a client can decide whether to ask for a view rather than requesting
    one and discovering it was truncated.
    """

    async def count(model: Any, *where: Any) -> int:
        return int(
            (
                await session.execute(select(func.count()).select_from(model).where(*where))
            ).scalar_one()
            or 0
        )

    return {
        "skills": await count(Fact, Fact.candidate_id == candidate_id, Fact.kind == "skill"),
        "facts": await count(Fact, Fact.candidate_id == candidate_id),
        "jobs": await count(Job),
        "scored_jobs": await count(JobScore, JobScore.candidate_id == candidate_id),
        "companies": await count(Company),
        "applications": await count(Application, Application.candidate_id == candidate_id),
    }


# ─── People, and what happens after you write to them ───────────────────────


async def _add_people(
    session: AsyncSession, graph: Graph, company_id: str, company_key: str, limit: int = 8
) -> None:
    """The people at one company, best first.

    Capped like everything else here. A company with forty names on its leadership page
    would bury the jobs, and the fortieth person teaches nothing the first eight did not —
    the ranking has already decided which of them matter.
    """
    from ..storage.models import PersonRecord

    rows = (
        (
            await session.execute(
                select(PersonRecord)
                .where(PersonRecord.company_id == company_id)
                .order_by(PersonRecord.relevance.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )

    for row in rows:
        node = person_node(
            row.name,
            row.id,
            title=row.title or "",
            relevance=row.relevance,
            reachable=bool(row.email or row.linkedin_url),
            contactable=bool(row.email and row.email_verification == "verified"),
        )
        graph.add(node)
        graph.link(Edge(EdgeKind.HAS_PERSON, company_key, node.id))

        if row.title:
            role = role_node(row.title)
            graph.add(role)
            graph.link(Edge(EdgeKind.HAS_ROLE, node.id, role.id))

        if row.function:
            department = department_node(row.function)
            graph.add(department)
            graph.link(Edge(EdgeKind.WORKS_IN, node.id, department.id))


async def _add_outreach(
    session: AsyncSession, graph: Graph, candidate_id: str, candidate_key: str
) -> None:
    """Every message sent, and what came of it.

    Only messages that actually reached somebody. A draft is a decision the sender has not
    finished making, and putting it in the graph would let "who have I contacted" answer
    with people who have heard nothing.
    """
    from ..storage.models import Outreach, OutreachContact, PersonRecord

    rows = (
        (
            await session.execute(
                select(Outreach)
                .where(Outreach.candidate_id == candidate_id)
                .order_by(Outreach.created_at.desc())
                .limit(FAN_OUT * 2)
            )
        )
        .scalars()
        .all()
    )
    if not rows:
        return

    contact_ids = {r.contact_id for r in rows if r.contact_id}
    contacts = {
        c.id: c
        for c in (
            (
                await session.execute(
                    select(OutreachContact).where(OutreachContact.id.in_(contact_ids or {""}))
                )
            )
            .scalars()
            .all()
        )
    }

    for row in rows:
        if row.status in {"drafted", "awaiting_approval", "cancelled"}:
            continue

        node = Node(
            NodeKind.OUTREACH,
            row.id,
            row.subject or f"{row.channel} message",
            {
                "channel": row.channel,
                "status": row.status,
                "sent_at": row.sent_at.isoformat() if row.sent_at else None,
                "replied": bool(row.replied_at),
            },
        )
        graph.add(node)
        graph.link(Edge(EdgeKind.CONTACTED, candidate_key, node.id))

        contact = contacts.get(row.contact_id or "")
        if contact is not None and contact.name:
            # Match the contact back to a discovered person where we can, so the graph
            # shows one human rather than a person node and a contact node side by side.
            match = (
                (
                    await session.execute(
                        select(PersonRecord).where(
                            PersonRecord.company_id == row.company_id,
                            PersonRecord.normalized_name == _normalized(contact.name),
                        )
                    )
                )
                .scalars()
                .first()
            )
            target = (
                person_node(match.name, match.id, title=match.title or "")
                if match is not None
                else Node(NodeKind.CONTACT, contact.id, contact.name or contact.address)
            )
            graph.add(target)
            graph.link(Edge(EdgeKind.TARGETS, node.id, target.id))
            if row.replied_at:
                graph.link(Edge(EdgeKind.REPLIED_TO, target.id, node.id))

        if row.job_id:
            graph.link(Edge(EdgeKind.ABOUT_JOB, node.id, row.job_id))


def _normalized(name: str) -> str:
    from ..models.person import normalize_name

    return normalize_name(name)
