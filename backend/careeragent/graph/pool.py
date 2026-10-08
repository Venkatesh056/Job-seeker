"""The apply pool: three applications in flight at once.

You pick companies, press go, and three browser agents work the queue in parallel while
the rest wait their turn. Each one is a full run of the apply graph, so each gets its
own tailored resume, its own form, its own verdict and its own approval.

Three constraints shape this, and all three are about not doing damage at speed:

  * **Bounded concurrency.** Each worker is a Chromium instance at roughly 400MB.
    Three is fast enough to clear twenty applications in a quarter of an hour and light
    enough to leave the machine usable.
  * **The daily cap is re-checked per item, not once.** Checking at the start lets a
    long queue sail past the cap while it drains.
  * **One company at a time per employer.** Two workers filling two forms at the same
    ATS tenant is how a run trips a rate limit and loses both.

Failure is per-item. One application failing to open a form does not stop the other
nineteen, and the queue records why each one ended where it did.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from ..config import get_settings
from ..events import EventType, Level, get_bus
from ..logging import get_logger
from ..storage import repository as repo
from ..storage import session_scope

log = get_logger("graph.pool")

DEFAULT_CONCURRENCY = 3
MAX_CONCURRENCY = 5


@dataclass
class Item:
    """One queued application and everything that happened to it."""

    job_id: str
    company: str = ""
    title: str = ""
    application_id: str = ""
    status: str = "queued"
    awaiting_approval: bool = False
    ats: str = ""
    url: str = ""
    needs_user: list[str] = field(default_factory=list)
    never_filled: list[str] = field(default_factory=list)
    holds: list[str] = field(default_factory=list)
    validation: dict[str, Any] = field(default_factory=dict)
    screenshots: list[str] = field(default_factory=list)
    error: str = ""
    started_at: float = 0.0
    finished_at: float = 0.0

    @property
    def seconds(self) -> float:
        if not self.started_at:
            return 0.0
        return round((self.finished_at or time.monotonic()) - self.started_at, 1)

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "company": self.company,
            "title": self.title,
            "application_id": self.application_id,
            "status": self.status,
            "awaiting_approval": self.awaiting_approval,
            "ats": self.ats,
            "url": self.url,
            "needs_user": list(self.needs_user),
            "never_filled": list(self.never_filled),
            "holds": list(self.holds),
            "validation": dict(self.validation),
            "screenshots": list(self.screenshots),
            "error": self.error,
            "seconds": self.seconds,
        }


@dataclass
class Batch:
    """A run of the pool. Read by the dashboard while it is still going."""

    id: str
    items: list[Item]
    mode: str = "preview"
    concurrency: int = DEFAULT_CONCURRENCY
    started_at: float = field(default_factory=time.monotonic)
    finished_at: float = 0.0
    stopped: bool = False

    @property
    def done(self) -> bool:
        return bool(self.finished_at)

    def summary(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for item in self.items:
            by_status[item.status] = by_status.get(item.status, 0) + 1
        return {
            "batch_id": self.id,
            "mode": self.mode,
            "concurrency": self.concurrency,
            "total": len(self.items),
            "done": self.done,
            "stopped": self.stopped,
            "elapsed": round((self.finished_at or time.monotonic()) - self.started_at, 1),
            "by_status": by_status,
            "awaiting_approval": sum(1 for i in self.items if i.awaiting_approval),
            "submitted": sum(1 for i in self.items if i.status == "applied"),
            "unverified": sum(1 for i in self.items if i.status == "submission_unverified"),
            "needs_you": sum(1 for i in self.items if i.status == "human_required"),
            "failed": sum(
                1 for i in self.items if i.status in {"failed", "failed_terminal", "error"}
            ),
            "items": [i.to_dict() for i in self.items],
        }


class ApplyPool:
    """Process-local. One batch at a time, because the browsers are a shared resource."""

    def __init__(self) -> None:
        self._batches: dict[str, Batch] = {}
        self._current: str | None = None
        self._task: asyncio.Task | None = None

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def get(self, batch_id: str) -> Batch | None:
        return self._batches.get(batch_id)

    def current(self) -> Batch | None:
        return self._batches.get(self._current) if self._current else None

    def recent(self, limit: int = 5) -> list[dict[str, Any]]:
        return [b.summary() for b in list(self._batches.values())[-limit:]]

    async def start(
        self,
        job_ids: list[str],
        candidate_id: str,
        *,
        mode: str = "preview",
        concurrency: int = DEFAULT_CONCURRENCY,
        use_llm: bool = True,
    ) -> Batch:
        """Queue jobs and start working them. Returns immediately."""
        if self.running:
            raise RuntimeError("a batch is already running — stop it or wait for it to finish")

        items: list[Item] = []
        async with session_scope() as session:
            from ..storage.models import Job

            for job_id in job_ids:
                job = await session.get(Job, job_id)
                items.append(
                    Item(
                        job_id=job_id,
                        company=job.company if job else "",
                        title=job.title if job else "",
                        status="queued" if job else "error",
                        error="" if job else "job not found",
                    )
                )

        batch = Batch(
            id=str(uuid.uuid4()),
            items=items,
            mode=mode,
            concurrency=max(1, min(concurrency, MAX_CONCURRENCY)),
        )
        self._batches[batch.id] = batch
        self._current = batch.id
        self._task = asyncio.create_task(self._run(batch, candidate_id, use_llm))
        return batch

    async def stop(self) -> bool:
        """Stop after the in-flight applications finish.

        Deliberately not a hard cancel: killing a worker mid-submit is exactly how an
        application ends up sent with no record of it.
        """
        batch = self.current()
        if batch is None or batch.done:
            return False
        batch.stopped = True
        return True

    async def _run(self, batch: Batch, candidate_id: str, use_llm: bool) -> None:
        from .apply import start_application

        settings = get_settings()
        gate = asyncio.Semaphore(batch.concurrency)
        # One in-flight application per employer: two forms on the same ATS tenant at
        # once is the reliable way to trip a rate limit and lose both.
        company_locks: dict[str, asyncio.Lock] = {}
        lock_guard = asyncio.Lock()

        await get_bus().emit_one(
            EventType.APPLICATION_STARTED,
            f"Applying to {len(batch.items)} job(s), {batch.concurrency} at a time",
            batch_id=batch.id,
            mode=batch.mode,
        )

        async def worker(item: Item) -> None:
            if batch.stopped or item.status == "error":
                if item.status != "error":
                    item.status = "skipped"
                    item.error = "batch stopped before this one started"
                return

            submitted = await self._submitted_today(candidate_id)
            if submitted >= settings.daily_application_cap:
                item.status = "skipped"
                item.error = f"daily cap reached ({submitted}/{settings.daily_application_cap})"
                return

            async with lock_guard:
                lock = company_locks.setdefault(item.company.lower(), asyncio.Lock())

            async with gate, lock:
                if batch.stopped:
                    item.status = "skipped"
                    item.error = "batch stopped"
                    return

                item.started_at = time.monotonic()
                item.status = "running"
                try:
                    application_id = await self._shortlist(item.job_id, candidate_id)
                    if application_id is None:
                        item.status = "skipped"
                        item.error = "already applied to this job"
                        return
                    item.application_id = application_id

                    state = await start_application(
                        application_id,
                        mode=batch.mode,
                        use_llm=use_llm,
                        autonomy_level=settings.autonomy_level,
                    )
                    item.status = state.get("status") or "unknown"
                    item.awaiting_approval = bool(state.get("awaiting_approval"))
                    item.ats = state.get("ats", "")
                    item.url = state.get("url", "")
                    item.needs_user = list(state.get("needs_user") or [])
                    item.never_filled = list(state.get("never_filled") or [])
                    item.holds = [str(h) for h in (state.get("holds") or [])]
                    item.validation = dict(state.get("validation") or {})
                    item.screenshots = list(state.get("screenshots") or [])
                    item.error = state.get("error", "")
                except Exception as exc:
                    log.exception("pool_item_failed", job_id=item.job_id)
                    item.status = "error"
                    item.error = f"{type(exc).__name__}: {exc}"[:200]
                finally:
                    item.finished_at = time.monotonic()

                await get_bus().emit_one(
                    EventType.APPLICATION_STARTED
                    if item.status not in {"failed", "error"}
                    else EventType.APPLICATION_FAILED,
                    f"{item.company}: {item.status}" + (f" — {item.error}" if item.error else ""),
                    application_id=item.application_id or None,
                    level=Level.WARN if item.error else Level.INFO,
                    batch_id=batch.id,
                )

        try:
            await asyncio.gather(*(worker(item) for item in batch.items))
        finally:
            batch.finished_at = time.monotonic()
            summary = batch.summary()
            await get_bus().emit_one(
                EventType.RUN_FINISHED,
                f"Batch finished: {summary['awaiting_approval']} awaiting approval, "
                f"{summary['submitted']} submitted, {summary['needs_you']} need you, "
                f"{summary['failed']} failed",
                batch_id=batch.id,
                **{k: v for k, v in summary.items() if k != "items"},
            )

    @staticmethod
    async def _submitted_today(candidate_id: str) -> int:
        async with session_scope() as session:
            return await repo.applications_submitted_today(session, candidate_id)

    @staticmethod
    async def _shortlist(job_id: str, candidate_id: str) -> str | None:
        """Create the application, or None if one already exists.

        The duplicate guard is the database's unique constraint; this just reads the
        answer before spending a browser on it.
        """
        from ..applications import service

        async with session_scope() as session:
            existing = await repo.application_for_job(session, job_id, candidate_id)
            if existing is not None and existing.status in {
                "applied",
                "submission_unverified",
            }:
                return None
            application = await service.shortlist(session, job_id, candidate_id)
            return application.id


_pool: ApplyPool | None = None


def get_pool() -> ApplyPool:
    global _pool
    if _pool is None:
        _pool = ApplyPool()
    return _pool
