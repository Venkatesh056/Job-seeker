"""The Career Supervisor — spec §5.

A supervisor is the one component here whose *job* is to be unpredictable, so what gets
tested is not the path it picks but the walls around it: the steps it is allowed to name,
the limits it cannot talk its way past, and the things it has no way to do at all.

The routing tests run without a model. That is deliberate and it is also the fallback path
in production: with no key, a rate limit, or an exhausted budget, the supervisor drops to a
fixed order and still works. A test suite that needed a live model would leave that path
uncovered — and it is the path that runs on a bad day.
"""

from __future__ import annotations

import pytest

from careeragent.graph import supervisor as sup

# ─── The step vocabulary is closed ──────────────────────────────────────────


def test_there_is_no_step_that_submits_an_application():
    """The supervisor's furthest reach is a filled form waiting for a person.

    Submission lives in the apply graph behind `interrupt()`. If a `submit` step ever
    appears here, a model's token could send an application nobody read.
    """
    for forbidden in ("submit", "apply", "send", "send_outreach", "confirm"):
        assert forbidden not in sup.STEPS


def test_the_steps_are_exactly_what_the_prompt_offers():
    """A step the prompt describes but the graph lacks becomes an invented step at
    runtime; a step the graph has but the prompt omits is dead code."""
    described = {
        line.strip().split(maxsplit=1)[0]
        for line in sup.DECIDE_SYSTEM.splitlines()
        if line.startswith("  ") and "—" in line
    }
    assert described == set(sup.STEPS)


# ─── Routing ────────────────────────────────────────────────────────────────


def test_routing_honours_the_step_limit_even_if_a_step_was_chosen():
    """Checked in the router as well as in `decide`, because a node ignoring its own
    limit is precisely the failure a supervisor can produce."""
    assert sup.route({"next_step": "discover", "steps_taken": 8, "max_steps": 8}) == "report"


def test_routing_stops_on_an_error():
    assert sup.route({"next_step": "discover", "error": "no candidate"}) == "report"


def test_routing_stops_when_done():
    assert sup.route({"next_step": "discover", "done": True}) == "report"


def test_an_unknown_step_routes_to_report_rather_than_being_improvised():
    assert sup.route({"next_step": "hack_the_ats", "steps_taken": 0, "max_steps": 8}) == "report"


@pytest.mark.parametrize("step", sup.STEPS)
def test_every_valid_step_routes_to_itself(step):
    assert sup.route({"next_step": step, "steps_taken": 0, "max_steps": 8}) == step


# ─── Being stuck is detected, not endured ───────────────────────────────────


def test_the_same_step_with_the_same_input_is_stuck():
    history = [{"step": "queue", "input": {"job_ids": ["j1"]}} for _ in range(sup.STUCK_AFTER)]
    assert sup._is_stuck({"history": history}, "queue", {"job_ids": ["j1"]})


def test_the_same_step_with_different_input_is_progress():
    """Researching three different companies is work. Researching one three times is not."""
    history = [{"step": "research", "input": {"company": name}} for name in ("A", "B", "C")]
    assert not sup._is_stuck({"history": history}, "research", {"company": "D"})


def test_input_order_does_not_disguise_a_repeat():
    """Key order in a model's JSON is not stable, so the signature is order-independent."""
    history = [{"step": "queue", "input": {"a": 1, "b": 2}} for _ in range(sup.STUCK_AFTER)]
    assert sup._is_stuck({"history": history}, "queue", {"b": 2, "a": 1})


# ─── The fallback is a working path, not an error path ──────────────────────


def test_with_no_model_the_supervisor_follows_a_fixed_order():
    state: dict = {"history": []}
    order = []
    for _ in range(5):
        step, reason = sup._fallback_step(state)
        order.append(step)
        assert "no model" in reason or "fixed order" in reason
        state["history"].append({"step": step, "input": {}})
    assert order == ["discover", "evaluate", "queue", "report", "report"]


def test_the_fallback_never_invents_a_step():
    state: dict = {"history": [{"step": s, "input": {}} for s in sup.STEPS]}
    step, _ = sup._fallback_step(state)
    assert step in sup.STEPS


# ─── Honest endings ─────────────────────────────────────────────────────────


def test_every_stop_reason_has_a_plain_language_explanation():
    """A raw enum in a user-facing summary is a shrug. These are the strings that can
    appear in `stopped_because`."""
    produced = {
        "goal_reached",
        "step_limit",
        "stuck",
        "invalid_step",
        "no_candidate",
        "no_model_available",
        "model_failed",
        "budget_exhausted",
    }
    assert produced <= set(sup.EXPLAIN)


def test_only_one_ending_claims_success():
    """The bug this pins: a run that stopped on a rate limit reported `goal_reached`, so
    the summary told the user the goal was met when the supervisor had simply stopped being
    able to choose a step."""
    claims_success = [
        key
        for key, text in sup.EXPLAIN.items()
        if "nothing useful left" in text or "reached the goal" in text
    ]
    assert claims_success == ["goal_reached"]


async def test_a_failing_model_is_reported_as_a_failing_model():
    """`decide` must set `stopped_because` when the model could not answer at all, rather
    than letting the report default to success."""
    from careeragent.llm import toolloop

    async def unavailable(**_kwargs):
        return toolloop.AgentRun(stopped=toolloop.NO_MODEL)

    original = toolloop.run
    toolloop.run = unavailable  # type: ignore[assignment]
    try:
        # A history in which every fixed-order step has run, so the fallback picks `report`.
        state = {
            "goal": "x",
            "steps_taken": 3,
            "max_steps": 8,
            "history": [{"step": s, "input": {}} for s in ("discover", "evaluate", "queue")],
        }
        update = await sup.decide_agent(state)  # type: ignore[arg-type]
    finally:
        toolloop.run = original  # type: ignore[assignment]

    assert update["next_step"] == "report"
    assert update["stopped_because"] == "no_model_available"
    assert sup.EXPLAIN[update["stopped_because"]]


async def test_the_step_limit_ends_the_run_before_asking_a_model():
    """Asking costs money. A run already at its limit must not pay to be told to stop."""
    from careeragent.llm import toolloop

    called = False

    async def should_not_run(**_kwargs):
        nonlocal called
        called = True
        return toolloop.AgentRun()

    original = toolloop.run
    toolloop.run = should_not_run  # type: ignore[assignment]
    try:
        update = await sup.decide_agent({"steps_taken": 5, "max_steps": 5})  # type: ignore[arg-type]
    finally:
        toolloop.run = original  # type: ignore[assignment]

    assert update["done"] is True
    assert update["stopped_because"] == "step_limit"
    assert not called, "the supervisor paid for a model call it did not need"


# ─── Bookkeeping the limits depend on ───────────────────────────────────────


def test_every_step_contributes_exactly_one_to_the_counter():
    """`steps_taken` sums across updates. A step returning its own dict without going
    through `_step_done` stops the counter, and then `max_steps` means nothing and the run
    only ends at the recursion limit — which is what happened the first time."""
    update = sup._step_done("evaluate", {}, {"scored": 3})
    assert update["steps_taken"] == 1
    assert update["history"][0]["step"] == "evaluate"


def test_the_counter_field_actually_sums():
    """Asserted on the annotation, because the reducer is the mechanism."""
    import operator
    from typing import get_args, get_type_hints

    from careeragent.graph.state import SupervisorState

    hints = get_type_hints(SupervisorState, include_extras=True)
    assert operator.add in get_args(hints["steps_taken"])


# ─── The graph is wired as a loop, not a line ──────────────────────────────


def test_every_step_except_report_returns_to_the_decider():
    """This is what makes the order emergent. A step wired straight to END would make the
    supervisor a one-shot router."""
    graph = sup.build_supervisor_graph()
    drawn = graph.get_graph()
    edges = {(e.source, e.target) for e in drawn.edges}

    for step in sup.STEPS:
        if step == "report":
            continue
        assert (step, "decide") in edges, f"{step} does not report back to the supervisor"
    assert any(target == "__end__" for source, target in edges if source == "report")


def test_the_supervisors_toolset_cannot_produce_a_document_or_touch_a_form():
    from careeragent import tools

    names = {t.name for t in tools.toolset("supervisor")}
    # It routes work to specialists; it does not do the work.
    assert not names & {"check_claims_supported", "recall_answer_to_question"}
    # And nothing it holds can reach outside this machine unattended.
    assert all(t.effect is not tools.Effect.EXTERNAL for t in tools.toolset("supervisor"))
