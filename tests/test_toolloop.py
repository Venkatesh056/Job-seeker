"""The tool-calling loop — the runtime that makes an agent an agent.

Tested against a fake provider rather than a live model, because the behaviour that matters
here is not what a model says — it is what the loop does when a model misbehaves. Every test
below is a specific way that happens:

  * it calls tools forever without answering,
  * it fans out twenty calls in one turn,
  * it emits malformed arguments,
  * it names a tool that does not exist,
  * it rate-limits halfway through,
  * the budget runs out mid-conversation.

Each of those has a defined outcome, and each of those outcomes is the difference between a
degraded run and a runaway bill.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from careeragent.llm import toolloop
from careeragent.llm.router import Tier

# ─── A fake provider ────────────────────────────────────────────────────────


def _message(content: str = "", tool_calls: list | None = None):
    """A response message shaped like the one litellm returns."""
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls,
        model_dump=lambda: {
            "role": "assistant",
            "content": content,
            "tool_calls": [
                {
                    "id": c.id,
                    "type": "function",
                    "function": {"name": c.function.name, "arguments": c.function.arguments},
                }
                for c in (tool_calls or [])
            ],
        },
    )


def _call(name: str, arguments: dict | str, call_id: str = "c1"):
    return SimpleNamespace(
        id=call_id,
        function=SimpleNamespace(
            name=name,
            arguments=arguments if isinstance(arguments, str) else json.dumps(arguments),
        ),
    )


def _response(message, cost: float = 0.001):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message)],
        usage=SimpleNamespace(prompt_tokens=100, completion_tokens=20),
        _hidden_params={"response_cost": cost},
    )


class FakeRouter:
    """Just enough router for the loop: a chain, a budget, and a call list."""

    def __init__(self, models: list[str], limit: float = 10.0) -> None:
        self._models = models
        self._calls: list = []
        self.budget = SimpleNamespace(
            spent=0.0,
            limit=limit,
            allows=lambda: self.budget.spent < self.budget.limit,
            record=lambda cost: setattr(self.budget, "spent", self.budget.spent + cost),
        )

    def chain(self, _tier):
        return list(self._models)


@pytest.fixture
def fake_litellm(monkeypatch):
    """Replace `litellm.acompletion` with a scripted sequence.

    Returns a recorder so a test can assert on how many calls were made and what messages
    the loop actually sent — the message history is where the subtle bugs live.
    """
    import litellm

    class Recorder:
        def __init__(self) -> None:
            self.script: list = []
            self.calls: list[dict] = []

        def responds(self, *items):
            self.script = list(items)
            return self

        async def acompletion(self, **kwargs):
            self.calls.append(kwargs)
            if not self.script:
                return _response(_message("no script left"))
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item

    recorder = Recorder()
    monkeypatch.setattr(litellm, "acompletion", recorder.acompletion)
    return recorder


async def _run(recorder, router=None, **kwargs):
    return await toolloop.run(
        agent="test_agent",
        toolset=kwargs.pop("toolset", "opportunity"),
        system="be useful",
        prompt="do the thing",
        tier=Tier.STRONG,
        router=router or FakeRouter(["fake/model-a"]),
        **kwargs,
    )


# ─── The happy path ─────────────────────────────────────────────────────────


async def test_a_plain_answer_ends_the_loop(fake_litellm):
    fake_litellm.responds(_response(_message('{"decision": "apply"}')))
    run = await _run(fake_litellm)

    assert run.ok
    assert run.turns == 1
    assert len(run.tools) == 0
    assert run.json() == {"decision": "apply"}


async def test_a_tool_call_is_executed_and_fed_back(fake_litellm):
    fake_litellm.responds(
        _response(
            _message(tool_calls=[_call("resolve_job_family", {"title": "Site Civil Engineer"})])
        ),
        _response(_message('{"family": "civil_engineering"}')),
    )
    run = await _run(fake_litellm, toolset="discovery")

    assert run.ok
    assert run.turns == 2
    assert run.tools.names() == ["resolve_job_family"]

    # The second request must carry the assistant's tool-call turn AND the tool result, or
    # the provider rejects it and the model has no idea what came back.
    roles = [
        m["role"] if isinstance(m, dict) else m.get("role")
        for m in fake_litellm.calls[1]["messages"]
    ]
    assert roles == ["system", "user", "assistant", "tool"]
    tool_message = fake_litellm.calls[1]["messages"][3]
    assert "civil_engineering" in tool_message["content"]


async def test_the_agents_toolset_is_what_gets_offered(fake_litellm):
    fake_litellm.responds(_response(_message("done")))
    await _run(fake_litellm, toolset="resume")

    offered = {t["function"]["name"] for t in fake_litellm.calls[0]["tools"]}
    assert offered == {"read_candidate_facts", "check_claims_supported", "resolve_job_family"}


async def test_an_agent_with_no_toolset_still_runs(fake_litellm):
    """An unknown toolset grants nothing, and the agent degrades to a single completion
    rather than being handed every tool in the system."""
    fake_litellm.responds(_response(_message("answered without tools")))
    run = await _run(fake_litellm, toolset="no_such_agent")

    assert run.ok
    assert "tools" not in fake_litellm.calls[0]


# ─── Runaway protection ─────────────────────────────────────────────────────


async def test_a_model_that_never_answers_hits_the_turn_limit(fake_litellm):
    """The expensive failure: without this cap one application spends the daily budget."""
    forever = [
        _response(_message(tool_calls=[_call("resolve_job_family", {"title": "x"}, f"c{i}")]))
        for i in range(20)
    ]
    fake_litellm.responds(*forever)
    run = await _run(fake_litellm, toolset="discovery", max_turns=3)

    assert not run.ok
    assert run.stopped == toolloop.TURN_LIMIT
    assert run.turns == 3


async def test_a_fan_out_hits_the_tool_call_limit(fake_litellm):
    """The same failure inside one turn."""
    many = [_call("resolve_job_family", {"title": f"t{i}"}, f"c{i}") for i in range(10)]
    fake_litellm.responds(_response(_message(tool_calls=many)))
    run = await _run(fake_litellm, toolset="discovery", max_tool_calls=4)

    assert not run.ok
    assert run.stopped == toolloop.CALL_LIMIT
    # None of them ran: the limit is checked before executing, not after.
    assert len(run.tools) == 0


async def test_the_budget_is_rechecked_every_turn(fake_litellm):
    """A loop authorised when it began can exhaust the ceiling halfway through, and the
    turn that tips it over is the expensive one."""
    router = FakeRouter(["fake/model-a"], limit=0.0015)
    fake_litellm.responds(
        _response(_message(tool_calls=[_call("resolve_job_family", {"title": "x"})]), cost=0.001),
        _response(_message(tool_calls=[_call("resolve_job_family", {"title": "y"})]), cost=0.001),
        _response(_message("would have answered")),
    )
    run = await _run(fake_litellm, router=router, toolset="discovery", max_turns=5)

    assert not run.ok
    assert run.stopped == toolloop.BUDGET
    assert run.turns < 5


async def test_with_no_model_it_returns_rather_than_raising(fake_litellm):
    run = await _run(fake_litellm, router=FakeRouter([]))
    assert not run.ok
    assert run.stopped == toolloop.NO_MODEL
    assert fake_litellm.calls == []


# ─── Model failures ─────────────────────────────────────────────────────────


async def test_a_failing_model_falls_through_to_the_next_in_the_chain(fake_litellm):
    """The bug this pins: the loop only ever tried the first model, so a Gemini rate limit
    sent the supervisor to its fixed order while a working Groq model sat unused."""
    fake_litellm.responds(
        RuntimeError("RateLimitError: quota"),
        _response(_message('{"ok": true}')),
    )
    router = FakeRouter(["fake/model-a", "fake/model-b"])
    run = await _run(fake_litellm, router=router)

    assert run.ok
    assert run.model == "fake/model-b"
    # The failed attempt did not consume a turn — the model never answered.
    assert run.turns == 1
    assert fake_litellm.calls[0]["model"] == "fake/model-a"
    assert fake_litellm.calls[1]["model"] == "fake/model-b"


async def test_when_every_model_refuses_tools_it_retries_without_them(fake_litellm):
    """Groq's gpt-oss models reject a tool-calling request outright — they try to deliver
    their answer *as* a tool call named `json` and the provider refuses it.

    Such a model can still reason and still answer; what it loses is the ability to look
    anything up. Giving up on the whole answer as well was an avoidable second loss.
    """
    fake_litellm.responds(
        RuntimeError("tool call validation failed"),
        RuntimeError("tool call validation failed"),
        _response(_message('{"step": "evaluate"}')),
    )
    run = await _run(fake_litellm, router=FakeRouter(["fake/a", "fake/b"]), toolset="discovery")

    assert run.ok
    assert run.json() == {"step": "evaluate"}
    # Flagged, because this answer is reasoning with no evidence behind it and a caller
    # weighing confidence needs to know the difference.
    assert run.tools_unavailable is True
    assert len(run.tools) == 0
    # The retry sends the original two messages only: replaying a half-finished tool
    # conversation is what the provider objected to.
    assert len(fake_litellm.calls[-1]["messages"]) == 2
    assert "tools" not in fake_litellm.calls[-1]


async def test_when_even_the_toolless_retry_fails_the_run_fails_cleanly(fake_litellm):
    fake_litellm.responds(*[RuntimeError("down") for _ in range(6)])
    run = await _run(fake_litellm, router=FakeRouter(["fake/a", "fake/b"]), toolset="discovery")

    assert not run.ok
    assert run.stopped == toolloop.FAILED
    assert run.tools_unavailable is False


async def test_an_agent_with_no_tools_does_not_get_a_toolless_retry(fake_litellm):
    """There would be nothing to retry — the first attempt already had no tools."""
    fake_litellm.responds(RuntimeError("down"))
    run = await _run(fake_litellm, router=FakeRouter(["fake/a"]), toolset="no_such_agent")

    assert run.stopped == toolloop.FAILED
    assert len(fake_litellm.calls) == 1


async def test_a_failing_provider_cannot_spin_the_loop(fake_litellm):
    """A model that fails on every attempt must not retry indefinitely."""
    fake_litellm.responds(*[RuntimeError("down") for _ in range(50)])
    run = await _run(fake_litellm, router=FakeRouter(["a", "b", "c"]), max_turns=4)

    assert run.stopped == toolloop.FAILED
    assert len(fake_litellm.calls) <= 4 + 3, "the loop retried more than its own ceiling"


# ─── Malformed model output ─────────────────────────────────────────────────


async def test_unparseable_arguments_become_a_message_the_model_can_correct(fake_litellm):
    """A truncated or malformed arguments blob is common. It must not raise."""
    fake_litellm.responds(
        _response(_message(tool_calls=[_call("resolve_job_family", '{"title": "x"')])),
        _response(_message("recovered")),
    )
    run = await _run(fake_litellm, toolset="discovery")

    assert run.ok
    failures = run.tools.failures()
    assert len(failures) == 1
    assert failures[0]["error"] == "bad_arguments"
    # And the model was told, rather than the loop dying.
    assert "bad_arguments" in fake_litellm.calls[1]["messages"][3]["content"]


async def test_a_tool_that_does_not_exist_is_reported_back(fake_litellm):
    fake_litellm.responds(
        _response(_message(tool_calls=[_call("submit_application", {"job_id": "j1"})])),
        _response(_message("understood")),
    )
    run = await _run(fake_litellm, toolset="discovery")

    assert run.ok
    assert run.tools.failures()[0]["error"] == "not_found"


async def test_a_tool_outside_the_agents_toolset_is_still_refused_if_it_does_not_exist(
    fake_litellm,
):
    """The toolset limits what is *offered*. A model naming something else gets nowhere."""
    fake_litellm.responds(
        _response(_message(tool_calls=[_call("run_command", {"cmd": "rm -rf /"})])),
        _response(_message("no")),
    )
    run = await _run(fake_litellm, toolset="resume")

    assert run.tools.failures()[0]["error"] == "not_found"
    assert run.tools.names() == ["run_command"]


async def test_prose_wrapped_json_is_still_parsed(fake_litellm):
    """Models fence their JSON even when told not to."""
    fake_litellm.responds(_response(_message('```json\n{"decision": "skip"}\n```')))
    run = await _run(fake_litellm)
    assert run.json() == {"decision": "skip"}


async def test_an_empty_answer_is_not_ok(fake_litellm):
    fake_litellm.responds(_response(_message("")))
    run = await _run(fake_litellm)
    assert not run.ok
    assert run.json() is None


# ─── Accounting and the receipt ─────────────────────────────────────────────


async def test_every_turn_is_recorded_on_the_router(fake_litellm):
    """A loop that spent money invisibly is the bug that made the cost page say
    "no LLM calls recorded" after a real spend."""
    router = FakeRouter(["fake/model-a"])
    fake_litellm.responds(
        _response(_message(tool_calls=[_call("resolve_job_family", {"title": "x"})]), cost=0.002),
        _response(_message("done"), cost=0.003),
    )
    run = await _run(fake_litellm, router=router, toolset="discovery")

    assert len(router._calls) == 2
    assert run.cost_usd == pytest.approx(0.005)
    assert router.budget.spent == pytest.approx(0.005)
    assert all(c.agent == "test_agent" for c in router._calls)


async def test_the_receipt_says_what_happened(fake_litellm):
    fake_litellm.responds(
        _response(_message(tool_calls=[_call("resolve_job_family", {"title": "x"})])),
        _response(_message("done")),
    )
    run = await _run(fake_litellm, toolset="discovery")
    receipt = run.to_dict()

    assert receipt["ok"] is True
    assert receipt["turns"] == 2
    assert receipt["tools_used"] == ["resolve_job_family"]
    assert receipt["stopped"] == "answered"
    assert receipt["model"] == "fake/model-a"


async def test_a_limited_run_is_not_ok_even_with_text(fake_litellm):
    """Text written before the model finished gathering evidence is a half-formed answer,
    and using it as a finished one is how a partial result gets presented as a conclusion."""
    run = toolloop.AgentRun(text="I think maybe", stopped=toolloop.TURN_LIMIT)
    assert not run.ok


async def test_approval_is_never_granted_from_inside_the_loop(fake_litellm, monkeypatch):
    """An agent loop is not a human. Nothing a model emits can set `approved`."""
    seen: list[bool] = []

    async def spy(name, arguments=None, *, approved=False):
        seen.append(approved)
        from careeragent.tools.base import ToolResult

        return ToolResult.success(name, {})

    monkeypatch.setattr(toolloop, "invoke", spy)
    fake_litellm.responds(
        _response(_message(tool_calls=[_call("resolve_job_family", {"title": "x"})])),
        _response(_message("done")),
    )
    await _run(fake_litellm, toolset="discovery")

    assert seen == [False]
