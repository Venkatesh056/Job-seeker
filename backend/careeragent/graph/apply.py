"""The apply graph: one application, five agents, one interrupt.

    START → ① tailor → ② answer → ③ form → ④ verify ─┬─ interrupt ─→ ⑤ submit → END
                                                      └─ (needs a human) ─────→ END

Why this one is a graph even though it is linear: `interrupt()`. The approval can sit
for days, and LangGraph's checkpointer is what lets the run resume at ⑤ rather than
restart at ①. Re-running ① would tailor a second resume and re-open a second form.

**The one thing that is not graph state: the browser page.** It is held in
`applications/sessions.py` keyed by application id, because an approval is only
meaningful if it approves *the form the user looked at*, and a page cannot be
serialized into any checkpointer. If the session has expired by the time approval
arrives, ⑤ re-prepares and says so in the event log — degrading to slower, never to
submitting something unseen.

Three modes, chosen per campaign:

    one_click   ④ → ⑤ directly. Clicking Apply was the approval.
    preview     ④ → interrupt → ⑤. Default. You see it before it leaves.
    threshold   one_click above a match score, preview below it.

None of them can reach a CAPTCHA, an MFA prompt, a login wall or a demographic
question: those are refused in the layer below and surface as `needs_user`.
"""

from __future__ import annotations

from typing import Any

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..logging import get_logger
from ..storage import repository as repo
from ..storage import session_scope
from .state import ApplyState

log = get_logger("graph.apply")

MODES = ("one_click", "preview", "threshold")
DEFAULT_THRESHOLD = 0.85


# ─── ① Tailor agent ─────────────────────────────────────────────────────────


async def tailor_agent(state: ApplyState) -> dict[str, Any]:
    """A job-specific resume, validated before anything else runs.

    First on purpose: if the evidence validator rejects the document there is no point
    opening a browser, and the failure is cheap and explainable rather than half an
    application into a real form.
    """
    from ..applications import service

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        application = await repo.get_application(session, state["application_id"])
        if candidate is None or application is None:
            return {"error": "application or candidate not found", "status": "failed"}

        try:
            render_id, validation = await service.tailor_for(
                session,
                application,
                candidate,
                use_llm=state.get("use_llm", True),
            )
        except Exception as exc:
            log.warning("tailor_agent_failed", error=f"{type(exc).__name__}: {exc}"[:160])
            return {"error": f"tailoring failed: {type(exc).__name__}", "status": "failed"}

    return {"resume_render_id": render_id or "", "validation": validation}


# ─── ② Answer agent ─────────────────────────────────────────────────────────


async def answer_agent(state: ApplyState) -> dict[str, Any]:
    """Cover letter, if the campaign asked for one.

    Open-question answering happens inside ③ rather than here, because the questions
    are not knowable until the form has been read — asking a model to pre-answer
    questions nobody has seen is how an application ends up with a confident answer to
    a question the form never asked.
    """
    if state.get("error") or not state.get("use_llm", True):
        return {}

    from ..applications import service

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        application = await repo.get_application(session, state["application_id"])
        if candidate is None or application is None:
            return {}
        try:
            letter_id = await service.cover_letter_for(session, application, candidate)
        except Exception as exc:
            log.info("cover_letter_skipped", error=f"{type(exc).__name__}"[:80])
            return {}
    return {"cover_letter_id": letter_id or ""}


# ─── ③ Form agent + ④ verify ────────────────────────────────────────────────


async def form_agent(state: ApplyState) -> dict[str, Any]:
    """Open the form, fill what can be evidenced, verify every write, and stop.

    Delegates to `service.prepare`, which owns the browser, the resolver, the never-fill
    rules and the pre-submit verdict. The graph does not reimplement any of that — it
    sequences it, and the safety properties stay in one place with one test suite.
    """
    if state.get("error"):
        return {}

    from ..applications import service
    from ..applications.service import ApplicationError

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            return {"error": "no candidate", "status": "failed"}
        try:
            result = await service.prepare(
                session,
                state["application_id"],
                candidate,
                use_llm=state.get("use_llm", True),
                autonomy_level=state.get("autonomy_level", 4),
            )
        except ApplicationError as exc:
            return {"error": str(exc), "status": "failed"}

    # `str()` on the status is not cosmetic: ApplicationStatus is a StrEnum, and a
    # checkpointer serializing the subclass warns today and refuses in a future
    # version. Graph state stays plain.
    return {
        "ats": result.ats,
        "url": result.url,
        "filled": result.filled,
        "needs_user": result.needs_user,
        "never_filled": result.never_filled,
        "holds": result.holds,
        "answers": result.answers,
        "screenshots": result.screenshots,
        "status": str(result.status),
        "error": result.error,
    }


def route_after_form(state: ApplyState) -> str:
    """Decide whether a human has to look at this one.

    Four ways to end up in front of a person, and only the last is a *choice*: a field
    we cannot evidence, a pre-submit hold, a blocker like CAPTCHA — and the configured
    mode. The first three are not overridable by any mode.
    """
    if state.get("error") or state.get("status") in {"failed", "failed_terminal"}:
        return END
    if state.get("status") == "human_required" or state.get("needs_user"):
        return END
    if state.get("holds"):
        return END

    mode = state.get("mode", "preview")
    if mode == "one_click":
        return "submit_agent"
    if mode == "threshold":
        score = (state.get("validation") or {}).get("match_score")
        if isinstance(score, (int, float)) and score >= DEFAULT_THRESHOLD:
            return "submit_agent"
    return "approval_gate"


# ─── The interrupt ──────────────────────────────────────────────────────────


async def approval_gate(state: ApplyState) -> dict[str, Any]:
    """Pause until a human approves. This is why the graph exists.

    `interrupt()` suspends the run and checkpoints it. Resuming with
    `Command(resume={"approved": True})` continues at ⑤ with the tailored resume, the
    generated answers and the held browser page all intact — no re-tailoring, no second
    form, no duplicate application.
    """
    decision = interrupt(
        {
            "application_id": state["application_id"],
            "ats": state.get("ats"),
            "url": state.get("url"),
            "filled": state.get("filled", []),
            "never_filled": state.get("never_filled", []),
            "answers": state.get("answers", []),
            "screenshots": state.get("screenshots", []),
            "validation": state.get("validation", {}),
            "question": "Approve and submit this application?",
        }
    )

    if isinstance(decision, dict):
        return {
            "approved": bool(decision.get("approved")),
            "field_overrides": decision.get("field_overrides") or {},
        }
    return {"approved": bool(decision)}


def route_after_approval(state: ApplyState) -> str:
    return "submit_agent" if state.get("approved") else END


# ─── ⑤ Submit agent ─────────────────────────────────────────────────────────


async def submit_agent(state: ApplyState) -> dict[str, Any]:
    """Submit, and record what actually happened.

    Goes through `service.approve`, which re-reads the page and re-runs the pre-submit
    verdict before clicking. An approval authorises submitting a form that passes — not
    one that stopped passing while it waited.
    """
    from ..applications import service
    from ..applications.service import ApplicationError

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            return {"error": "no candidate", "status": "failed"}
        try:
            result = await service.approve(
                session,
                state["application_id"],
                candidate,
                field_overrides=state.get("field_overrides") or {},
                autonomy_level=max(state.get("autonomy_level", 4), 4),
            )
        except ApplicationError as exc:
            return {"error": str(exc), "status": "failed"}

    return {
        "status": str(result.status),
        "outcome": str(result.status),
        "evidence": dict(result.evidence),
        "holds": [str(h) for h in result.holds],
        "screenshots": list(result.screenshots),
        "error": result.error,
    }


# ─── The graph ──────────────────────────────────────────────────────────────


def build_apply_graph(checkpointer: Any = None):
    """Compile the apply graph.

    A checkpointer is required for the interrupt to be resumable. The durable one is
    opened at startup (`graph/checkpoints.py`) and follows the application database, so
    a parked approval survives a restart and resumes at the submit step instead of
    re-tailoring the resume and re-opening the form.

    When no durable saver is open — a unit test, a CLI one-shot — this falls back to
    `InMemorySaver`, which is honest about its limits rather than refusing to run.
    """
    graph = StateGraph(ApplyState)

    graph.add_node("tailor_agent", tailor_agent)
    graph.add_node("answer_agent", answer_agent)
    graph.add_node("form_agent", form_agent)
    graph.add_node("approval_gate", approval_gate)
    graph.add_node("submit_agent", submit_agent)

    graph.add_edge(START, "tailor_agent")
    graph.add_edge("tailor_agent", "answer_agent")
    graph.add_edge("answer_agent", "form_agent")
    graph.add_conditional_edges(
        "form_agent", route_after_form, ["approval_gate", "submit_agent", END]
    )
    graph.add_conditional_edges("approval_gate", route_after_approval, ["submit_agent", END])
    graph.add_edge("submit_agent", END)

    if checkpointer is None:
        from .checkpoints import current

        checkpointer = current() or InMemorySaver()
    return graph.compile(checkpointer=checkpointer)


_graph = None
_graph_saver: Any = None


def get_apply_graph():
    """The compiled graph, rebuilt if the checkpointer changed underneath it.

    The rebuild matters at startup: the graph can be compiled by an early caller before
    `open_checkpointer()` has run, and a graph compiled against the in-memory fallback
    would keep using it for the life of the process — silently non-durable.
    """
    global _graph, _graph_saver
    from .checkpoints import current

    saver = current()
    if _graph is None or saver is not _graph_saver:
        _graph = build_apply_graph(saver)
        _graph_saver = saver
    return _graph


async def start_application(
    application_id: str,
    *,
    mode: str = "preview",
    use_llm: bool = True,
    autonomy_level: int = 4,
) -> dict[str, Any]:
    """Run one application up to its first stopping point.

    Returns the state plus `awaiting_approval`, which tells the caller whether the graph
    is parked at an interrupt waiting for `resume_application`.
    """
    graph = get_apply_graph()
    config = {"configurable": {"thread_id": application_id}, "recursion_limit": 40}

    initial: ApplyState = {
        "application_id": application_id,
        "mode": mode if mode in MODES else "preview",
        "use_llm": use_llm,
        "autonomy_level": autonomy_level,
    }
    state = await graph.ainvoke(initial, config)
    snapshot = await graph.aget_state(config)
    return {
        **state,
        "awaiting_approval": bool(snapshot.next and "approval_gate" in str(snapshot.next)),
        "interrupts": [i.value for task in snapshot.tasks for i in (task.interrupts or [])],
    }


async def resume_application(
    application_id: str,
    *,
    approved: bool,
    field_overrides: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Answer a parked approval and let the run finish."""
    from langgraph.types import Command

    graph = get_apply_graph()
    config = {"configurable": {"thread_id": application_id}, "recursion_limit": 40}

    state = await graph.ainvoke(
        Command(resume={"approved": approved, "field_overrides": field_overrides or {}}),
        config,
    )
    return dict(state)


async def pending_approval(application_id: str) -> dict[str, Any] | None:
    """What a parked run is waiting to be told, or None if it is not parked."""
    graph = get_apply_graph()
    config = {"configurable": {"thread_id": application_id}}
    try:
        snapshot = await graph.aget_state(config)
    except Exception:
        return None
    for task in snapshot.tasks:
        for item in task.interrupts or []:
            return item.value
    return None
