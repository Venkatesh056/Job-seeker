"""Tracing — making an agent's reasoning inspectable after the fact.

This system already records more than most: every LLM call lands in `llm_call` with its
prompt version, model and cost; every provider call lands in `provider_call`; every agent
decision lands in `agent_decision` with the evidence behind it. That is the audit trail, it
is in the database, and it survives a restart.

What it does not give you is the *shape* of a run — which step called which, where the time
went, which branch a supervisor took and why. LangSmith draws that, and wiring it costs
almost nothing because LangChain reads its configuration from the environment.

So this module is small on purpose. It does not wrap anything, buffer anything, or add a
second place where traces could be lost. It sets three environment variables and reports
honestly whether they mean anything.

**Without a key this is a no-op, and says so.** `describe()` returns `enabled: False` rather
than pretending, because a tracing integration that silently does nothing is worse than none
— it makes people stop looking for the reason their traces are missing.
"""

from __future__ import annotations

import os
from typing import Any

from ..config import get_settings
from ..logging import get_logger

log = get_logger("net.tracing")

_configured: bool | None = None


def configure() -> bool:
    """Turn on LangSmith if a key is present. Returns whether it is on.

    Idempotent, and called from the API lifespan and the CLI setup. Setting the variables
    twice is harmless; deciding twice is not, because the second decision would log a second
    time and make the startup output suggest two tracing backends.
    """
    global _configured
    if _configured is not None:
        return _configured

    settings = get_settings()
    key = (getattr(settings, "langsmith_api_key", "") or "").strip()
    if not key:
        _configured = False
        return False

    # LangChain reads these itself. Set rather than passed, because the tracer is
    # constructed deep inside LangGraph where this code has no reference to hand anything to.
    os.environ.setdefault("LANGCHAIN_TRACING_V2", "true")
    os.environ.setdefault("LANGCHAIN_API_KEY", key)
    os.environ.setdefault(
        "LANGCHAIN_PROJECT", getattr(settings, "langsmith_project", "") or "careeragent"
    )
    _configured = True
    # The key itself is never logged, here or anywhere. Only that one was found.
    log.info("tracing_enabled", backend="langsmith", project=os.environ["LANGCHAIN_PROJECT"])
    return True


def describe() -> dict[str, Any]:
    """What tracing is actually doing, for `/api/v1/status`.

    `where` names the durable record even when LangSmith is off, because that is the honest
    answer to "can I see why the agent did that" — the answer is yes, in the database, and
    has been since long before this module existed.
    """
    settings = get_settings()
    enabled = bool((getattr(settings, "langsmith_api_key", "") or "").strip())
    return {
        "enabled": enabled,
        "backend": "langsmith" if enabled else "none",
        "project": (getattr(settings, "langsmith_project", "") or "careeragent")
        if enabled
        else None,
        # True regardless. These tables are the audit trail and they do not depend on any
        # third party being reachable, or on anybody having signed up for anything.
        "durable_record": ["llm_call", "provider_call", "agent_decision", "agent_event"],
    }


def reset() -> None:
    """For tests, and for a settings change that should take effect without a restart."""
    global _configured
    _configured = None
