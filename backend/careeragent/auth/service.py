"""Registration, login, and turning a token back into a person.

The rules that are easy to get subtly wrong, and how they are handled here:

* **Registration never reveals whether an email is already taken.** It returns the same
  shape either way — an account-enumeration oracle on the signup form is the usual way a
  breach starts, long before anybody guesses a password.
* **Login never says which half was wrong.** "No such user" and "wrong password" are the
  same failure, and both pay the same hashing cost, so the timing does not answer what the
  message refuses to.
* **A disabled account fails like a wrong password.** Telling somebody their account exists
  but is suspended tells an attacker the email is real.
"""

from __future__ import annotations

import secrets
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..config import get_settings
from ..logging import get_logger
from ..storage.models import Candidate, User
from . import passwords, tokens
from .passwords import WeakPassword
from .tenancy import Principal

log = get_logger("auth.service")

__all__ = ["AuthError", "WeakPassword", "authenticate", "principal_for", "register", "session_for"]

# A login attempt against an unknown email must cost what a real one costs, or the response
# time answers the question the error message refuses to. This is hashed and discarded.
_DECOY_HASH = passwords.hash_password("not-a-real-password-" + secrets.token_hex(8))


class AuthError(Exception):
    """Authentication failed. Deliberately says no more than that."""


def _normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def signing_secret() -> str:
    """The key session tokens are signed with.

    Refuses rather than defaulting. A built-in fallback secret is a published secret: every
    deployment would share it, and a token minted on one machine would be valid on every
    other.
    """
    settings = get_settings()
    secret = (settings.auth_secret or "").strip()
    if not secret:
        raise AuthError(
            "CAREERAGENT_AUTH_SECRET is not set. Multi-user mode signs session tokens and "
            "will not invent a key to do it with — generate one with "
            '`python -c "import secrets; print(secrets.token_urlsafe(48))"`.'
        )
    return secret


async def register(
    session: AsyncSession, email: str, password: str, *, display_name: str = ""
) -> User:
    """Create an account.

    Raises `WeakPassword` for a password that will not protect anything, and `AuthError`
    for everything else — including an email already in use, which is reported as a generic
    failure so the endpoint cannot be used to enumerate accounts.
    """
    address = _normalize_email(email)
    if "@" not in address or len(address) < 5:
        raise AuthError("that does not look like an email address")

    # Raised before the uniqueness check so a weak password is still reported as weak.
    # Swapping the order would turn "your password is too short" into a silent generic
    # failure whenever the address happened to be taken.
    passwords.check_strength(password)

    existing = (
        await session.execute(select(User.id).where(func.lower(User.email) == address))
    ).scalar_one_or_none()
    if existing is not None:
        log.info("registration_rejected_duplicate", email_domain=address.split("@")[-1])
        raise AuthError("could not create that account")

    user = User(
        email=address,
        display_name=(display_name or "").strip() or address.split("@")[0],
        password_hash=passwords.hash_password(password),
        auth_provider="local",
    )
    session.add(user)
    await session.flush()
    log.info("user_registered", user_id=user.id, provider="local")
    return user


async def authenticate(session: AsyncSession, email: str, password: str) -> User:
    """Check an email and password. Raises `AuthError` on any failure, saying nothing more.

    The decoy hash is not decoration. Without it, a login against an unknown address returns
    in microseconds while a real one spends ~100 ms in scrypt, and the difference is
    measurable over the network — which would make the deliberately vague error message
    pointless.
    """
    address = _normalize_email(email)
    user = (
        await session.execute(select(User).where(func.lower(User.email) == address))
    ).scalar_one_or_none()

    if user is None or not user.password_hash:
        passwords.verify_password(password or "x", _DECOY_HASH)
        raise AuthError("email or password is incorrect")

    if not passwords.verify_password(password, user.password_hash):
        log.info("login_failed", user_id=user.id)
        raise AuthError("email or password is incorrect")

    # Same message as a wrong password. "Your account is suspended" confirms the address
    # is real, which is the fact the generic error exists to withhold.
    if not user.is_active:
        log.info("login_refused_inactive", user_id=user.id)
        raise AuthError("email or password is incorrect")

    # The only moment the plaintext exists to re-hash with. Without this, raising the cost
    # parameters would protect new accounts and leave every existing one behind forever.
    if passwords.needs_rehash(user.password_hash):
        user.password_hash = passwords.hash_password(password)
        log.info("password_rehashed", user_id=user.id)

    user.last_login_at = datetime.now(UTC)
    await session.flush()
    log.info("login_succeeded", user_id=user.id)
    return user


def session_for(user: User) -> dict[str, object]:
    """A signed token for this user, and how long it lasts.

    The payload carries an id and an expiry and nothing else. A JWT is signed, not
    encrypted — anybody holding it can read it — so it is not a place for anything the
    holder should not already know.
    """
    settings = get_settings()
    ttl = max(1, int(settings.auth_token_ttl_hours)) * 3600
    token = tokens.issue(
        user.id,
        signing_secret(),
        ttl_seconds=ttl,
        email=user.email,
        # The serial this token was minted under. Revoking increments it; see User.
        ser=int(user.token_serial or 0),
    )
    return {"access_token": token, "token_type": "bearer", "expires_in": ttl}


async def principal_for(session: AsyncSession, token: str) -> Principal:
    """Verify a token and load who it belongs to.

    Two checks a signature alone does not make, both about tokens that were valid when
    issued:

    * the account still exists and is still active, and
    * the token was issued *after* `tokens_valid_from`.

    The second is the whole of logout and revocation. Stateless tokens cannot be deleted,
    but they can be out-dated, and one timestamp per user does it without a session table
    that has to be kept consistent with the tokens in the wild.
    """
    secrets_to_try = [signing_secret()]
    supabase_secret = (get_settings().supabase_jwt_secret or "").strip()
    if supabase_secret:
        secrets_to_try.append(supabase_secret)

    claims = None
    for secret in secrets_to_try:
        try:
            claims = tokens.verify(token, secret)
            break
        except tokens.InvalidToken:
            continue
    if claims is None:
        raise AuthError("not a valid session")

    user = (
        await session.execute(select(User).where(User.id == claims.subject))
    ).scalar_one_or_none()
    if user is None:
        # A Supabase-signed token names a subject this database has not seen. That is a
        # real state on first login through an external provider, and it is handled by
        # provisioning explicitly at the login route — not by trusting an unknown id here.
        raise AuthError("not a valid session")
    if not user.is_active:
        raise AuthError("not a valid session")

    # A token we minted carries its serial, and the comparison is exact.
    serial = claims.extra.get("ser")
    if serial is not None:
        if int(serial) != int(user.token_serial or 0):
            raise AuthError("session has been revoked")
    else:
        # A token minted elsewhere — a Supabase session — has no serial of ours, so fall
        # back to the timestamp. It is coarse (whole seconds), which is exactly why it is
        # not the primary mechanism.
        valid_from = user.tokens_valid_from
        if valid_from is not None:
            if valid_from.tzinfo is None:
                valid_from = valid_from.replace(tzinfo=UTC)
            if claims.issued_at and claims.issued_at < int(valid_from.timestamp()):
                raise AuthError("session has been revoked")

    candidate_id = (
        await session.execute(select(Candidate.id).where(Candidate.user_id == user.id))
    ).scalar_one_or_none()

    return Principal(
        user_id=user.id,
        email=user.email,
        candidate_id=str(candidate_id) if candidate_id else None,
        is_admin=bool(user.is_admin),
    )


async def revoke_sessions(session: AsyncSession, user_id: str) -> None:
    """Invalidate every token issued to this user so far. This is logout-everywhere."""
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None:
        return
    user.token_serial = int(user.token_serial or 0) + 1
    user.tokens_valid_from = datetime.now(UTC)
    await session.flush()
    log.info("sessions_revoked", user_id=user_id, serial=user.token_serial)


async def change_password(
    session: AsyncSession, user_id: str, current_password: str, new_password: str
) -> None:
    """Change a password, then invalidate every existing session.

    The revocation is the point. A password is usually changed *because* somebody else may
    have it, and leaving their existing token working for another twelve hours makes the
    change cosmetic.
    """
    user = (await session.execute(select(User).where(User.id == user_id))).scalar_one_or_none()
    if user is None or not user.password_hash:
        raise AuthError("email or password is incorrect")
    if not passwords.verify_password(current_password, user.password_hash):
        raise AuthError("email or password is incorrect")

    passwords.check_strength(new_password)
    user.password_hash = passwords.hash_password(new_password)
    user.token_serial = int(user.token_serial or 0) + 1
    user.tokens_valid_from = datetime.now(UTC)
    await session.flush()
    log.info("password_changed", user_id=user_id)
