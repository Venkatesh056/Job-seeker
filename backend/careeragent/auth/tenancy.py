"""Who the current work is being done for.

Every per-user table in this system already carries a `candidate_id`, and has since the
first migration. What was single-user was never the *schema* — it was the resolution:

```python
row = (await session.execute(select(Candidate).limit(1))).scalar_one_or_none()
```

`limit(1)`. The first candidate in the table. There are 111 call sites downstream of that
line, and rewriting each one to thread an owner through would be 111 chances to forget one —
and a forgotten one does not fail, it quietly serves somebody else's data.

So the binding lives in a context variable instead, and the *lookup* changes rather than its
callers. A request binds the authenticated user for its duration; a CLI command binds the
local owner; a background task binds whoever it is acting for.

**The fallback is a refusal, not a default.** In multi-user mode, work that reaches the
database with nobody bound raises. That is deliberately a 500 rather than a quiet read of the
first row, because the quiet read is a cross-tenant disclosure and the exception is a bug
report. Single-user mode is the one case where falling back to the sole account is correct,
and it says so out loud.

`contextvars` and not a thread-local: this application is async, many requests share a
thread, and a thread-local would hand one user's identity to another's coroutine.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass

from ..config import get_settings
from ..logging import get_logger

log = get_logger("auth.tenancy")


@dataclass(frozen=True)
class Principal:
    """Whoever this unit of work is for.

    `candidate_id` may be None for a user who has registered but not yet uploaded a resume —
    that is an ordinary state, not an error, and the code that needs a candidate asks for one
    explicitly.
    """

    user_id: str
    email: str
    candidate_id: str | None = None
    is_admin: bool = False


_current: ContextVar[Principal | None] = ContextVar("careeragent_principal", default=None)


class NoPrincipal(RuntimeError):
    """Work reached the database without an owner, in a mode that requires one.

    Raised rather than falling back. The fallback would be somebody else's data.
    """


def multi_user_enabled() -> bool:
    """Whether identity is enforced.

    Off by default, because the shipped configuration is one person running this against
    their own job hunt on their own machine, and demanding a login to read your own resume
    would be security theatre with a real cost.

    On, it is not optional anywhere: the same switch that adds the login requirement is the
    one that turns the tenancy fallback into an exception, so there is no deployment in
    which requests are authenticated but queries are not scoped.
    """
    return bool(getattr(get_settings(), "multi_user", False))


def current() -> Principal | None:
    return _current.get()


def require() -> Principal:
    principal = _current.get()
    if principal is None:
        raise NoPrincipal(
            "no user is bound to this unit of work. In multi-user mode every database "
            "read must name its owner — see careeragent.auth.tenancy.bind()."
        )
    return principal


def current_candidate_id() -> str | None:
    """The bound candidate, or None.

    Returns None rather than raising in single-user mode so the existing repository lookup
    can keep its "first row" behaviour, which is correct when there is exactly one account.
    """
    principal = _current.get()
    if principal is not None:
        return principal.candidate_id
    if multi_user_enabled():
        raise NoPrincipal(
            "a candidate was needed and no user is bound. Refusing to fall back to the "
            "first row in the table: in multi-user mode that is another tenant's data."
        )
    return None


def bind_token(principal: Principal | None) -> Token[Principal | None]:
    """Bind for the rest of this context. Returns the token to reset with."""
    return _current.set(principal)


def reset(token: Token[Principal | None]) -> None:
    _current.reset(token)


@contextmanager
def bind(principal: Principal | None) -> Iterator[Principal | None]:
    """Bind an owner for a block of work.

    ```python
    with tenancy.bind(principal):
        await do_the_thing(session)
    ```

    Always resets, including when the block raises. A binding that survived an exception
    would leak into whatever the worker picked up next.
    """
    token = _current.set(principal)
    try:
        yield principal
    finally:
        _current.reset(token)


def with_candidate(principal: Principal, candidate_id: str | None) -> Principal:
    """The same principal, now knowing its candidate.

    Used the first time a request resolves one, so the rest of the request does not look it
    up again.
    """
    return Principal(
        user_id=principal.user_id,
        email=principal.email,
        candidate_id=candidate_id,
        is_admin=principal.is_admin,
    )


def assert_owns(candidate_id: str | None, *, what: str = "this record") -> None:
    """Refuse to act on a row belonging to somebody else.

    Called at the point of use rather than trusted to the query, because the dangerous case
    is a route that takes an id from the caller: `/applications/{id}` is a perfectly ordinary
    endpoint and a perfectly ordinary way to read another tenant's row by guessing.

    Raises `PermissionError`, which the API renders as **404, not 403**. A 403 confirms the
    id exists, which is exactly the fact being protected.
    """
    if not multi_user_enabled():
        return
    principal = require()
    if principal.is_admin:
        return
    if candidate_id and candidate_id != principal.candidate_id:
        log.warning(
            "cross_tenant_access_refused",
            user_id=principal.user_id,
            wanted=candidate_id,
            owns=principal.candidate_id,
        )
        raise PermissionError(what)
