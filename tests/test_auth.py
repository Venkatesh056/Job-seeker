"""Passwords, tokens and the things they must refuse.

Every test here is a refusal. That is the shape of the subject: authentication code is
judged almost entirely by what it declines to do, and each of these cases is a documented
way the naive version is wrong.
"""

from __future__ import annotations

import time

import pytest

from careeragent.auth import passwords, tokens
from careeragent.auth.passwords import WeakPassword

SECRET = "test-signing-secret-not-used-anywhere-real"

# ─── Passwords ──────────────────────────────────────────────────────────────


def test_a_password_verifies_against_its_own_hash():
    encoded = passwords.hash_password("correct horse battery staple")
    assert passwords.verify_password("correct horse battery staple", encoded)


def test_the_wrong_password_does_not():
    encoded = passwords.hash_password("correct horse battery staple")
    assert not passwords.verify_password("correct horse battery stapler", encoded)


def test_the_same_password_hashes_differently_every_time():
    """Per-password salt. Identical hashes for identical passwords would let anyone with
    the table see which accounts share one, and would make a single rainbow table work
    against all of them at once."""
    a = passwords.hash_password("correct horse battery staple")
    b = passwords.hash_password("correct horse battery staple")
    assert a != b
    assert passwords.verify_password("correct horse battery staple", a)
    assert passwords.verify_password("correct horse battery staple", b)


def test_an_empty_password_is_refused_rather_than_hashed():
    """A blank secret that hashes successfully is an account with no password that looks
    exactly like an account with one."""
    with pytest.raises(WeakPassword):
        passwords.hash_password("")
    assert not passwords.verify_password("", passwords.hash_password("a real password"))


def test_a_short_password_is_refused():
    with pytest.raises(WeakPassword):
        passwords.hash_password("short")


def test_the_cost_parameters_travel_with_the_hash():
    """So raising them later does not invalidate every existing password: an old hash
    verifies against its own parameters, and is re-hashed on the next successful login."""
    encoded = passwords.hash_password("correct horse battery staple")
    scheme, n, r, p, salt, key = encoded.split("$")
    assert scheme == "scrypt"
    assert (int(n), int(r), int(p)) == (passwords.N, passwords.R, passwords.P)

    weaker = f"scrypt$16384${r}${p}${salt}${key}"
    assert passwords.needs_rehash(weaker)
    assert not passwords.needs_rehash(encoded)


def test_a_corrupt_stored_hash_is_false_rather_than_an_exception():
    """A row nobody can authenticate against is the safe reading. Raising would turn a
    corrupt record into a 500 that distinguishes it from a wrong password."""
    for junk in ("", "not-a-hash", "scrypt$$$$", "argon2$1$2$3$aa$bb", "scrypt$x$y$z$aa$bb"):
        assert passwords.verify_password("anything", junk) is False


# ─── Tokens ─────────────────────────────────────────────────────────────────


def test_a_token_round_trips():
    token = tokens.issue("user-1", SECRET, ttl_seconds=60, email="a@example.com")
    claims = tokens.verify(token, SECRET)
    assert claims.subject == "user-1"
    assert claims.extra["email"] == "a@example.com"
    assert 0 < claims.seconds_remaining <= 60


def test_a_token_signed_with_another_secret_is_refused():
    token = tokens.issue("user-1", SECRET, ttl_seconds=60)
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(token, "a-different-secret-entirely")


def test_a_tampered_payload_is_refused():
    """The signature is checked over the segments as received, not over a re-serialisation
    of the decoded payload — re-encoding JSON is how signature checks pass on tokens whose
    contents differ."""
    import base64
    import json

    token = tokens.issue("user-1", SECRET, ttl_seconds=60)
    header, payload, signature = token.split(".")
    decoded = json.loads(base64.urlsafe_b64decode(payload + "=="))
    decoded["sub"] = "user-2"
    forged = base64.urlsafe_b64encode(json.dumps(decoded).encode()).rstrip(b"=").decode()
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(f"{header}.{forged}.{signature}", SECRET)


def test_alg_none_is_refused():
    """The classic. A verifier that reads the algorithm out of the token lets the token
    choose how carefully it is checked; this one reads it from its own constant."""
    import base64
    import json

    def segment(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    header = segment({"alg": "none", "typ": "JWT"})
    payload = segment({"sub": "user-1", "exp": int(time.time()) + 600, "iat": int(time.time())})
    for forged in (f"{header}.{payload}.", f"{header}.{payload}.x"):
        with pytest.raises(tokens.InvalidToken):
            tokens.verify(forged, SECRET)


def test_an_expired_token_is_refused():
    token = tokens.issue("user-1", SECRET, ttl_seconds=-120)
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(token, SECRET)


def test_a_token_with_no_expiry_is_refused():
    """No expiry is not "never expires". An eternal credential cannot be revoked by
    waiting, which is the only revocation a stateless token has."""
    import base64
    import hashlib
    import hmac
    import json

    def segment(value: dict) -> str:
        return base64.urlsafe_b64encode(json.dumps(value).encode()).rstrip(b"=").decode()

    header = segment({"alg": "HS256", "typ": "JWT"})
    payload = segment({"sub": "user-1", "iat": int(time.time())})
    signing_input = f"{header}.{payload}".encode("ascii")
    signature = hmac.new(SECRET.encode(), signing_input, hashlib.sha256).digest()
    token = f"{header}.{payload}.{base64.urlsafe_b64encode(signature).rstrip(b'=').decode()}"

    with pytest.raises(tokens.InvalidToken):
        tokens.verify(token, SECRET)


def test_nonsense_is_refused_without_raising_something_else():
    for junk in ("", "abc", "a.b", "a.b.c.d", "....", "a.b.c"):
        with pytest.raises(tokens.InvalidToken):
            tokens.verify(junk, SECRET)


def test_signing_without_a_secret_is_refused():
    """A default signing key is a published one: every deployment would share it, and a
    token minted on one machine would be valid on every other."""
    with pytest.raises(ValueError):
        tokens.issue("user-1", "")
    with pytest.raises(tokens.InvalidToken):
        tokens.verify(tokens.issue("user-1", SECRET), "")
