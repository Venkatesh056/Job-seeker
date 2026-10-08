"""Password hashing.

`hashlib.scrypt` from the standard library rather than a dependency. scrypt is memory-hard,
it is in every Python since 3.6, and adding bcrypt or argon2 here would trade a supply-chain
dependency for a difference nobody in this system can measure.

Three things this module will not do, each because the failure is silent:

* **Return a plain comparison.** Verification goes through `hmac.compare_digest`, so a wrong
  password takes the same time whether it was wrong in the first character or the last.
* **Store parameters separately from the hash.** The cost parameters live *in* the encoded
  string, so raising them later does not invalidate every existing password — an old hash
  verifies against its own parameters and is re-hashed on the next successful login.
* **Accept an empty password.** A blank secret that hashes successfully is an account with
  no password that looks like an account with one.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets

# OWASP's 2023 floor for scrypt is n=2^17, r=8, p=1. That costs roughly 128 MB and ~100 ms,
# which is the point: it is the attacker's cost too, multiplied by every guess.
N = 2**17
R = 8
P = 1
SALT_BYTES = 16
KEY_BYTES = 32
# scrypt needs n * r * 128 bytes; CPython's default OpenSSL maxmem is too small for these
# parameters and raises rather than allocating.
MAXMEM = 256 * 1024 * 1024

SCHEME = "scrypt"
MIN_LENGTH = 10


class WeakPassword(ValueError):
    """Raised rather than silently accepting something that will not protect anything."""


def check_strength(password: str) -> None:
    """The rules, stated once so registration and password change cannot disagree.

    Length only. Composition rules ("one capital, one symbol") push people towards
    `Password1!` — they measurably reduce entropy while feeling stricter, and NIST dropped
    them for exactly that reason.
    """
    if not password:
        raise WeakPassword("a password is required")
    if len(password) < MIN_LENGTH:
        raise WeakPassword(f"at least {MIN_LENGTH} characters, and longer is better than odder")


def hash_password(password: str) -> str:
    """`scrypt$n$r$p$salt$key`, all parameters carried with the hash."""
    check_strength(password)
    salt = secrets.token_bytes(SALT_BYTES)
    key = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=N, r=R, p=P, dklen=KEY_BYTES, maxmem=MAXMEM
    )
    return f"{SCHEME}${N}${R}${P}${salt.hex()}${key.hex()}"


def verify_password(password: str, encoded: str) -> bool:
    """Constant-time check against a stored hash.

    A malformed stored hash returns False rather than raising. A row that cannot be parsed
    is a row nobody can authenticate against, which is the safe reading; raising here would
    turn a corrupt record into a 500 that distinguishes it from a wrong password.
    """
    if not password or not encoded:
        return False
    try:
        scheme, n, r, p, salt_hex, key_hex = encoded.split("$")
        if scheme != SCHEME:
            return False
        candidate = hashlib.scrypt(
            password.encode("utf-8"),
            salt=bytes.fromhex(salt_hex),
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(bytes.fromhex(key_hex)),
            maxmem=MAXMEM,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, bytes.fromhex(key_hex))


def needs_rehash(encoded: str) -> bool:
    """True when a stored hash was made with weaker parameters than the current ones.

    Called after a *successful* login, which is the only moment the plaintext is available
    to re-hash with. Without this, raising the cost parameters would protect new accounts
    and leave every existing one at the old setting forever.
    """
    try:
        scheme, n, r, p, _salt, _key = encoded.split("$")
    except (ValueError, AttributeError):
        return True
    return scheme != SCHEME or (int(n), int(r), int(p)) != (N, R, P)
