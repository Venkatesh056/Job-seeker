"""The FastAPI dependency that decides who a request is.

Attached to the **application**, not to the routers and not to the endpoints that seemed to
need it. An opt-in list of protected endpoints is a list somebody forgets to add to, and the
symptom of forgetting is not an error — it is an endpoint quietly serving anybody's data.

It was attached at `include_router` first, and the isolation tests failed within a minute:
`api.py` declares its own endpoints with `@app.get`, so `/api/v1/candidate` — the parsed
resume — answered a caller with no token. An app-level dependency covers every route that
exists and every route anybody adds later. The genuinely public paths say so out loud in
`PUBLIC_PATHS`.

Two modes, and the difference is enforced here rather than trusted to callers:

* **single-user** — no token needed, nothing bound, and the repository falls back to the one
  profile in the database. Correct when there is one.
* **multi-user** — a valid bearer token is required, the principal is bound for the life of
  the request, and the repository's fallback becomes an exception instead of a silent read
  of the first row.
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from ..logging import get_logger
from ..storage import session_scope
from . import service, tenancy
from .tenancy import Principal

log = get_logger("auth.deps")

# `auto_error=False` so a missing header reaches this code rather than being turned into a
# 403 by the security scheme. Single-user mode has to be able to allow it.
bearer = HTTPBearer(auto_error=False)

# Reachable without a token even when multi-user is on. Everything here either *is* the way
# to get a token, or says nothing about anybody: an unauthenticated caller learns the
# service is running and what version it is, and no more.
PUBLIC_PATHS: frozenset[str] = frozenset(
    {
        # The two endpoints that exist to hand out a token.
        "/api/v1/auth/register",
        "/api/v1/auth/login",
        # The dashboard shell. It is a static file with no data in it, and it has to load
        # for the login screen inside it to be reachable at all — serving 401 here is a
        # locked door with the key behind it. Every byte of data the page then shows comes
        # from an API call that does need a token.
        "/",
        # Liveness and version. An unauthenticated caller learns the service is running
        # and what build it is, and no more.
        "/api/v1/status",
        "/health",
        "/docs",
        "/redoc",
        "/openapi.json",
    }
)


def _unauthorized(detail: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        # Without this header a browser or client cannot tell "you need to log in" from
        # "you are logged in and not allowed", which is the distinction 401 exists to make.
        headers={"WWW-Authenticate": "Bearer"},
    )


async def bind_principal(
    request: Request,
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
) -> Principal | None:
    """Resolve the caller and bind them for the rest of the request.

    The binding is a `ContextVar` set inside this dependency. FastAPI runs dependencies and
    the endpoint in the same task, and each request's task copies the context at creation,
    so the value is visible to the endpoint and cannot leak into another request.
    """
    if not tenancy.multi_user_enabled():
        # Nothing is bound: the repository's single-user fallback is the correct answer
        # when there is exactly one profile, and binding a fake principal here would make
        # the two modes differ in ways no test would catch.
        return None

    if request.url.path in PUBLIC_PATHS:
        return None

    if credentials is None or not (credentials.credentials or "").strip():
        raise _unauthorized("this endpoint needs a bearer token")

    async with session_scope() as session:
        try:
            principal = await service.principal_for(session, credentials.credentials)
        except service.AuthError as exc:
            raise _unauthorized(str(exc)) from exc

    tenancy.bind_token(principal)
    request.state.principal = principal
    return principal


async def current_principal(
    principal: Principal | None = Depends(bind_principal),
) -> Principal:
    """For endpoints that genuinely cannot proceed without knowing who is asking.

    In single-user mode there is no token, so this synthesises a principal for the sole
    account rather than refusing — the local dashboard has always worked without a login and
    turning `/auth/me` into a 401 there would be a regression dressed as security.
    """
    if principal is not None:
        return principal

    if tenancy.multi_user_enabled():
        raise _unauthorized("this endpoint needs a bearer token")

    from sqlalchemy import select

    from ..storage.models import User

    async with session_scope() as session:
        user = (await session.execute(select(User).limit(1))).scalar_one_or_none()
        if user is None:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "no account exists yet — run `careeragent account create` or upload a resume",
            )
        from ..storage import repository as repo

        candidate = await repo.load_candidate(session)
        candidate_row = None
        if candidate is not None:
            candidate_row = await repo.get_or_create_candidate(session)

    return Principal(
        user_id=user.id,
        email=user.email,
        candidate_id=candidate_row.id if candidate_row else None,
        is_admin=bool(user.is_admin),
    )
