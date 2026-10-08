"""Accounts and sessions.

    POST /auth/register   create an account
    POST /auth/login      exchange email and password for a token
    GET  /auth/me         who this token belongs to
    POST /auth/logout     invalidate every token issued so far
    POST /auth/password   change it, and end every existing session

Nothing here ever returns a password hash. There is no field for one on any response model
in this file, which is a stronger guarantee than remembering not to fill it in.

`/register` and `/login` are the two endpoints reachable without a token, so they are the
two that have to be careful about what a *failure* says. Both answer the same way whether
the email exists or not — an enumeration oracle on a signup form is how a credential-stuffing
run picks its targets.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from ..auth import service, tenancy
from ..auth.deps import current_principal
from ..auth.passwords import MIN_LENGTH, WeakPassword
from ..auth.tenancy import Principal
from ..logging import get_logger
from ..storage import session_scope

log = get_logger("routes.auth")

router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class Registration(BaseModel):
    email: str = Field(min_length=5, max_length=320)
    # No `max_length` cap below something generous: a passphrase is the strongest thing
    # somebody is likely to type, and a short limit quietly forbids the best passwords.
    password: str = Field(min_length=MIN_LENGTH, max_length=512)
    display_name: str = Field(default="", max_length=200)


class Credentials(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    password: str = Field(min_length=1, max_length=512)


class PasswordChange(BaseModel):
    current_password: str = Field(min_length=1, max_length=512)
    new_password: str = Field(min_length=MIN_LENGTH, max_length=512)


class Me(BaseModel):
    """Deliberately small. Everything here the holder of the token already knows."""

    user_id: str
    email: str
    candidate_id: str | None
    is_admin: bool
    multi_user: bool


@router.post("/register", status_code=status.HTTP_201_CREATED)
async def register(body: Registration) -> dict[str, Any]:
    """Create an account and return a session for it.

    A weak password is reported as weak — that is a fact about what the caller typed, not
    about what is in the database, so saying it plainly costs nothing. Everything else
    fails identically.
    """
    async with session_scope() as session:
        try:
            user = await service.register(
                session, body.email, body.password, display_name=body.display_name
            )
        except WeakPassword as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        except service.AuthError as exc:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

        token = service.session_for(user) if tenancy.multi_user_enabled() else None
        payload: dict[str, Any] = {"user_id": user.id, "email": user.email}

    if token is not None:
        payload.update(token)
    else:
        # Issuing a session in single-user mode would imply the token does something. It
        # does not: nothing checks one, and handing out a credential that is not verified
        # anywhere is worse than handing out none.
        payload["note"] = (
            "Account created. This deployment runs in single-user mode, so no token is "
            "issued and none is required — set CAREERAGENT_MULTI_USER=true to enforce logins."
        )
    return payload


@router.post("/login")
async def login(body: Credentials) -> dict[str, Any]:
    """Exchange an email and password for a bearer token."""
    async with session_scope() as session:
        try:
            user = await service.authenticate(session, body.email, body.password)
        except service.AuthError as exc:
            # 401 and one message for every cause: unknown address, wrong password,
            # disabled account. Each of the alternatives confirms something.
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED,
                "email or password is incorrect",
                headers={"WWW-Authenticate": "Bearer"},
            ) from exc

        try:
            token = service.session_for(user)
        except service.AuthError as exc:
            # The signing secret is missing. That is a deployment fault, not a credential
            # fault, and reporting it as 401 would send somebody hunting for a typo in
            # their password for an afternoon.
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(exc)) from exc

        return {"user_id": user.id, "email": user.email, **token}


@router.get("/me", response_model=Me)
async def me(principal: Annotated[Principal, Depends(current_principal)]) -> Me:
    """Who this token belongs to, and whether identity is being enforced at all.

    `multi_user` is here because a client cannot otherwise tell a deployment that trusts it
    from one that authenticated it, and those deserve different interfaces.
    """
    return Me(
        user_id=principal.user_id,
        email=principal.email,
        candidate_id=principal.candidate_id,
        is_admin=principal.is_admin,
        multi_user=tenancy.multi_user_enabled(),
    )


@router.post("/logout")
async def logout(principal: Annotated[Principal, Depends(current_principal)]) -> dict[str, Any]:
    """End every session for this account, not just the one that called.

    Stateless tokens cannot be deleted, so this out-dates them: `tokens_valid_from` moves to
    now and every token issued before it is refused. One timestamp does the whole job
    without a session table that has to be kept consistent with tokens already in the wild.

    Logging out only the calling token would be the weaker and more surprising behaviour —
    somebody logs out because they think a session is compromised, and that is exactly the
    session they cannot call from.
    """
    async with session_scope() as session:
        await service.revoke_sessions(session, principal.user_id)
    return {"revoked": True, "note": "every existing token for this account is now refused"}


@router.post("/password")
async def change_password(
    body: PasswordChange, principal: Annotated[Principal, Depends(current_principal)]
) -> dict[str, Any]:
    """Change the password and end every existing session.

    The revocation is the point of the endpoint, not a side effect. A password is usually
    changed because somebody else may have it, and leaving their token working for another
    twelve hours would make the change cosmetic.
    """
    async with session_scope() as session:
        try:
            await service.change_password(
                session, principal.user_id, body.current_password, body.new_password
            )
        except WeakPassword as exc:
            raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        except service.AuthError as exc:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "email or password is incorrect"
            ) from exc
    return {"changed": True, "sessions_revoked": True}
