"""The outbound edge: how this system touches other people's servers.

Two concerns live here, and both are about being a good client rather than a fast one.

* `limits` — how often. A token bucket per provider, in-process by default and shared
  across processes when Redis is configured.
* `faults` — what happens when the other side misbehaves. A fault injector used by the
  test suite to prove the system degrades honestly instead of corrupting data or claiming
  success it did not have.
"""

from . import tracing
from .faults import Fault, FaultTransport, fault_client, flaky
from .limits import (
    DEFAULT_LIMITS,
    Limit,
    Limiter,
    MemoryBackend,
    RateLimited,
    RedisBackend,
    get_limiter,
    reset_limiter,
    retry_after_seconds,
)

__all__ = [
    "DEFAULT_LIMITS",
    "Fault",
    "FaultTransport",
    "Limit",
    "Limiter",
    "MemoryBackend",
    "RateLimited",
    "RedisBackend",
    "fault_client",
    "flaky",
    "get_limiter",
    "reset_limiter",
    "retry_after_seconds",
    "tracing",
]
