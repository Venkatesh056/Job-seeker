"""The tool-calling loop: the runtime that makes an agent an agent.

`router.complete()` asks a model one question and takes one answer. That is enough for
extraction and scoring, where the input is already assembled. It is not enough for the
thing the spec actually asks for: an agent that decides *what it needs to know* and goes
and finds out.

This is that loop, and it is deliberately small:

    build messages → ask the model → if it called tools, run them → tell it what happened
                   → ask again → … → return its final answer and what it did

**Four limits, each because of a specific way this goes wrong.**

  * `max_turns` — a model that cannot answer will keep calling tools. Without a cap, one
    application spends the whole daily budget.
  * `max_tool_calls` — the same failure inside a single turn, when a model fans out.
  * The budget is re-checked *every turn*, not once at the start. A loop authorised when
    it began can exhaust the ceiling halfway through.
  * A tool failure is fed back as text, never raised. The model is expected to read "that
    job id does not exist" and try a different one, which is the entire reason this is
    better than a fixed pipeline rather than merely more expensive.

**What comes back is a verdict plus a receipt.** `AgentRun` carries the text, the tools
that were called, the cost, and why the loop stopped. The tool log is the auditable part:
a model's reasoning is not evidence, but "it called `check_claims_supported` and then
rewrote its answer" is.

**This does not cache.** `complete()` caches on a content hash, which is right for a single
question with a deterministic answer. A multi-turn loop over live data is not that, and
serving a cached tool conversation would replay yesterday's job board as today's.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from ..logging import get_logger
from ..tools import ToolLog, invoke, specs_for
from .router import LLMResult, LLMRouter, Tier, get_router

log = get_logger("llm.toolloop")

MAX_TURNS = 6
MAX_TOOL_CALLS = 12
# Long enough for a model to reason over a job description and several tool results.
TURN_TIMEOUT_S = 90


class Stop(str):
    """Why the loop ended. A plain string subclass so it logs and serialises cleanly."""


ANSWERED = Stop("answered")
TURN_LIMIT = Stop("turn_limit")
CALL_LIMIT = Stop("tool_call_limit")
NO_MODEL = Stop("no_model_available")
BUDGET = Stop("budget_exhausted")
FAILED = Stop("model_failed")


@dataclass
class AgentRun:
    """What an agent produced, and what it did to get there."""

    text: str = ""
    stopped: str = ANSWERED
    turns: int = 0
    tools: ToolLog = field(default_factory=ToolLog)
    cost_usd: float = 0.0
    model: str = ""
    prompt_version: str | None = None
    # True when the answer came from a retry with no tools attached, because every model
    # refused the tool-calling request. The answer is then reasoning without evidence, and
    # a caller weighing confidence should know that.
    tools_unavailable: bool = False

    @property
    def ok(self) -> bool:
        """Whether there is an answer worth using.

        A run that hit a limit may still have produced text on an earlier turn, but that
        text was written before the model finished gathering evidence — using it would be
        using a half-formed answer as a finished one.
        """
        return bool(self.text) and self.stopped == ANSWERED

    def json(self) -> Any | None:
        """The answer parsed as JSON, or None.

        Models wrap JSON in prose even when told not to, so this reuses the router's
        extractor rather than a bare `json.loads`.
        """
        from .router import extract_json

        return extract_json(self.text) if self.text else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "stopped": str(self.stopped),
            "turns": self.turns,
            "tool_calls": len(self.tools),
            "tools_used": self.tools.names(),
            "tool_failures": self.tools.failures(),
            "cost_usd": round(self.cost_usd, 6),
            "model": self.model,
            "prompt_version": self.prompt_version,
            "tools_unavailable": self.tools_unavailable,
        }


def _tool_calls_of(message: Any) -> list[Any]:
    """Pull tool calls off a response message across provider shapes.

    litellm normalises most of this, but a model that emits no tool calls sets the
    attribute to None on some providers and omits it on others.
    """
    calls = getattr(message, "tool_calls", None)
    return list(calls) if calls else []


def _arguments_of(call: Any) -> dict[str, Any]:
    """Parse one tool call's arguments.

    Malformed JSON here is common — a truncated response, a trailing comma — and it must
    become a message the model can correct, not an exception.
    """
    raw = getattr(getattr(call, "function", None), "arguments", "") or "{}"
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {"__unparseable__": str(raw)[:200]}
    return parsed if isinstance(parsed, dict) else {"__not_an_object__": str(parsed)[:200]}


async def _answer_without_tools(
    *,
    agent: str,
    models: list[str],
    messages: list[dict[str, Any]],
    temperature: float,
    max_tokens: int | None,
    router: LLMRouter,
    tier: Tier,
    result: AgentRun,
) -> AgentRun:
    """One last attempt, with no tools attached.

    Only the original system and user messages are sent: a half-finished tool conversation
    is what the provider objected to, and replaying it would fail the same way.

    The answer that comes back is reasoning without evidence. `tools_unavailable` says so on
    the receipt, and it is the caller's business whether that is good enough — for a
    supervisor choosing a step it usually is, for anything that writes a claim about the
    candidate it is not.
    """
    import litellm

    for model in models:
        if not router.budget.allows():
            result.stopped = BUDGET
            return result
        try:
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens or 2000,
                timeout=TURN_TIMEOUT_S,
            )
        except Exception as exc:
            log.debug(
                "toolless_retry_failed",
                agent=agent,
                model=model,
                error=f"{type(exc).__name__}"[:80],
            )
            continue

        cost = float(getattr(response, "_hidden_params", {}).get("response_cost") or 0.0)
        router.budget.record(cost)
        result.cost_usd += cost
        usage = getattr(response, "usage", None)
        router._calls.append(
            LLMResult(
                text="",
                model=model,
                tier=tier,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                cost_usd=cost,
                agent=agent,
            )
        )

        text = (getattr(response.choices[0].message, "content", "") or "").strip()
        if not text:
            continue

        result.text = text
        result.model = model
        result.stopped = ANSWERED
        result.tools_unavailable = True
        log.info("toolloop_answered_without_tools", agent=agent, model=model)
        return result

    result.stopped = FAILED
    return result


async def run(
    *,
    agent: str,
    toolset: str,
    system: str,
    prompt: str,
    tier: Tier = Tier.STRONG,
    temperature: float = 0.2,
    max_turns: int = MAX_TURNS,
    max_tool_calls: int = MAX_TOOL_CALLS,
    max_tokens: int | None = None,
    router: LLMRouter | None = None,
    prompt_version: str | None = None,
) -> AgentRun:
    """Run one tool-using agent to a final answer.

    `toolset` names the agent's permission boundary — see `tools.TOOLSETS`. An unknown name
    grants no tools, which degrades the agent to a single-shot completion rather than
    handing it everything.

    Returns an `AgentRun` in every case, including when no model is configured. Callers are
    expected to check `ok` and fall back to their deterministic path; that is the hybrid
    pattern, and it is why nothing here raises.
    """
    router = router or get_router()
    result = AgentRun(prompt_version=prompt_version)

    specs = specs_for(toolset)
    chain = router.chain(tier)
    if not chain:
        result.stopped = NO_MODEL
        log.debug("toolloop_no_model", agent=agent, tier=tier.value)
        return result

    # The whole chain, not just the first model. `router.complete()` has always fallen
    # through to the next one; this loop did not, so a Gemini rate limit sent the supervisor
    # to its fixed order while a working Groq model sat unused two entries down.
    models = list(chain)
    model_index = 0
    model = models[0]
    result.model = model
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": prompt},
    ]

    import litellm

    litellm.suppress_debug_info = True

    turn = 0
    # Bounded independently of `max_turns`: a failing model retries the same turn, so the
    # loop needs its own ceiling or a provider failing on every model could spin.
    attempts = 0
    while turn < max_turns and attempts < max_turns + len(models):
        attempts += 1
        turn += 1
        result.turns = turn

        # Checked every turn. A loop authorised when it started can exhaust the ceiling
        # halfway through, and the turn that tips it over is the expensive one.
        if not router.budget.allows():
            result.stopped = BUDGET
            log.warning(
                "toolloop_budget_exhausted",
                agent=agent,
                turn=turn,
                spent=round(router.budget.spent, 4),
            )
            return result

        try:
            response = await litellm.acompletion(
                model=model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens or 2000,
                timeout=TURN_TIMEOUT_S,
                **({"tools": specs, "tool_choice": "auto"} if specs else {}),
            )
        except Exception as exc:
            log.warning(
                "toolloop_model_failed",
                agent=agent,
                model=model,
                turn=turn,
                error=f"{type(exc).__name__}: {exc}".splitlines()[0][:160],
            )
            model_index += 1
            if model_index >= len(models):
                # Every model refused. Before giving up, ask once with no tools attached:
                # some models (Groq's gpt-oss family) reject a tool-calling request while
                # still being perfectly able to reason and answer. Losing the lookups is a
                # real loss; losing the whole answer as well is an avoidable one.
                if specs:
                    return await _answer_without_tools(
                        agent=agent,
                        models=models,
                        messages=messages[:2],
                        temperature=temperature,
                        max_tokens=max_tokens,
                        router=router,
                        tier=tier,
                        result=result,
                    )
                result.stopped = FAILED
                return result
            # Retry the same turn on the next model. The conversation so far carries over:
            # litellm normalises tool calls and results across providers, so the messages
            # are not tied to the model that produced them.
            model = models[model_index]
            result.model = model
            log.info("toolloop_next_model", agent=agent, model=model, turn=turn)
            # The turn did not happen — the model never answered — so it is given back.
            turn -= 1
            continue

        cost = float(getattr(response, "_hidden_params", {}).get("response_cost") or 0.0)
        router.budget.record(cost)
        result.cost_usd += cost
        # Recorded on the router so `/api/v1/analytics/costs` accounts for agent turns the
        # same way it accounts for single completions. A loop that spent money invisibly
        # was the bug that made the cost page say "no LLM calls recorded".
        usage = getattr(response, "usage", None)
        router._calls.append(
            LLMResult(
                text="",
                model=model,
                tier=tier,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                cost_usd=cost,
                agent=agent,
            )
        )

        message = response.choices[0].message
        calls = _tool_calls_of(message)

        if not calls:
            result.text = (getattr(message, "content", "") or "").strip()
            result.stopped = ANSWERED
            log.info(
                "toolloop_answered",
                agent=agent,
                turns=turn,
                tool_calls=len(result.tools),
                cost_usd=round(result.cost_usd, 6),
            )
            return result

        if len(result.tools) + len(calls) > max_tool_calls:
            result.stopped = CALL_LIMIT
            log.warning(
                "toolloop_tool_call_limit",
                agent=agent,
                turn=turn,
                already=len(result.tools),
                wanted=len(calls),
            )
            return result

        # The assistant's tool-call turn has to go back in verbatim, or the tool results
        # below have nothing to attach to and the provider rejects the next request.
        messages.append(message.model_dump() if hasattr(message, "model_dump") else dict(message))

        for call in calls:
            name = getattr(getattr(call, "function", None), "name", "") or ""
            arguments = _arguments_of(call)
            # `approved` is never set here. Approval comes from a human click threaded
            # through the graph, and an agent loop is not a human.
            outcome = await invoke(name, arguments)
            result.tools.record(name, arguments, outcome)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": getattr(call, "id", "") or name,
                    "name": name,
                    # Failures go back as content, not as an exception: the model should
                    # read "no job with that id" and try another one.
                    "content": outcome.for_model(),
                }
            )

    result.stopped = TURN_LIMIT
    log.warning(
        "toolloop_turn_limit", agent=agent, turns=result.turns, tool_calls=len(result.tools)
    )
    return result
