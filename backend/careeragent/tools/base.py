"""The tool layer — spec §12.

An agent that can reason but not act is a text generator. An agent that can act without
constraint is a liability. This module is the boundary between the two, and its design
rests on one idea:

    **An agent can do exactly what a tool lets it do, and nothing else.**

Not "the prompt tells it not to". A prompt is a request; a missing tool is a wall. So the
security properties the spec asks for are enforced by the *shape of this registry* rather
than by instructions a model may or may not follow:

  * There is no tool that submits an application. Submission sits behind the graph's
    `interrupt()`, where a human clicks it.
  * There is no tool that sends a message. Outreach drafts; a person sends.
  * There is no tool that writes to the Fact Bank. An agent that could edit the candidate's
    facts could fabricate by editing its own evidence, and every downstream validator
    would then agree with it.
  * There is no tool that fetches an arbitrary URL, runs a command, or reads a file.
  * There is no tool that touches a CAPTCHA, an MFA prompt or a login form.

`tests/test_tools.py` asserts those absences by name, so re-adding one is a visible,
deliberate act rather than a convenience someone reaches for at 2am.

**Effects are declared, not inferred.** Every tool states whether it only reads, writes to
our own database, or reaches the outside world. `EXTERNAL` tools are the ones that can be
noticed by somebody else, and the registry refuses to hold one that does not require
approval — a check that runs at import time, so the process will not start if it is wrong.

**Results are values, never exceptions.** A tool that raises inside an agent loop either
kills the run or gets swallowed into "something went wrong". `ToolResult` carries failure
as data so the agent can read it, reason about it, and try something else — which is the
behaviour that makes a tool-using agent better than a fixed pipeline rather than worse.
"""

from __future__ import annotations

import asyncio
import enum
import inspect
import json
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from ..logging import get_logger

log = get_logger("tools")

# A tool that has not answered in this long is not going to. Agents wait on these inside a
# graph node, so an unbounded call would hang a whole application run.
DEFAULT_TIMEOUT_S = 20.0

# Enough for an agent to work with, small enough that twenty tool calls do not blow the
# context window and crowd out the candidate's own facts.
MAX_RESULT_CHARS = 6_000


class Effect(enum.StrEnum):
    """What calling this tool can change.

    The distinction that matters is the third one: `WRITE` is undoable by us, `EXTERNAL`
    has been seen by someone who is not us.
    """

    READ = "read"
    """Reads data or fetches a public page. Changes nothing."""

    WRITE = "write"
    """Changes our own database. Reversible, private, ours."""

    EXTERNAL = "external"
    """Visible outside this machine and not reliably undoable. Always needs approval."""


class ToolError(enum.StrEnum):
    """Why a call failed, in a form an agent can branch on."""

    BAD_ARGUMENTS = "bad_arguments"
    NOT_FOUND = "not_found"
    TIMEOUT = "timeout"
    UNAVAILABLE = "unavailable"
    REFUSED = "refused"
    FAILED = "failed"


class ToolFailure(Exception):
    """Raised by a handler to fail the call with a specific reason.

    Without this, a handler signals "no such job" by returning `{"error": ...}` and the
    result still says `ok: true` — so an agent reads a failure as data and carries on with
    an error message where it expected a score. Raising keeps the two apart.
    """

    def __init__(self, error: ToolError, detail: str = "") -> None:
        super().__init__(detail or error.value)
        self.error = error
        self.detail = detail


@dataclass
class ToolResult:
    """The outcome of one tool call.

    `ok=False` is a normal outcome. Agents are expected to read `error` and `detail` and
    decide what to do, which is why the failure taxonomy above is small and meaningful
    rather than a stringly-typed message.
    """

    tool: str
    ok: bool
    data: Any = None
    error: ToolError | None = None
    detail: str = ""
    elapsed_ms: int = 0

    @classmethod
    def success(cls, tool: str, data: Any, elapsed_ms: int = 0) -> ToolResult:
        return cls(tool=tool, ok=True, data=data, elapsed_ms=elapsed_ms)

    @classmethod
    def failure(
        cls, tool: str, error: ToolError, detail: str = "", elapsed_ms: int = 0
    ) -> ToolResult:
        return cls(tool=tool, ok=False, error=error, detail=detail[:500], elapsed_ms=elapsed_ms)

    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": self.tool,
            "ok": self.ok,
            "data": self.data,
            "error": self.error.value if self.error else None,
            "detail": self.detail,
            "elapsed_ms": self.elapsed_ms,
        }

    def for_model(self) -> str:
        """What the agent actually reads back.

        Truncated, because a tool that returns a whole job board would push the
        candidate's own facts out of the context window — and those are the thing the
        model must not lose sight of.
        """
        if not self.ok:
            return json.dumps(
                {
                    "ok": False,
                    "error": self.error.value if self.error else "failed",
                    "detail": self.detail,
                }
            )
        try:
            body = json.dumps({"ok": True, "data": self.data}, default=str)
        except (TypeError, ValueError):
            body = json.dumps({"ok": True, "data": str(self.data)})
        if len(body) <= MAX_RESULT_CHARS:
            return body
        return body[:MAX_RESULT_CHARS] + f'… (truncated, {len(body)} chars total)"}}'


Handler = Callable[..., Awaitable[Any]] | Callable[..., Any]


@dataclass(frozen=True)
class Tool:
    """One capability, described well enough for a model to choose it correctly.

    The description is not documentation — it is the only thing the model has when
    deciding whether this tool answers its question. A vague description produces a
    plausible-looking wrong tool call, which is worse than no tool at all.
    """

    name: str
    description: str
    parameters: dict[str, Any]
    handler: Handler
    effect: Effect = Effect.READ
    requires_approval: bool = False
    timeout_s: float = DEFAULT_TIMEOUT_S
    # Why this tool is safe to expose, or what it deliberately cannot reach. Read by
    # `tests/test_tools.py` only in the sense that its absence is a review smell.
    safety_note: str = ""

    @property
    def required(self) -> list[str]:
        return list(self.parameters.get("required") or [])

    def spec(self) -> dict[str, Any]:
        """OpenAI/Anthropic-style function schema, for handing to a model."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description.strip(),
                "parameters": self.parameters,
            },
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description.strip(),
            "effect": self.effect.value,
            "requires_approval": self.requires_approval,
            "parameters": sorted((self.parameters.get("properties") or {}).keys()),
            "required": self.required,
            "safety_note": self.safety_note,
        }


class Registry:
    """The set of tools that exist. Assembled at import, read everywhere else."""

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def add(self, tool: Tool) -> Tool:
        if tool.name in self._tools:
            raise ValueError(f"tool {tool.name!r} is already registered")
        # The invariant, checked where it cannot be skipped. An EXTERNAL tool without
        # approval is the single mistake in this file that could cost the user something
        # irreversible, so the process refuses to start rather than trusting a reviewer.
        if tool.effect is Effect.EXTERNAL and not tool.requires_approval:
            raise ValueError(
                f"tool {tool.name!r} has EXTERNAL effect but does not require approval; "
                "anything visible outside this machine needs a human in front of it"
            )
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def names(self) -> list[str]:
        return sorted(self._tools)

    def all(self) -> list[Tool]:
        return [self._tools[n] for n in self.names()]

    def subset(self, names: list[str]) -> list[Tool]:
        """The tools one agent is allowed, in the order asked for.

        Agents get a subset rather than everything: a model offered thirty tools picks
        worse than one offered the four that fit its job, and an agent that never needs to
        touch the browser should not be able to.
        """
        return [self._tools[n] for n in names if n in self._tools]

    def specs(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        tools = self.subset(names) if names is not None else self.all()
        return [t.spec() for t in tools]

    def read_only(self) -> list[Tool]:
        return [t for t in self.all() if t.effect is Effect.READ]


REGISTRY = Registry()


def tool(
    name: str,
    description: str,
    *,
    parameters: dict[str, Any] | None = None,
    effect: Effect = Effect.READ,
    requires_approval: bool = False,
    timeout_s: float = DEFAULT_TIMEOUT_S,
    safety_note: str = "",
) -> Callable[[Handler], Handler]:
    """Register a function as a tool. Returns it unchanged, so it stays directly callable.

    Keeping the plain function usable matters: these are also the deterministic paths the
    rest of the system calls, and the tool wrapper is an additional way in, not a
    replacement for it. The pure-function safety layer must remain testable without going
    through an agent.
    """

    def decorate(handler: Handler) -> Handler:
        REGISTRY.add(
            Tool(
                name=name,
                description=description,
                parameters=parameters or {"type": "object", "properties": {}},
                handler=handler,
                effect=effect,
                requires_approval=requires_approval,
                timeout_s=timeout_s,
                safety_note=safety_note,
            )
        )
        return handler

    return decorate


def _validate(tool_: Tool, arguments: dict[str, Any]) -> str:
    """Check arguments against the declared schema. Returns "" when they are fine.

    Deliberately shallow: presence, type, and no unexpected keys. A full JSON Schema
    validator would add a dependency to reject things the handlers already handle, and the
    failures that actually happen are a missing required field or a model passing a string
    where a number belongs.
    """
    properties: dict[str, Any] = tool_.parameters.get("properties") or {}
    for key in tool_.required:
        if key not in arguments or arguments[key] in (None, ""):
            return f"missing required argument {key!r}"

    unexpected = sorted(set(arguments) - set(properties))
    if unexpected:
        return f"unexpected argument(s): {', '.join(unexpected)}"

    kinds: dict[str, tuple[type, ...]] = {
        "string": (str,),
        "integer": (int,),
        "number": (int, float),
        "boolean": (bool,),
        "array": (list, tuple),
        "object": (dict,),
    }
    for key, value in arguments.items():
        if value is None:
            continue
        declared = str((properties.get(key) or {}).get("type") or "")
        allowed = kinds.get(declared)
        # `bool` is a subclass of `int`, so a boolean would pass an integer check and
        # silently become 0 or 1 somewhere downstream.
        if allowed and (
            not isinstance(value, allowed) or (declared != "boolean" and isinstance(value, bool))
        ):
            return f"argument {key!r} should be {declared}, got {type(value).__name__}"
        enum_values = (properties.get(key) or {}).get("enum")
        if enum_values and value not in enum_values:
            return f"argument {key!r} must be one of {enum_values}"
    return ""


async def invoke(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    approved: bool = False,
) -> ToolResult:
    """Call one tool by name. Never raises.

    `approved` is the human's click, threaded down from the graph's interrupt. It is a
    parameter rather than ambient state on purpose: nothing an agent produces can set it,
    and there is no configuration that turns it on globally.
    """
    arguments = dict(arguments or {})
    tool_ = REGISTRY.get(name)
    if tool_ is None:
        return ToolResult.failure(
            name,
            ToolError.NOT_FOUND,
            f"no tool named {name!r}; available: {', '.join(REGISTRY.names())}",
        )

    if problem := _validate(tool_, arguments):
        # Returned rather than raised so the agent can correct itself; a malformed call is
        # the most common and most recoverable thing a model does.
        return ToolResult.failure(tool_.name, ToolError.BAD_ARGUMENTS, problem)

    if tool_.requires_approval and not approved:
        return ToolResult.failure(
            tool_.name,
            ToolError.REFUSED,
            f"{tool_.name} needs explicit human approval and none was given",
        )

    started = time.monotonic()

    def elapsed() -> int:
        return int((time.monotonic() - started) * 1000)

    try:
        result = tool_.handler(**arguments)
        if inspect.isawaitable(result):
            result = await asyncio.wait_for(result, timeout=tool_.timeout_s)
    except TimeoutError:
        log.info("tool_timeout", tool=tool_.name, timeout_s=tool_.timeout_s)
        return ToolResult.failure(
            tool_.name,
            ToolError.TIMEOUT,
            f"no answer within {tool_.timeout_s:g}s",
            elapsed(),
        )
    except ToolFailure as exc:
        # The handler's own verdict: a real failure with a reason, not a crash.
        return ToolResult.failure(tool_.name, exc.error, exc.detail, elapsed())
    except TypeError as exc:
        # A signature mismatch the shallow validator did not catch. Note this is caught
        # *after* ToolFailure, which would otherwise be swallowed as a bad-arguments error.
        return ToolResult.failure(tool_.name, ToolError.BAD_ARGUMENTS, str(exc), elapsed())
    except Exception as exc:
        log.warning("tool_failed", tool=tool_.name, error=f"{type(exc).__name__}: {exc}"[:200])
        return ToolResult.failure(
            tool_.name, ToolError.FAILED, f"{type(exc).__name__}: {exc}", elapsed()
        )

    ms = elapsed()
    log.debug("tool_called", tool=tool_.name, effect=tool_.effect.value, elapsed_ms=ms)
    return ToolResult.success(tool_.name, result, ms)


@dataclass
class ToolLog:
    """What an agent actually did, for the decision trail.

    An agent's reasoning is not auditable; its tool calls are. This is the part of a
    hybrid agent's behaviour that can be written down as fact.
    """

    calls: list[dict[str, Any]] = field(default_factory=list)

    def record(self, name: str, arguments: dict[str, Any], result: ToolResult) -> None:
        self.calls.append(
            {
                "tool": name,
                # Arguments are summarised: an agent can pass a whole job description, and
                # the trail should say which tool was used, not duplicate its input.
                "arguments": {k: str(v)[:120] for k, v in (arguments or {}).items()},
                "ok": result.ok,
                "error": result.error.value if result.error else None,
                "elapsed_ms": result.elapsed_ms,
            }
        )

    def names(self) -> list[str]:
        """Unique tool names in call order — what goes in `agent_decision.tools_used`."""
        seen: list[str] = []
        for call in self.calls:
            if call["tool"] not in seen:
                seen.append(call["tool"])
        return seen

    def failures(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if not c["ok"]]

    def __len__(self) -> int:
        return len(self.calls)
