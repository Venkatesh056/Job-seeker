"""Agent event bus.

Every meaningful agent action emits an event. Events are the single source of
truth for the live dashboard feed, the audit trail, and post-hoc debugging — so
they are append-only and carry structured payloads, never formatted prose.

The bus is deliberately small: an in-process async fan-out with pluggable sinks.
A Postgres sink (durability) and a Redis sink (cross-process SSE) drop in behind
the same `EventSink` protocol without callers changing.
"""

from __future__ import annotations

import asyncio
import contextlib
import enum
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from typing import Any, Protocol

from pydantic import BaseModel, Field

from .logging import get_logger

log = get_logger("events")


class EventType(enum.StrEnum):
    # Discovery
    RUN_STARTED = "RUN_STARTED"
    RUN_FINISHED = "RUN_FINISHED"
    SOURCE_QUERIED = "SOURCE_QUERIED"
    ATS_BOARD_DISCOVERED = "ATS_BOARD_DISCOVERED"
    ATS_BOARD_FAILED = "ATS_BOARD_FAILED"
    JOB_FOUND = "JOB_FOUND"
    JOB_DUPLICATE = "JOB_DUPLICATE"
    # Intelligence
    JOB_ANALYZED = "JOB_ANALYZED"
    JOB_SCORED = "JOB_SCORED"
    JOB_REJECTED = "JOB_REJECTED"
    JOB_SHORTLISTED = "JOB_SHORTLISTED"
    COMPANY_ANALYZED = "COMPANY_ANALYZED"
    # Resume
    RESUME_TAILORING_STARTED = "RESUME_TAILORING_STARTED"
    RESUME_GENERATED = "RESUME_GENERATED"
    RESUME_VALIDATED = "RESUME_VALIDATED"
    EVIDENCE_REJECTED = "EVIDENCE_REJECTED"
    # Application
    APPLICATION_STARTED = "APPLICATION_STARTED"
    ATS_DETECTED = "ATS_DETECTED"
    FORM_FIELD_DETECTED = "FORM_FIELD_DETECTED"
    FORM_FIELD_FILLED = "FORM_FIELD_FILLED"
    PRESUBMIT_HELD = "PRESUBMIT_HELD"
    HUMAN_APPROVAL_REQUIRED = "HUMAN_APPROVAL_REQUIRED"
    APPLICATION_SUBMITTED = "APPLICATION_SUBMITTED"
    APPLICATION_UNVERIFIED = "APPLICATION_UNVERIFIED"
    APPLICATION_FAILED = "APPLICATION_FAILED"
    # Outreach
    OUTREACH_DRAFTED = "OUTREACH_DRAFTED"
    OUTREACH_APPROVED = "OUTREACH_APPROVED"
    OUTREACH_SENT = "OUTREACH_SENT"
    # Outcomes
    INTERVIEW_RECORDED = "INTERVIEW_RECORDED"
    OFFER_RECORDED = "OFFER_RECORDED"
    # Ops
    LLM_CALL = "LLM_CALL"
    BUDGET_EXCEEDED = "BUDGET_EXCEEDED"
    ERROR = "ERROR"


class Level(enum.StrEnum):
    DEBUG = "debug"
    INFO = "info"
    WARN = "warn"
    ERROR = "error"


class AgentEvent(BaseModel):
    id: uuid.UUID = Field(default_factory=uuid.uuid4)
    run_id: uuid.UUID | None = None
    type: EventType
    level: Level = Level.INFO
    message: str = ""
    job_id: str | None = None
    application_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    at: datetime = Field(default_factory=lambda: datetime.now(UTC))


class EventSink(Protocol):
    async def emit(self, event: AgentEvent) -> None: ...


class LogSink:
    """Mirrors events into the structured log. Redaction happens downstream."""

    async def emit(self, event: AgentEvent) -> None:
        log.info(
            event.type.value,
            run_id=str(event.run_id) if event.run_id else None,
            job_id=event.job_id,
            message=event.message,
            **event.payload,
        )


class DatabaseSink:
    """Persists events to `agent_event`, so the activity feed survives a restart
    and there is an audit trail of what the agent actually did.

    Failures are swallowed on purpose: an unwritable event log is a lost record,
    not a reason to abort a discovery run that is otherwise working.
    """

    async def emit(self, event: AgentEvent) -> None:
        try:
            from .storage import repository as repo
            from .storage import session_scope

            async with session_scope() as session:
                await repo.record_event(session, event)
        except Exception as exc:
            log.debug("event_persist_failed", error=str(exc)[:120])


class EventBus:
    """In-process fan-out with bounded subscriber queues.

    A slow subscriber (a browser tab that stopped reading its SSE stream) must
    never block the agent producing events, so queues drop their oldest item
    rather than applying backpressure. Durability is the sinks' job, not the
    subscribers'.
    """

    def __init__(self, sinks: list[EventSink] | None = None, queue_size: int = 512) -> None:
        self._sinks: list[EventSink] = sinks if sinks is not None else [LogSink()]
        self._subscribers: set[asyncio.Queue[AgentEvent]] = set()
        self._queue_size = queue_size

    def add_sink(self, sink: EventSink) -> bool:
        """Register a sink. Returns whether it was added.

        Idempotent per sink type, and that is the whole point. The bus is process-global
        while the application lifespan is not: `uvicorn --reload` restarts the app in
        place, and every restart used to append a second `DatabaseSink`, so each event
        was written to `agent_event` twice — then three times, then four. Nothing raised;
        the activity feed just quietly grew duplicates of everything.

        A second sink of a type already present is therefore refused rather than stacked.
        Distinct sink types compose as before.
        """
        if any(type(existing) is type(sink) for existing in self._sinks):
            log.debug("event_sink_already_registered", sink=type(sink).__name__)
            return False
        self._sinks.append(sink)
        return True

    def remove_sink(self, sink: EventSink) -> bool:
        """Unregister a sink, by identity or by type.

        Whoever adds a sink at startup should remove it at shutdown; `add_sink`'s dedupe
        is the safety net, not the mechanism.
        """
        for existing in list(self._sinks):
            if existing is sink or type(existing) is type(sink):
                self._sinks.remove(existing)
                return True
        return False

    def sink_names(self) -> list[str]:
        return [type(sink).__name__ for sink in self._sinks]

    async def emit(self, event: AgentEvent) -> None:
        for sink in self._sinks:
            try:
                await sink.emit(event)
            except Exception as exc:  # a failing sink must not stop the agent
                log.warning("event_sink_failed", sink=type(sink).__name__, error=str(exc))

        for queue in list(self._subscribers):
            if queue.full():
                with contextlib.suppress(asyncio.QueueEmpty):
                    queue.get_nowait()  # drop oldest
            queue.put_nowait(event)

    async def emit_one(
        self,
        type_: EventType,
        message: str = "",
        *,
        run_id: uuid.UUID | None = None,
        level: Level = Level.INFO,
        job_id: str | None = None,
        application_id: str | None = None,
        **payload: Any,
    ) -> AgentEvent:
        event = AgentEvent(
            type=type_,
            message=message,
            run_id=run_id,
            level=level,
            job_id=job_id,
            application_id=application_id,
            payload=payload,
        )
        await self.emit(event)
        return event

    async def subscribe(self) -> AsyncIterator[AgentEvent]:
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue(maxsize=self._queue_size)
        self._subscribers.add(queue)
        try:
            while True:
                yield await queue.get()
        finally:
            self._subscribers.discard(queue)


_bus: EventBus | None = None


def get_bus() -> EventBus:
    global _bus
    if _bus is None:
        _bus = EventBus()
    return _bus
