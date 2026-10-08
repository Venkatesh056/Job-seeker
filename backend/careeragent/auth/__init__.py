"""Identity, sessions and tenancy.

Four modules, each with one job:

* `passwords` — scrypt hashing, on the standard library
* `tokens` — HS256 JWTs, also on the standard library, so a Supabase session and one this
  system issued verify through the same code
* `service` — register, authenticate, revoke; the place that decides what an error is
  allowed to say
* `tenancy` — who the current unit of work is for, and the refusal that replaces the old
  "first row in the table"
"""

from __future__ import annotations

from .service import AuthError, WeakPassword, authenticate, principal_for, register, session_for
from .tenancy import (
    NoPrincipal,
    Principal,
    assert_owns,
    bind,
    current,
    current_candidate_id,
    multi_user_enabled,
    require,
)

__all__ = [
    "AuthError",
    "NoPrincipal",
    "Principal",
    "WeakPassword",
    "assert_owns",
    "authenticate",
    "bind",
    "current",
    "current_candidate_id",
    "multi_user_enabled",
    "principal_for",
    "register",
    "require",
    "session_for",
]
