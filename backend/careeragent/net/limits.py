"""Outbound rate limiting — how often this system is allowed to touch somebody else's server.

Distinct from the limits in `applications/outreach.py`, which are a *policy* about how many
messages a person may send in a day. This is mechanical: 24 ATS boards, several search
providers and a handful of enrichment APIs, all hit in bursts by an agent that does not get
tired. Being impolite here does not produce an error — it produces a 429, then a block, and
the block arrives days later as "that board stopped working".

A token bucket rather than a fixed window. A fixed window lets a caller spend its whole
allowance in the first millisecond of each window and then hammer the boundary — twice the
nominal rate, arriving as two bursts. A bucket that refills continuously has no boundary to
crowd against.

Two ways to be limited, because they are not the same need:

* `acquire()` **waits**. For politeness: the work is still worth doing, just not right now.
* `try_acquire()` **refuses**. For hard caps, where waiting would mean an agent parked on a
  provider whose budget is already spent.

**The memory backend is per-process, and says so.** A limiter that does not cross processes
is a limiter that quietly lies the moment a second worker starts — each one politely obeying
a limit, together exceeding it. That is what the Redis backend is for, and why
`describe()` reports which one is in use rather than leaving it to be assumed.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Protocol

from ..logging import get_logger

log = get_logger("net.limits")


@dataclass(frozen=True)
class Limit:
    """`rate` requests per second, with a burst allowance of `burst`.

    `burst` is what makes this usable: probing eight ATS providers concurrently for one
    company is a legitimate burst, and a limiter with no headroom would serialise it into
    eight seconds for no benefit to anybody.
    """

    rate: float
    burst: int = 1

    def __post_init__(self) -> None:
        if self.rate <= 0:
            raise ValueError("a rate limit of zero or less would block forever")
        if self.burst < 1:
            raise ValueError("a burst of zero permits nothing")


# Defaults chosen per provider rather than one global number, because the limits these
# actually protect against are different sizes. The ATS boards are public JSON endpoints
# belonging to companies who did not ask to be crawled; the search providers sell credits
# and publish a rate. When in doubt the number here is lower than the published one.
DEFAULT_LIMITS: dict[str, Limit] = {
    "greenhouse": Limit(rate=4.0, burst=8),
    "lever": Limit(rate=4.0, burst=8),
    "ashby": Limit(rate=3.0, burst=6),
    "workday": Limit(rate=2.0, burst=4),
    "recruitee": Limit(rate=3.0, burst=6),
    "smartrecruiters": Limit(rate=3.0, burst=6),
    "personio": Limit(rate=2.0, burst=4),
    "teamtailor": Limit(rate=2.0, burst=4),
    "workable": Limit(rate=3.0, burst=6),
    "jobvite": Limit(rate=2.0, burst=4),
    "breezy": Limit(rate=2.0, burst=4),
    "bamboohr": Limit(rate=2.0, burst=4),
    "jazzhr": Limit(rate=2.0, burst=4),
    "rippling": Limit(rate=2.0, burst=4),
    # Paid, metered, and slower on purpose: every call here costs a credit somebody counted.
    "tavily": Limit(rate=1.0, burst=3),
    "serpapi": Limit(rate=1.0, burst=2),
    "apollo": Limit(rate=1.0, burst=2),
    "hunter": Limit(rate=1.0, burst=2),
    # Crawling a company's own website. Slowest of all: this is somebody's marketing site,
    # not an API, and there is no plan under which we are entitled to its capacity.
    "website": Limit(rate=1.0, burst=2),
}
FALLBACK = Limit(rate=2.0, burst=4)


class Backend(Protocol):
    async def take(self, key: str, limit: Limit, tokens: float) -> float:
        """Seconds to wait before `tokens` may be spent. 0.0 means now.

        Returning a delay rather than sleeping keeps the decision and the waiting apart, so
        `try_acquire` can ask the same question without committing to the answer.
        """
        ...

    async def penalise(self, key: str, seconds: float) -> None: ...

    def describe(self) -> str: ...


@dataclass
class _Bucket:
    tokens: float
    updated: float
    # Set by a 429. Until this passes, nothing is granted regardless of the bucket, because
    # the server has told us its own limit and it outranks our guess about its limit.
    blocked_until: float = 0.0


class MemoryBackend:
    """Per-process buckets. Correct for one worker, and honest about being no more.

    The lock is around the arithmetic only. Holding it across the sleep would serialise
    every caller behind the first one's wait, turning a rate limiter into a mutex.
    """

    def __init__(self) -> None:
        self._buckets: dict[str, _Bucket] = {}
        self._lock = asyncio.Lock()

    async def take(self, key: str, limit: Limit, tokens: float = 1.0) -> float:
        now = time.monotonic()
        async with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                bucket = _Bucket(tokens=float(limit.burst), updated=now)
                self._buckets[key] = bucket

            if bucket.blocked_until > now:
                return bucket.blocked_until - now

            elapsed = max(0.0, now - bucket.updated)
            bucket.tokens = min(float(limit.burst), bucket.tokens + elapsed * limit.rate)
            bucket.updated = now

            if bucket.tokens >= tokens:
                bucket.tokens -= tokens
                return 0.0
            # Spend nothing. A caller that took a partial token and waited would let two
            # callers each hold half a permit and both proceed.
            return (tokens - bucket.tokens) / limit.rate

    async def penalise(self, key: str, seconds: float) -> None:
        async with self._lock:
            bucket = self._buckets.setdefault(key, _Bucket(tokens=0.0, updated=time.monotonic()))
            bucket.blocked_until = max(bucket.blocked_until, time.monotonic() + max(0.0, seconds))
            bucket.tokens = 0.0

    def describe(self) -> str:
        return "memory (per-process; a second worker would not see these buckets)"


class RedisBackend:
    """Buckets shared across processes, in one atomic script.

    Lua rather than WATCH/MULTI because the read-modify-write has to be indivisible: two
    workers reading the same bucket, each finding a token, each taking it, is exactly the
    over-spend this exists to prevent — and it is the case that only appears under load.

    **A Redis failure does not stop work.** If the server is unreachable the limiter fails
    *open*, allowing the call and logging it. The alternative is that a cache outage takes
    down job discovery, which trades a small politeness risk for a total one.
    """

    # KEYS[1] bucket, ARGV: rate, burst, tokens, now_ms
    _SCRIPT = """
    local data = redis.call('HMGET', KEYS[1], 'tokens', 'updated', 'blocked')
    local rate, burst = tonumber(ARGV[1]), tonumber(ARGV[2])
    local want, now = tonumber(ARGV[3]), tonumber(ARGV[4])
    local tokens = tonumber(data[1]) or burst
    local updated = tonumber(data[2]) or now
    local blocked = tonumber(data[3]) or 0

    if blocked > now then return tostring((blocked - now) / 1000.0) end

    local elapsed = math.max(0, now - updated) / 1000.0
    tokens = math.min(burst, tokens + elapsed * rate)
    local wait = 0.0
    if tokens >= want then
        tokens = tokens - want
    else
        wait = (want - tokens) / rate
    end
    redis.call('HSET', KEYS[1], 'tokens', tokens, 'updated', now)
    -- Expire idle buckets rather than accumulating a key per host forever.
    redis.call('PEXPIRE', KEYS[1], math.ceil((burst / rate) * 1000) + 60000)
    return tostring(wait)
    """

    def __init__(self, client: object, prefix: str = "careeragent:rl:") -> None:
        self._client = client
        self._prefix = prefix
        self._script = None
        self._degraded = False

    async def take(self, key: str, limit: Limit, tokens: float = 1.0) -> float:
        try:
            if self._script is None:
                self._script = self._client.register_script(self._SCRIPT)  # type: ignore[attr-defined]
            raw = await self._script(  # type: ignore[misc]
                keys=[self._prefix + key],
                args=[limit.rate, limit.burst, tokens, int(time.time() * 1000)],
            )
            self._degraded = False
            return float(raw)
        except Exception as exc:
            if not self._degraded:
                self._degraded = True
                log.warning("rate_limiter_degraded", backend="redis", error=str(exc)[:200])
            # Fail open. A cache outage should not take down job discovery.
            return 0.0

    async def penalise(self, key: str, seconds: float) -> None:
        try:
            until = int((time.time() + max(0.0, seconds)) * 1000)
            await self._client.hset(self._prefix + key, "blocked", until)  # type: ignore[attr-defined]
            await self._client.pexpire(self._prefix + key, int(seconds * 1000) + 60000)  # type: ignore[attr-defined]
        except Exception as exc:
            log.warning("rate_limiter_penalty_lost", error=str(exc)[:200])

    def describe(self) -> str:
        return "redis (shared across processes)" + (" — degraded" if self._degraded else "")


@dataclass
class Limiter:
    """The thing callers hold."""

    backend: Backend
    limits: dict[str, Limit] = field(default_factory=lambda: dict(DEFAULT_LIMITS))
    # Waiting longer than this is not politeness, it is a stall. Past it the caller is told
    # to go away and try later, which it can report rather than hang on.
    max_wait_seconds: float = 30.0

    def limit_for(self, key: str) -> Limit:
        return self.limits.get(key, FALLBACK)

    async def acquire(self, key: str, tokens: float = 1.0) -> float:
        """Wait until a call to `key` is allowed. Returns how long that took.

        Loops rather than sleeping once: the backend returns the wait *as of now*, and
        between the answer and the sleep ending, another caller may have taken the token.
        Trusting the first answer is how two callers both proceed after waiting politely.
        """
        waited = 0.0
        while True:
            delay = await self.backend.take(key, self.limit_for(key), tokens)
            if delay <= 0:
                if waited > 0.5:
                    log.debug("rate_limited", key=key, waited=round(waited, 2))
                return waited
            if waited + delay > self.max_wait_seconds:
                raise RateLimited(key, waited + delay)
            await asyncio.sleep(delay)
            waited += delay

    async def try_acquire(self, key: str, tokens: float = 1.0) -> bool:
        """Take a permit if one is free. Never waits."""
        return await self.backend.take(key, self.limit_for(key), tokens) <= 0

    async def penalise(self, key: str, seconds: float) -> None:
        """The server told us to slow down. Its number outranks ours.

        Called with the `Retry-After` from a 429. A limiter that ignores this is guessing
        against a server that has stopped guessing.
        """
        if seconds > 0:
            log.info("rate_limit_penalty", key=key, seconds=round(seconds, 1))
        await self.backend.penalise(key, seconds)

    def describe(self) -> dict[str, object]:
        return {"backend": self.backend.describe(), "keys": len(self.limits)}


class RateLimited(Exception):
    """The wait would have exceeded `max_wait_seconds`.

    Raised rather than slept through, so a caller can report "this provider is saturated"
    instead of appearing to hang.
    """

    def __init__(self, key: str, seconds: float) -> None:
        self.key = key
        self.seconds = seconds
        super().__init__(f"{key} is rate limited for another {seconds:.1f}s")


_limiter: Limiter | None = None


def get_limiter() -> Limiter:
    """The process-wide limiter, built on first use.

    Redis when a URL is configured *and* the client library is installed; memory otherwise.
    Falling back silently would be wrong — `describe()` is surfaced by `/api/v1/status` so
    the answer to "are my limits shared?" is visible rather than assumed.
    """
    global _limiter
    if _limiter is not None:
        return _limiter

    from ..config import get_settings

    settings = get_settings()
    backend: Backend = MemoryBackend()
    url = (getattr(settings, "redis_url", "") or "").strip()
    if url and getattr(settings, "redis_enabled", False):
        try:
            import redis.asyncio as redis_asyncio

            backend = RedisBackend(redis_asyncio.from_url(url, decode_responses=True))
            log.info("rate_limiter_backend", backend="redis")
        except ImportError:
            log.warning(
                "rate_limiter_backend_unavailable",
                wanted="redis",
                using="memory",
                reason="the redis package is not installed",
            )

    _limiter = Limiter(backend=backend)
    return _limiter


def reset_limiter() -> None:
    """For tests, and for a settings change that should take effect without a restart."""
    global _limiter
    _limiter = None


def retry_after_seconds(headers: object, default: float = 5.0) -> float:
    """Parse `Retry-After`, which comes in two forms and is often absent.

    Seconds, or an HTTP date. A missing or unparseable value becomes `default` rather than
    zero: a 429 with no header still means slow down, and treating it as "no delay
    requested" would retry immediately into the same wall.
    """
    try:
        raw = headers.get("retry-after") if hasattr(headers, "get") else None
    except Exception:
        raw = None
    if not raw:
        return default

    value = str(raw).strip()
    try:
        return max(0.0, float(value))
    except ValueError:
        pass

    from email.utils import parsedate_to_datetime

    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return default
    if when is None:
        return default
    from datetime import UTC, datetime

    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, (when - datetime.now(UTC)).total_seconds())
