"""The Career Supervisor — spec §5.

Every other graph here has a shape decided before it runs. Discovery is always
source → aggregate → enrich → rank. An application is always tailor → answer → fill →
approve → submit. Those are pipelines, and a pipeline is the right answer when you already
know the order.

This one does not know the order. It is given a goal in a sentence — *"find me backend
roles in Bangalore and get three applications ready"* — and it decides, one step at a time,
what to do next based on what the last step found. That is the difference between a
workflow and a supervisor, and it is the whole reason this file exists.

    ┌──────────┐      ┌────────┐      ┌──────────────────────────────┐
    │  assess  │ ───▶ │ decide │ ───▶ │ discover │ evaluate │ queue   │
    └──────────┘      └────────┘      │ prepare  │ research │ report  │
                           ▲          └──────────┬───────────────────┘
                           └─────────────────────┘
                             until done, or a limit is hit

**Deciding is a model's job; the limits are not.** `decide` asks a tool-using agent what to
do next. Everything that constrains it is code:

  * the step it names must be one of `STEPS` — an unknown step ends the run rather than
    being improvised;
  * `max_steps` is enforced by the router, not requested in the prompt;
  * repeating the same step against the same input three times ends the run, because that
    is what a stuck agent looks like from outside;
  * `prepare` respects the autonomy level, and at every level below 5 it stops at the
    approval. The supervisor cannot submit anything. There is no step for it.

**Why it cannot apply.** `prepare` fills a form and parks it. Submission happens in the
apply graph behind `interrupt()`, where a person looks at what was filled and clicks. A
supervisor that could submit would be a supervisor that could submit a form nobody read,
and no amount of reasoning quality makes that acceptable.

**When no model is available** the supervisor still works. It falls back to a fixed,
sensible order — assess, discover, evaluate, queue, report — which is the pipeline it
replaced. Degrading to the old behaviour is a working outcome; refusing to run is not.
"""

from __future__ import annotations

import json
from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph

from ..logging import get_logger
from .state import SupervisorState

log = get_logger("graph.supervisor")

# The steps the supervisor may choose between. A model naming anything else is a model
# that has lost the thread, and the router treats that as a reason to stop.
STEPS = ("discover", "evaluate", "research", "queue", "prepare", "report")

MAX_STEPS = 8
# Three identical attempts is what being stuck looks like from the outside.
STUCK_AFTER = 3

DECIDE_SYSTEM = """You are a career supervisor. You decide what to do next, one step at a time.

You are working towards the candidate's stated goal. Look at what has already happened,
use your tools to find out what you need, then name the single next step.

The steps available to you:
  discover  — find companies and openings matching the goal
  evaluate  — score and rank opportunities already found
  research  — read a company's own pages to learn about them
  queue     — shortlist the opportunities worth pursuing
  prepare   — tailor a resume and fill the application form, stopping before submission
  report    — nothing useful is left to do; summarise what happened

Rules you cannot change:
- You cannot submit an application. `prepare` stops at the approval, where a person
  decides. Do not plan around this.
- Do not repeat a step that already produced what it was going to produce.
- If the goal is already met, choose `report`.
- Prefer the cheapest step that moves things forward. `discover` costs real API quota.

When you have finished using tools, reply with a message whose entire content is this
JSON object — not as a tool call, as your message:
{"step": "one of the steps above",
 "reason": "one sentence on why this is the right next step",
 "input": {}}"""


# Why the supervisor could not choose, in words that are true of the specific failure. An
# earlier version said "no model available" for every case, including a rate limit — so the
# summary explained the run with something that had not happened.
WHY_FALLBACK = {
    "no_model_available": "no language model is configured",
    "model_failed": "the language model stopped responding",
    "budget_exhausted": "the daily LLM budget is spent",
    "turn_limit": "the model kept calling tools without answering",
    "tool_call_limit": "the model called too many tools without answering",
}


def _fallback_step(state: SupervisorState, because: str = "") -> tuple[str, str]:
    """The next step when the model could not be asked, or could not answer usably.

    This is the fixed pipeline the supervisor replaced, and falling back to it is a working
    outcome rather than a failure: the system still finds jobs, still ranks them, still
    queues them. What is lost is the judgement about *which* of those is worth doing next,
    so the reason says so plainly rather than implying a choice was made.
    """
    cause = WHY_FALLBACK.get(because, "the model did not return a usable answer")
    done = {entry.get("step") for entry in state.get("history") or []}
    for step in ("discover", "evaluate", "queue", "report"):
        if step not in done:
            return step, f"next in the fixed order — {cause}"
    return "report", f"every step in the fixed order has run — {cause}"


def _is_stuck(state: SupervisorState, step: str, step_input: dict[str, Any]) -> bool:
    """Has this exact step already been tried this many times?

    Compared on step *and* input: legitimately running `research` for three different
    companies is progress, and running it three times for the same one is not.
    """
    signature = (step, json.dumps(step_input or {}, sort_keys=True, default=str))
    seen = sum(
        1
        for entry in state.get("history") or []
        if (entry.get("step"), json.dumps(entry.get("input") or {}, sort_keys=True, default=str))
        == signature
    )
    return seen >= STUCK_AFTER


# ─── ① Assess ───────────────────────────────────────────────────────────────


async def assess_agent(state: SupervisorState) -> dict[str, Any]:
    """Establish where things stand before deciding anything.

    Deterministic on purpose. A model asked to both gather context and choose a step tends
    to choose first and justify afterwards, so the facts are assembled by code and handed
    to it.
    """
    from sqlalchemy import func, select

    from ..storage import repository as repo
    from ..storage import session_scope
    from ..storage.models import Application, Job

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        jobs = (await session.execute(select(func.count()).select_from(Job))).scalar_one()
        row = await repo.get_or_create_candidate(session)
        statuses = (
            await session.execute(
                select(Application.status, func.count())
                .where(Application.candidate_id == row.id)
                .group_by(Application.status)
            )
        ).all()

    if candidate is None:
        return {
            "done": True,
            "stopped_because": "no_candidate",
            "error": "no candidate profile has been loaded; upload a resume first",
        }

    by_status = {str(status): int(count) for status, count in statuses}
    context = {
        "candidate": candidate.name,
        "skills": len(candidate.skill_labels()),
        "facts": len(candidate.facts),
        "jobs_known": int(jobs or 0),
        "applications_by_status": by_status,
        "awaiting_approval": by_status.get("awaiting_approval", 0),
    }
    log.info("supervisor_assessed", **{k: v for k, v in context.items() if k != "candidate"})
    return {
        "candidate_id": row.id,
        "history": [{"step": "assess", "input": {}, "result": context}],
    }


# ─── ② Decide ───────────────────────────────────────────────────────────────


async def decide_agent(state: SupervisorState) -> dict[str, Any]:
    """Choose the next step. The only node here that reasons."""
    if state.get("done"):
        return {}

    steps_taken = state.get("steps_taken", 0)
    if steps_taken >= state.get("max_steps", MAX_STEPS):
        return {
            "done": True,
            "stopped_because": "step_limit",
            "next_step": "report",
        }

    from ..llm import toolloop
    from ..llm.router import Tier

    history = state.get("history") or []
    prompt = (
        f"GOAL: {state.get('goal', '')}\n\n"
        f"STEPS TAKEN SO FAR ({steps_taken}, limit {state.get('max_steps', MAX_STEPS)}):\n"
        + (
            "\n".join(
                f"  {i + 1}. {e.get('step')} → {json.dumps(e.get('result'), default=str)[:400]}"
                for i, e in enumerate(history)
            )
            or "  (nothing yet)"
        )
        + f"\n\nAUTONOMY LEVEL: {state.get('autonomy_level', 4)} "
        "(below 5, `prepare` always stops at the approval)\n"
        "\nWhat is the single next step?"
    )

    run = await toolloop.run(
        agent="career_supervisor",
        toolset="supervisor",
        system=DECIDE_SYSTEM,
        prompt=prompt,
        tier=Tier.STRONG,
        max_turns=4,
    )

    parsed = run.json() if run.ok else None
    if not isinstance(parsed, dict):
        step, reason = _fallback_step(state, str(run.stopped))
        log.info("supervisor_fallback", step=step, stopped=str(run.stopped))
        # If the model could not answer at all — no key, rate limited, out of budget — that
        # is why the run ends, and the report has to say so. Falling through to the default
        # "goal_reached" told the user the goal was met when the supervisor had simply
        # stopped being able to think, which is the one thing a summary must never do.
        unreachable = str(run.stopped) in {
            "no_model_available",
            "model_failed",
            "budget_exhausted",
        }
        return {
            "next_step": step,
            "step_reason": reason,
            "step_input": {},
            "findings": [f"decided by fixed order ({run.stopped})"],
            # Recorded in `plan` as well, so the plan is a complete account of what was
            # decided. Omitting the steps chosen under failure made a three-step run look
            # like a one-step one.
            "plan": [{"step": step, "reason": reason, "input": {}, "chosen_by": "fixed order"}],
            **({"stopped_because": str(run.stopped)} if unreachable and step == "report" else {}),
        }

    step = str(parsed.get("step") or "").strip().lower()
    reason = str(parsed.get("reason") or "")[:400]
    raw_input = parsed.get("input")
    step_input: dict[str, Any] = dict(raw_input) if isinstance(raw_input, dict) else {}

    if step not in STEPS:
        # An invented step ends the run. Guessing what was meant is how a supervisor
        # starts doing something nobody asked for.
        log.warning("supervisor_invented_step", step=step[:60])
        return {
            "done": True,
            "stopped_because": "invalid_step",
            "next_step": "report",
            "step_reason": f"named a step that does not exist: {step[:60]!r}",
        }

    if _is_stuck(state, step, step_input):
        log.warning("supervisor_stuck", step=step, attempts=STUCK_AFTER)
        return {
            "done": True,
            "stopped_because": "stuck",
            "next_step": "report",
            "step_reason": f"chose {step} with the same input {STUCK_AFTER} times",
        }

    await _record(
        state,
        agent="career_supervisor",
        task=f"what next, towards: {state.get('goal', '')[:120]}",
        decision=step,
        confidence=0.8 if run.ok else 0.4,
        evidence={
            "steps_taken": steps_taken,
            "turns": run.turns,
            "tool_calls": len(run.tools),
        },
        tools_used=run.tools.names(),
        outcome=reason,
        model=run.model,
    )

    log.info("supervisor_decided", step=step, tools=run.tools.names(), turns=run.turns)
    return {
        "next_step": step,
        "step_reason": reason,
        "step_input": step_input,
        "plan": [{"step": step, "reason": reason, "input": step_input, "chosen_by": "model"}],
    }


async def _record(state: SupervisorState, **fields: Any) -> None:
    """Write one supervisor decision to the trail. Never raises."""
    from ..agents import audit
    from ..storage import session_scope

    try:
        async with session_scope() as session:
            await audit.record(
                session,
                audit.Decision(subject_type="campaign", subject_id=state.get("run_id"), **fields),
                run_id=state.get("run_id"),
                candidate_id=state.get("candidate_id"),
            )
    except Exception as exc:  # pragma: no cover — the audit path is already defensive
        log.debug("supervisor_audit_failed", error=f"{type(exc).__name__}"[:80])


# ─── ③ Routing ──────────────────────────────────────────────────────────────


def route(state: SupervisorState) -> str:
    """Where to go after deciding. The dynamic edge that makes this a supervisor.

    `max_steps` is checked here as well as in `decide`, because a node that ignores its own
    limit is exactly the failure a supervisor can produce.
    """
    if state.get("done") or state.get("error"):
        return "report"
    if state.get("steps_taken", 0) >= state.get("max_steps", MAX_STEPS):
        return "report"
    step = state.get("next_step") or "report"
    return step if step in STEPS else "report"


# ─── ④ The steps ────────────────────────────────────────────────────────────


async def discover_step(state: SupervisorState) -> dict[str, Any]:
    """Find companies and openings. The expensive one — it spends API quota."""
    from .campaign import run_campaign

    step_input = state.get("step_input") or {}
    locations = step_input.get("locations") or ["remote"]
    if isinstance(locations, str):
        locations = [locations]

    if state.get("dry_run"):
        return _step_done("discover", step_input, {"skipped": "dry run — no API quota spent"})

    try:
        result = await run_campaign(
            candidate_id=state.get("candidate_id", ""),
            locations=list(locations)[:3],
            family_ids=step_input.get("family_ids") or None,
            max_companies=int(step_input.get("max_companies") or 40),
        )
    except Exception as exc:
        return _step_done("discover", step_input, {"error": f"{type(exc).__name__}: {exc}"[:200]})

    ranked = result.get("ranked") or []
    return _step_done(
        "discover",
        step_input,
        {
            "companies": len(result.get("companies") or []),
            "openings": len(ranked),
            "top": [
                {"job_id": j.get("job_id"), "title": j.get("title"), "company": j.get("company")}
                for j in ranked[:5]
            ],
        },
    )


async def evaluate_step(state: SupervisorState) -> dict[str, Any]:
    """Score what is already known. Cheap, deterministic, no network."""
    from sqlalchemy import select

    from .. import preferences
    from ..applications.service import _job_to_normalized
    from ..pipeline.opportunity import decide as decide_opportunity
    from ..pipeline.opportunity import score_opportunity
    from ..pipeline.score import ScoreContext, score_job
    from ..storage import repository as repo
    from ..storage import session_scope
    from ..storage.models import Job

    step_input = state.get("step_input") or {}
    limit = int(step_input.get("limit") or 40)

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        jobs = (
            (await session.execute(select(Job).order_by(Job.posted_date.desc()).limit(limit)))
            .scalars()
            .all()
        )
        if candidate is None:
            return _step_done("evaluate", step_input, {"error": "no candidate"})

        ctx = ScoreContext(
            candidate=candidate, config=await preferences.load(session, state["candidate_id"])
        )
        assessed: list[dict[str, Any]] = []
        for job in jobs:
            normalized = _job_to_normalized(job)
            deterministic = score_job(normalized, ctx)
            score = score_opportunity(normalized, deterministic)
            verdict = decide_opportunity(
                normalized, score, autonomy_level=state.get("autonomy_level", 4)
            )
            assessed.append(
                {
                    "job_id": job.id,
                    "title": job.title,
                    "company": job.company,
                    "score": round(score.final_score, 4),
                    "action": verdict.action,
                    "reason": verdict.reason,
                }
            )

    assessed.sort(key=lambda row: row["score"], reverse=True)
    worth = [row for row in assessed if row["action"] in {"apply", "review"}]
    return _step_done(
        "evaluate",
        step_input,
        {
            "scored": len(assessed),
            "worth_pursuing": len(worth),
            "top": worth[:8],
        },
    )


async def research_step(state: SupervisorState) -> dict[str, Any]:
    """Read one company's own pages. Bounded to one company per step on purpose —
    research is slow, and a supervisor that researches forty companies before deciding
    anything has spent the run on preparation."""
    step_input = state.get("step_input") or {}
    name = str(step_input.get("company") or "").strip()
    if not name:
        return _step_done("research", step_input, {"error": "no company named"})

    from ..company import research
    from ..storage import repository as repo
    from ..storage import session_scope

    try:
        profile = await research(name)
    except Exception as exc:
        return _step_done("research", step_input, {"error": f"{type(exc).__name__}"[:80]})

    async with session_scope() as session:
        await repo.upsert_company(session, profile.name, profile.domain)

    return _step_done(
        "research",
        step_input,
        {
            "company": profile.name,
            "domain": profile.domain,
            "readable_chars": profile.readable_chars,
            "tech_focus": list(profile.tech_focus)[:10],
            "fit_score": profile.fit_score,
        },
    )


async def queue_step(state: SupervisorState) -> dict[str, Any]:
    """Shortlist what is worth pursuing. Writes application rows; sends nothing."""
    from ..tools import invoke

    step_input = state.get("step_input") or {}
    job_ids = step_input.get("job_ids") or []
    if isinstance(job_ids, str):
        job_ids = [job_ids]

    if not job_ids:
        # Nothing named: take the best from the last evaluation rather than guessing.
        for entry in reversed(state.get("history") or []):
            if entry.get("step") == "evaluate":
                job_ids = [
                    row["job_id"]
                    for row in (entry.get("result") or {}).get("top") or []
                    if row.get("action") == "apply"
                ][:5]
                break

    queued: list[dict[str, Any]] = []
    already: list[str] = []
    refused: list[dict[str, Any]] = []
    for job_id in list(job_ids)[:10]:
        result = await invoke(
            "shortlist_job",
            {"job_id": job_id, "reason": state.get("step_reason", "")[:200]},
        )
        if not result.ok:
            refused.append({"job_id": job_id, "why": result.detail[:160]})
        elif (result.data or {}).get("newly_queued"):
            queued.append({"job_id": job_id, **(result.data or {})})
        else:
            already.append(job_id)

    # Reported separately, because "queued 3" and "3 were already queued" mean opposite
    # things to whatever decides the next step. Conflating them is how a supervisor
    # concludes it made progress and then runs the same step again.
    return {
        **_step_done(
            "queue",
            step_input,
            {
                "newly_queued": len(queued),
                "already_queued": len(already),
                "refused": refused,
                "note": (
                    "nothing new was queued — every candidate job was already in the queue"
                    if already and not queued
                    else ""
                ),
            },
        ),
        "shortlisted": queued,
    }


async def prepare_step(state: SupervisorState) -> dict[str, Any]:
    """Tailor a resume, fill the form, and stop.

    The supervisor's furthest reach. `prepare` opens a browser and writes into a real form,
    then parks the application at `awaiting_approval` — which is where a person looks at it.
    Nothing here can click submit, and there is no step that can.
    """
    from sqlalchemy import select

    from ..applications import service
    from ..storage import repository as repo
    from ..storage import session_scope
    from ..storage.models import Application
    from ..storage.models import ApplicationStatus as S

    step_input = state.get("step_input") or {}
    limit = int(step_input.get("limit") or 1)

    if state.get("dry_run"):
        return _step_done("prepare", step_input, {"skipped": "dry run — no browser opened"})

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        row = await repo.get_or_create_candidate(session)
        pending = (
            (
                await session.execute(
                    select(Application)
                    .where(
                        Application.candidate_id == row.id,
                        Application.status.in_([S.SHORTLISTED, S.RESUME_TAILORED]),
                    )
                    .limit(max(1, min(limit, 3)))
                )
            )
            .scalars()
            .all()
        )
        application_ids = [a.id for a in pending]
        if candidate is None:
            return _step_done("prepare", step_input, {"error": "no candidate"})

    prepared: list[dict[str, Any]] = []
    parked: list[str] = []
    for application_id in application_ids:
        async with session_scope() as session:
            try:
                result = await service.prepare(
                    session,
                    application_id,
                    candidate,
                    autonomy_level=min(state.get("autonomy_level", 4), 4),
                )
            except Exception as exc:
                prepared.append(
                    {"application_id": application_id, "error": f"{type(exc).__name__}"[:80]}
                )
                continue
        prepared.append(
            {
                "application_id": application_id,
                "status": str(result.status),
                "filled": len(result.filled),
                "needs_user": len(result.needs_user),
                "error": result.error or None,
            }
        )
        if str(result.status) == "awaiting_approval":
            parked.append(application_id)

    return {
        **_step_done("prepare", step_input, {"prepared": prepared}),
        "prepared": prepared,
        "awaiting_approval": parked,
    }


# Plain-language endings. `goal_reached` is the only one that claims success, and it is
# only used when nothing interrupted the run — an earlier version defaulted to it after a
# rate limit, reporting that the goal was met when the supervisor had stopped being able to
# choose a step at all.
EXPLAIN = {
    "goal_reached": "there was nothing useful left to do",
    "step_limit": "the step limit was reached",
    "stuck": "the same step kept being chosen with no new result",
    "invalid_step": "the model named a step that does not exist",
    "no_candidate": "no candidate profile has been loaded",
    "no_model_available": "no language model is configured, so it fell back to a fixed order",
    "model_failed": "the language model stopped responding (often a rate limit)",
    "budget_exhausted": "the daily LLM budget ran out",
}


async def report_step(state: SupervisorState) -> dict[str, Any]:
    """Say what happened, in the terms the user asked in.

    Written by code rather than by a model. A summary is the one place where an
    invented detail is most likely to be believed, and the facts are all to hand.
    """
    from sqlalchemy import func, select

    from ..storage import repository as repo
    from ..storage import session_scope
    from ..storage.models import Application

    history = state.get("history") or []
    steps = [str(e.get("step")) for e in history if e.get("step") not in (None, "assess")]
    parked = state.get("awaiting_approval") or []
    shortlisted = state.get("shortlisted") or []

    # Read from the database rather than from what the supervisor believes. A model asked
    # why it stopped will occasionally characterise the state wrongly — one run reported
    # "no opportunities have been queued yet" with two sitting in the queue — and its reason
    # is quoted verbatim below. Stating the real counts beside the quote means a wrong
    # characterisation is contradicted on the same line instead of standing alone.
    waiting = queued_total = 0
    try:
        async with session_scope() as session:
            row = await repo.get_or_create_candidate(session)
            rows = (
                await session.execute(
                    select(Application.status, func.count())
                    .where(Application.candidate_id == row.id)
                    .group_by(Application.status)
                )
            ).all()
        counts: dict[str, int] = {str(status): int(count or 0) for status, count in rows}
        # Rejected applications are not "in your queue" in any useful sense.
        queued_total = sum(n for status, n in counts.items() if status != "rejected")
        waiting = counts.get("awaiting_approval", 0)
    except Exception as exc:  # pragma: no cover — a summary must not fail the run
        log.debug("supervisor_report_counts_failed", error=f"{type(exc).__name__}"[:80])

    parts = [f"Goal: {state.get('goal', '')}".strip()]
    parts.append(f"Ran {len(steps)} step(s): {', '.join(steps) or 'none'}.")
    if shortlisted:
        parts.append(f"Shortlisted {len(shortlisted)} new opening(s).")
    parts.append(
        f"Your queue now holds {queued_total} application(s)"
        + (f", {waiting} of them waiting for you." if waiting else ".")
    )
    if parked:
        parts.append(f"{len(parked)} of those were filled during this run. Nothing was submitted.")
    # The supervisor's own account of why it stopped. Quoted rather than paraphrased, and
    # only when it chose `report` itself: it is the one place a partial result gets
    # explained ("two of the target three are queued"), and summarising it in code would
    # mean deciding what "partial" meant, which is the judgement being quoted.
    if state.get("next_step") == "report" and (said := state.get("step_reason")):
        parts.append(f"It stopped here because: {said.rstrip('.')}.")
    if reason := state.get("stopped_because"):
        parts.append(f"Stopped because: {EXPLAIN.get(reason, reason)}.")
    if err := state.get("error"):
        parts.append(f"Error: {err}")

    summary = " ".join(p for p in parts if p)
    log.info("supervisor_finished", steps=len(steps), parked=len(parked))
    return {
        "done": True,
        "summary": summary,
        "stopped_because": state.get("stopped_because") or "goal_reached",
    }


def _step_done(step: str, step_input: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
    """The bookkeeping every step shares.

    `steps_taken` is 1 because the field sums across updates. Every step must go through
    this helper for that reason — a step that returns its own dict without it stops the
    counter, and `max_steps` stops meaning anything.
    """
    return {
        "history": [{"step": step, "input": step_input, "result": result}],
        "steps_taken": 1,
    }


# ─── ⑤ The graph ────────────────────────────────────────────────────────────


def build_supervisor_graph(checkpointer: Any = None):
    """Compile the supervisor.

    Every step routes back to `decide`, which is what makes the order emergent rather than
    written down. `report` is the only node that leads to END.
    """
    graph = StateGraph(SupervisorState)

    graph.add_node("assess", assess_agent)
    graph.add_node("decide", decide_agent)
    graph.add_node("discover", discover_step)
    graph.add_node("evaluate", evaluate_step)
    graph.add_node("research", research_step)
    graph.add_node("queue", queue_step)
    graph.add_node("prepare", prepare_step)
    graph.add_node("report", report_step)

    graph.add_edge(START, "assess")
    graph.add_edge("assess", "decide")
    graph.add_conditional_edges(
        "decide",
        route,
        {step: step for step in STEPS},
    )
    # The loop. Each step reports back and the supervisor decides again.
    for step in STEPS:
        if step != "report":
            graph.add_edge(step, "decide")
    graph.add_edge("report", END)

    if checkpointer is None:
        from .checkpoints import current

        checkpointer = current() or InMemorySaver()
    # The recursion limit is the backstop behind `max_steps`: six steps plus a decide turn
    # each, with headroom, and a run that somehow exceeds it fails loudly rather than
    # spending the budget.
    return graph.compile(checkpointer=checkpointer)


_graph = None
_graph_saver: Any = None


def get_supervisor_graph():
    global _graph, _graph_saver
    from .checkpoints import current

    saver = current()
    if _graph is None or saver is not _graph_saver:
        _graph = build_supervisor_graph(saver)
        _graph_saver = saver
    return _graph


async def run_supervisor(
    goal: str,
    *,
    max_steps: int = MAX_STEPS,
    autonomy_level: int | None = None,
    dry_run: bool = False,
    run_id: str | None = None,
) -> dict[str, Any]:
    """Pursue one goal. Returns the final state.

    `dry_run` skips the two steps that cost something real — `discover` spends API quota and
    `prepare` opens a browser — so the routing itself can be exercised for the price of the
    model calls alone.
    """
    import uuid

    from ..config import get_settings

    settings = get_settings()
    thread = run_id or str(uuid.uuid4())
    state: SupervisorState = {
        "run_id": thread,
        "goal": goal,
        "max_steps": max(1, min(max_steps, 20)),
        "autonomy_level": (settings.autonomy_level if autonomy_level is None else autonomy_level),
        "dry_run": dry_run,
        "steps_taken": 0,
    }
    config = {
        "configurable": {"thread_id": thread},
        "recursion_limit": max(25, state["max_steps"] * 3 + 10),
    }
    log.info("supervisor_started", goal=goal[:120], max_steps=state["max_steps"], dry_run=dry_run)
    return await get_supervisor_graph().ainvoke(state, config=config)
