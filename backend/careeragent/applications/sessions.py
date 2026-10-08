"""Live browser sessions held between prepare and approve.

An approval is only meaningful if it approves *the form the user looked at*. So
the filled page stays open, and the submit click lands on that same page — not on
a fresh load that may render differently, have different options, or have lost a
file upload.

Holding a browser open across HTTP requests is real state, so the rules are
explicit:

  * **Bounded.** At most `MAX_SESSIONS`; the oldest idle one is evicted.
  * **Expiring.** `TTL_SECONDS` of inactivity closes it. An abandoned approval must
    not leave Chromium running for a day.
  * **Not required.** If a session is gone — expired, evicted, server restarted —
    the approval path re-prepares from scratch and says so in the event log. Losing
    a session degrades to slower, never to wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from ..logging import get_logger

if TYPE_CHECKING:
    from ..browser.adapters import FormSpec
    from ..browser.runner import BrowserRunner

log = get_logger("applications.sessions")

TTL_SECONDS = 30 * 60
MAX_SESSIONS = 4


@dataclass
class LiveSession:
    application_id: str
    runner: BrowserRunner
    spec: FormSpec
    url: str
    screenshots: list[str] = field(default_factory=list)
    created_at: float = field(default_factory=time.monotonic)
    touched_at: float = field(default_factory=time.monotonic)
    headed: bool = False

    @property
    def age_seconds(self) -> float:
        return time.monotonic() - self.created_at

    @property
    def idle_seconds(self) -> float:
        return time.monotonic() - self.touched_at

    @property
    def alive(self) -> bool:
        return self.runner.held_page is not None and self.idle_seconds < TTL_SECONDS

    def touch(self) -> None:
        self.touched_at = time.monotonic()

    async def close(self) -> None:
        with contextlib.suppress(Exception):
            await self.runner.__aexit__(None, None, None)


class SessionRegistry:
    """Process-local. Deliberately not shared across workers.

    A browser page cannot be handed to another process, so pretending otherwise
    with a shared store would produce approvals that silently re-prepare. Run the
    API single-worker when using the approval flow; the fallback covers the rest.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, LiveSession] = {}
        self._lock = asyncio.Lock()

    async def put(self, session: LiveSession) -> None:
        async with self._lock:
            if existing := self._sessions.pop(session.application_id, None):
                await existing.close()
            self._sessions[session.application_id] = session
            await self._evict_locked()
        log.info("session_held", application_id=session.application_id, url=session.url[:120])

    async def get(self, application_id: str) -> LiveSession | None:
        async with self._lock:
            session = self._sessions.get(application_id)
            if session is None:
                return None
            if not session.alive:
                self._sessions.pop(application_id, None)
                await session.close()
                log.info("session_expired", application_id=application_id)
                return None
            session.touch()
            return session

    async def drop(self, application_id: str) -> None:
        async with self._lock:
            session = self._sessions.pop(application_id, None)
        if session is not None:
            await session.close()
            log.info("session_closed", application_id=application_id)

    async def sweep(self) -> int:
        """Close expired sessions. Called on a timer by the API lifespan."""
        async with self._lock:
            dead = [sid for sid, s in self._sessions.items() if not s.alive]
            sessions = [self._sessions.pop(sid) for sid in dead]
        for session in sessions:
            await session.close()
        if sessions:
            log.info("sessions_swept", closed=len(sessions))
        return len(sessions)

    async def close_all(self) -> None:
        async with self._lock:
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            await session.close()

    async def _evict_locked(self) -> None:
        while len(self._sessions) > MAX_SESSIONS:
            oldest = min(self._sessions.values(), key=lambda s: s.touched_at)
            self._sessions.pop(oldest.application_id, None)
            await oldest.close()
            log.info("session_evicted", application_id=oldest.application_id)

    def describe(self) -> list[dict[str, Any]]:
        return [
            {
                "application_id": s.application_id,
                "url": s.url,
                "alive": s.alive,
                "headed": s.headed,
                "age_seconds": round(s.age_seconds, 1),
                "idle_seconds": round(s.idle_seconds, 1),
                "screenshots": list(s.screenshots),
            }
            for s in self._sessions.values()
        ]


_registry: SessionRegistry | None = None


def get_registry() -> SessionRegistry:
    global _registry
    if _registry is None:
        _registry = SessionRegistry()
    return _registry
