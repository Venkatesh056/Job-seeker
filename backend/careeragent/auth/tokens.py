"""Signed session tokens — HS256 JWTs, on the standard library.

A JWT rather than an opaque random string for one reason: Supabase issues HS256 JWTs signed
with the project's secret, so a verifier that understands this format can accept a Supabase
session *and* one this system issued, with the same code path and no second concept of
identity. Writing it out is about sixty lines; a dependency for sixty lines of `hmac` is a
bad trade.

Every documented JWT footgun is closed here deliberately:

* **`alg: none` is refused.** The algorithm is not read from the token at all — the header is
  checked to *equal* HS256, so a caller cannot choose their own verification.
* **The signature is compared in constant time**, over the exact bytes that were signed
  rather than a re-serialisation of the decoded payload. Re-encoding JSON is how signature
  checks pass on tokens whose contents differ.
* **Expiry is mandatory.** A token with no `exp` is rejected rather than treated as eternal.
* **Nothing secret goes in the payload.** A JWT is signed, not encrypted; anybody holding one
  can read it. It carries an id and an expiry.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass
from typing import Any

ALGORITHM = "HS256"
DEFAULT_TTL_SECONDS = 60 * 60 * 12
# Clocks on two machines are never identical. Thirty seconds forgives ordinary drift
# without meaningfully extending the life of an expired token.
LEEWAY_SECONDS = 30


class InvalidToken(Exception):
    """Unreadable, unsigned, tampered with, or expired.

    One exception for all of them on purpose. Telling a caller *which* of those it was tells
    an attacker which half of their guess was right.
    """


@dataclass(frozen=True)
class Claims:
    subject: str
    expires_at: int
    issued_at: int
    extra: dict[str, Any]

    @property
    def seconds_remaining(self) -> int:
        return max(0, self.expires_at - int(time.time()))


def _b64encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + padding)


def _sign(signing_input: bytes, secret: str) -> bytes:
    return hmac.new(secret.encode("utf-8"), signing_input, hashlib.sha256).digest()


def issue(
    subject: str,
    secret: str,
    *,
    ttl_seconds: int = DEFAULT_TTL_SECONDS,
    **extra: Any,
) -> str:
    """Sign a token for `subject`, valid for `ttl_seconds`."""
    if not secret:
        raise ValueError("cannot sign a token without a secret")
    now = int(time.time())
    header = {"alg": ALGORITHM, "typ": "JWT"}
    payload: dict[str, Any] = {"sub": subject, "iat": now, "exp": now + ttl_seconds, **extra}

    segments = [
        _b64encode(json.dumps(header, separators=(",", ":"), sort_keys=True).encode("utf-8")),
        _b64encode(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")),
    ]
    signing_input = ".".join(segments).encode("ascii")
    segments.append(_b64encode(_sign(signing_input, secret)))
    return ".".join(segments)


def verify(token: str, secret: str) -> Claims:
    """Check the signature and the expiry, then return the claims. Raises `InvalidToken`.

    The signature is verified over the token's own first two segments as received, not over
    anything re-serialised from the parsed payload.
    """
    if not secret:
        raise InvalidToken("no signing secret is configured")
    if not token or token.count(".") != 2:
        raise InvalidToken("not a token")

    header_segment, payload_segment, signature_segment = token.split(".")
    try:
        header = json.loads(_b64decode(header_segment))
        payload = json.loads(_b64decode(payload_segment))
        signature = _b64decode(signature_segment)
    except (ValueError, TypeError) as exc:
        raise InvalidToken("unreadable token") from exc

    # Read from our own constant, never from the token. A token that names its own
    # algorithm gets to choose how carefully it is checked.
    if not isinstance(header, dict) or header.get("alg") != ALGORITHM:
        raise InvalidToken("unsupported signature algorithm")
    if not isinstance(payload, dict):
        raise InvalidToken("unreadable token")

    expected = _sign(f"{header_segment}.{payload_segment}".encode("ascii"), secret)
    if not hmac.compare_digest(expected, signature):
        raise InvalidToken("signature does not match")

    subject = payload.get("sub")
    expires_at = payload.get("exp")
    if not isinstance(subject, str) or not subject:
        raise InvalidToken("token names no subject")
    # No expiry is not "never expires". An eternal session token is a credential that
    # cannot be revoked by waiting, and Supabase always sets one.
    if not isinstance(expires_at, int):
        raise InvalidToken("token has no expiry")
    if expires_at + LEEWAY_SECONDS < int(time.time()):
        raise InvalidToken("token has expired")

    reserved = {"sub", "exp", "iat"}
    return Claims(
        subject=subject,
        expires_at=expires_at,
        issued_at=int(payload.get("iat") or 0),
        extra={k: v for k, v in payload.items() if k not in reserved},
    )
