"""Recording why an agent decided what it decided — spec §50, §77.

The event bus already answers "what happened": resume tailored, form filled, application
submitted. This answers the question you actually ask three weeks later, when a recruiter
mentions something odd or an application went somewhere surprising:

    Why did it choose that?

A decision row carries the inputs the agent saw, the evidence behind the choice, which
tools it consulted, what it picked, and how confident it was. That is a different record
from an event, and keeping them separate matters: events are a timeline, decisions are a
justification, and collapsing them produces a log that is too noisy to scan and too thin
to explain anything.

Two rules, both learned from the event sink:

  * **Failing to record must never fail the work.** An unwritable audit row is a lost
    record, not a reason to abandon an application that is otherwise fine.
  * **Evidence is ids and numbers, not copies.** Storing the full job description in
    every decision would dominate the database and go stale against the job itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..logging import get_logger

log = get_logger("agents.audit")

# Anything longer is a copy of a document rather than a justification.
MAX_OUTCOME_CHARS = 2_000


@dataclass
class Decision:
    """One agent choice, ready to persist.

    Built by the agent, recorded by the caller that owns a database session — so a pure
    agent can produce one without acquiring a session or becoming async.
    """

    agent: str
    decision: str
    task: str = ""
    subject_type: str = ""
    subject_id: str | None = None
    confidence: float = 0.0
    evidence: dict[str, Any] = field(default_factory=dict)
    tools_used: list[str] = field(default_factory=list)
    inputs: dict[str, Any] = field(default_factory=dict)
    outcome: str = ""
    model: str | None = None
    prompt_version: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "task": self.task,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "decision": self.decision,
            "confidence": round(self.confidence, 3),
            "evidence": dict(self.evidence),
            "tools_used": list(self.tools_used),
            "inputs": dict(self.inputs),
            "outcome": self.outcome[:MAX_OUTCOME_CHARS],
            "model": self.model,
            "prompt_version": self.prompt_version,
        }

    def summary(self) -> str:
        """One line, for the dashboard and the logs."""
        confidence = f" ({self.confidence:.0%})" if self.confidence else ""
        return f"{self.agent}: {self.decision}{confidence}"


async def record(
    session: Any,
    decision: Decision,
    *,
    run_id: str | None = None,
    candidate_id: str | None = None,
) -> bool:
    """Persist one decision. Returns whether it was written.

    Never raises. An audit trail that can take down an application is worse than no
    audit trail, and the caller has already done the work this describes.
    """
    from ..storage.models import AgentDecision

    try:
        session.add(
            AgentDecision(
                run_id=run_id,
                candidate_id=candidate_id,
                agent=decision.agent[:60],
                task=decision.task[:200],
                subject_type=decision.subject_type[:40],
                subject_id=decision.subject_id,
                decision=decision.decision[:120],
                confidence=decision.confidence,
                evidence=decision.evidence,
                tools_used=decision.tools_used,
                inputs=decision.inputs,
                outcome=decision.outcome[:MAX_OUTCOME_CHARS],
                model=decision.model,
                prompt_version=decision.prompt_version,
            )
        )
        await session.flush()
        log.debug("decision_recorded", agent=decision.agent, decision=decision.decision)
        return True
    except Exception as exc:
        log.debug("decision_record_failed", error=f"{type(exc).__name__}: {exc}"[:120])
        return False


async def record_many(
    session: Any,
    decisions: list[Decision],
    *,
    run_id: str | None = None,
    candidate_id: str | None = None,
) -> int:
    written = 0
    for decision in decisions:
        if await record(session, decision, run_id=run_id, candidate_id=candidate_id):
            written += 1
    return written


class Trail:
    """Decisions collected during one unit of work, flushed together.

    A campaign makes dozens of decisions across concurrent branches; writing each one
    inside its own session would serialise the fan-out on the database. Agents append
    here and the orchestrator flushes once.
    """

    def __init__(self, run_id: str | None = None, candidate_id: str | None = None) -> None:
        self.run_id = run_id
        self.candidate_id = candidate_id
        self.decisions: list[Decision] = []

    def add(self, decision: Decision) -> Decision:
        self.decisions.append(decision)
        return decision

    def note(self, agent: str, decision: str, **kwargs: Any) -> Decision:
        """Shorthand for the common case."""
        return self.add(Decision(agent=agent, decision=decision, **kwargs))

    async def flush(self, session: Any) -> int:
        written = await record_many(
            session, self.decisions, run_id=self.run_id, candidate_id=self.candidate_id
        )
        self.decisions.clear()
        return written

    def to_dicts(self) -> list[dict[str, Any]]:
        return [d.to_dict() for d in self.decisions]


# ─── Readers ────────────────────────────────────────────────────────────────


async def for_subject(session: Any, subject_type: str, subject_id: str) -> list[dict[str, Any]]:
    """Every decision made about one job, company or application."""
    from sqlalchemy import select

    from ..storage.models import AgentDecision

    rows = (
        (
            await session.execute(
                select(AgentDecision)
                .where(
                    AgentDecision.subject_type == subject_type,
                    AgentDecision.subject_id == subject_id,
                )
                .order_by(AgentDecision.at.asc())
            )
        )
        .scalars()
        .all()
    )
    return [_row_to_dict(row) for row in rows]


async def recent(session: Any, limit: int = 100, agent: str | None = None) -> list[dict[str, Any]]:
    from sqlalchemy import select

    from ..storage.models import AgentDecision

    stmt = select(AgentDecision).order_by(AgentDecision.at.desc()).limit(limit)
    if agent:
        stmt = stmt.where(AgentDecision.agent == agent)
    rows = (await session.execute(stmt)).scalars().all()
    return [_row_to_dict(row) for row in rows]


async def summarize(session: Any) -> dict[str, Any]:
    """How many decisions each agent has made, and how sure it tends to be.

    Low average confidence on a high-volume agent is worth seeing: it means the agent is
    doing a lot of work it is not certain about.
    """
    from sqlalchemy import func, select

    from ..storage.models import AgentDecision

    rows = (
        await session.execute(
            select(
                AgentDecision.agent,
                func.count().label("decisions"),
                func.avg(AgentDecision.confidence).label("avg_confidence"),
            ).group_by(AgentDecision.agent)
        )
    ).all()

    by_agent = [
        {
            "agent": agent,
            "decisions": int(count or 0),
            "avg_confidence": round(float(avg or 0.0), 3),
        }
        for agent, count, avg in rows
    ]
    by_agent.sort(key=lambda row: row["decisions"], reverse=True)
    return {
        "agents": by_agent,
        "total": sum(row["decisions"] for row in by_agent),
    }


def _row_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": row.id,
        "at": row.at.isoformat(),
        "agent": row.agent,
        "task": row.task,
        "subject_type": row.subject_type,
        "subject_id": row.subject_id,
        "decision": row.decision,
        "confidence": row.confidence,
        "evidence": row.evidence or {},
        "tools_used": row.tools_used or [],
        "inputs": row.inputs or {},
        "outcome": row.outcome,
        "model": row.model,
        "prompt_version": row.prompt_version,
    }
