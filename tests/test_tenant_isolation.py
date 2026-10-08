"""Two accounts on one database, and the line between them.

This is the test file the phase exists for. Everything else in multi-user support is
plumbing; the question that matters is whether one person's session can read another
person's resume, applications, scores or do-not-contact list.

It is asked through the **HTTP API**, not against the repository. A unit test that calls
`get_or_create_candidate` with a principal bound proves the function respects an argument;
it proves nothing about the forty endpoints that never pass one. The bug being guarded
against is an endpoint that forgets, and only a request can catch that.

Every assertion is negative, and deliberately so. "Bob sees his own data" is satisfied by a
system with no isolation at all.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind

SECRET = "isolation-test-secret-not-used-anywhere-real"
PASSWORD = "a long enough password"


@pytest.fixture
def multi_user(tmp_path, monkeypatch):
    """A throwaway database with identity enforced."""
    monkeypatch.setenv("CAREERAGENT_ENV", "test")
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'iso.db'}")
    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("CAREERAGENT_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("CAREERAGENT_MULTI_USER", "true")
    monkeypatch.setenv("CAREERAGENT_AUTH_SECRET", SECRET)

    from careeragent import config as config_module
    from careeragent import storage

    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
    yield tmp_path
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]


def _fact(label: str) -> Fact:
    return Fact(
        kind=FactKind.SKILL,
        label=label,
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#page=1",
    )


async def _account(client: TestClient, email: str, *, name: str, skill: str) -> str:
    """Register an account, then give it a profile with one distinctive skill.

    The skill is the marker. If a later request returns it to the wrong caller, the leak is
    unambiguous rather than a matter of counting rows.
    """
    from careeragent.auth import service, tenancy
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    registered = client.post("/api/v1/auth/register", json={"email": email, "password": PASSWORD})
    assert registered.status_code == 201, registered.text

    logged_in = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert logged_in.status_code == 200, logged_in.text
    token = logged_in.json()["access_token"]

    async with session_scope() as session:
        principal = await service.principal_for(session, token)
    # Bind explicitly for the seeding, exactly as a request would. Seeding through the
    # repository without a principal would write the profile to whoever happened to be
    # first in the table, which is the bug under test.
    with tenancy.bind(principal):
        async with session_scope() as session:
            await repo.save_candidate_profile(
                session,
                Candidate(name=name, email=email, facts=[_fact(skill)]),
            )
            row = await repo.get_or_create_candidate(session)
            await repo.replace_facts(session, row.id, [_fact(skill)])
    return token


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
async def two_accounts(multi_user):
    """Alice and Bob, each with their own profile, on one database."""
    from careeragent.api import app
    from careeragent.storage import init_db

    await init_db(drop=True)
    with TestClient(app) as client:
        alice = await _account(client, "alice@example.com", name="Alice", skill="haskell")
        bob = await _account(client, "bob@example.com", name="Bob", skill="fortran")
        yield client, alice, bob


# ─── The line ───────────────────────────────────────────────────────────────


async def test_each_account_sees_only_its_own_profile(two_accounts):
    client, alice, bob = two_accounts

    a = client.get("/api/v1/candidate", headers=_auth(alice)).json()
    b = client.get("/api/v1/candidate", headers=_auth(bob)).json()

    assert a["name"] == "Alice"
    assert b["name"] == "Bob"
    assert a["email"] != b["email"]


async def test_one_account_never_sees_the_others_facts(two_accounts):
    """The Fact Bank is the most sensitive thing here: it is the resume, parsed."""
    client, alice, bob = two_accounts

    a = client.get("/api/v1/candidate/facts", headers=_auth(alice)).json()
    b = client.get("/api/v1/candidate/facts", headers=_auth(bob)).json()

    a_labels = {f["label"] for f in (a if isinstance(a, list) else a.get("facts", []))}
    b_labels = {f["label"] for f in (b if isinstance(b, list) else b.get("facts", []))}

    assert "haskell" in a_labels and "fortran" not in a_labels
    assert "fortran" in b_labels and "haskell" not in b_labels


async def test_a_second_account_does_not_inherit_the_first_ones_profile(two_accounts):
    """The specific failure the old lookup would have produced.

    `select(Candidate).limit(1)` returns whichever row was inserted first — so Bob, the
    second account to register, would have been served Alice's profile for every request he
    ever made, and nothing would have looked broken.
    """
    client, _alice, bob = two_accounts
    body = client.get("/api/v1/candidate", headers=_auth(bob)).json()
    assert body["name"] == "Bob", "the second account was served the first account's profile"


async def test_a_do_not_contact_list_is_not_shared(two_accounts):
    """It leaks in two directions at once: Alice's opt-out would silence Bob's outreach to
    somebody who never spoke to him, and the list itself records who Alice wrote to and who
    told her to stop."""
    client, alice, bob = two_accounts

    added = client.post(
        "/api/v1/outreach/suppressed",
        headers=_auth(alice),
        json={"reason": "opted_out", "email": "someone@example.com", "note": "asked to stop"},
    )
    assert added.status_code in (200, 201), added.text

    mine = client.get("/api/v1/outreach/suppressed", headers=_auth(alice)).json()
    theirs = client.get("/api/v1/outreach/suppressed", headers=_auth(bob)).json()

    def addresses(payload) -> set[str]:
        rows = payload if isinstance(payload, list) else payload.get("suppressed", [])
        return {str(r.get("target") or r.get("email") or "") for r in rows}

    assert addresses(mine), "the account that added it cannot see it"
    assert not addresses(theirs), "another account can read this do-not-contact list"


# ─── No token, bad token, stale token ───────────────────────────────────────


async def test_an_unauthenticated_request_is_refused(two_accounts):
    client, _alice, _bob = two_accounts
    for path in ("/api/v1/candidate", "/api/v1/candidate/facts", "/api/v1/auth/me"):
        assert client.get(path).status_code == 401, f"{path} served an anonymous caller"


async def test_a_forged_token_is_refused(two_accounts):
    client, _alice, _bob = two_accounts
    from careeragent.auth import tokens

    forged = tokens.issue("some-user-id", "a-secret-this-deployment-does-not-use")
    assert client.get("/api/v1/candidate", headers=_auth(forged)).status_code == 401


async def test_a_token_for_a_deleted_account_is_refused(two_accounts):
    """A signature check alone would still pass: the token was validly signed when it was
    issued, and nothing about it changes when the account goes away."""
    client, alice, _bob = two_accounts
    from sqlalchemy import select

    from careeragent.storage import session_scope
    from careeragent.storage.models import User

    async with session_scope() as session:
        user = (
            await session.execute(select(User).where(User.email == "alice@example.com"))
        ).scalar_one()
        user.is_active = False

    assert client.get("/api/v1/candidate", headers=_auth(alice)).status_code == 401


async def test_logging_out_refuses_every_existing_token(two_accounts):
    """Stateless tokens cannot be deleted, so they are out-dated instead. Somebody logs out
    because they think a session is compromised — and that is exactly the session they
    cannot call from, so logging out only the calling token would be the useless behaviour.

    This test failed the first time it ran in the full suite and passed on its own, which is
    what exposed the timestamp bug: `iat` is whole seconds, so a token issued in the same
    second as the logout compared equal to the cutoff and survived. Revocation is a serial
    now, and there is no clock left in it to lose a race with.
    """
    client, alice, bob = two_accounts

    assert client.get("/api/v1/auth/me", headers=_auth(alice)).status_code == 200
    assert client.post("/api/v1/auth/logout", headers=_auth(alice)).status_code == 200
    assert client.get("/api/v1/auth/me", headers=_auth(alice)).status_code == 401
    # And only that account's sessions.
    assert client.get("/api/v1/auth/me", headers=_auth(bob)).status_code == 200


async def test_logging_out_twice_in_the_same_second_still_revokes(two_accounts):
    """The regression, pinned without a sleep.

    Log in and log out back to back, which on any reasonably fast machine happens inside one
    second. With the old whole-second comparison this passed only when the machine was slow.
    """
    client, _alice, _bob = two_accounts

    fresh = client.post(
        "/api/v1/auth/login", json={"email": "bob@example.com", "password": PASSWORD}
    ).json()["access_token"]
    assert client.post("/api/v1/auth/logout", headers=_auth(fresh)).status_code == 200
    assert client.get("/api/v1/auth/me", headers=_auth(fresh)).status_code == 401


async def test_changing_a_password_ends_existing_sessions(two_accounts):
    client, alice, _bob = two_accounts
    changed = client.post(
        "/api/v1/auth/password",
        headers=_auth(alice),
        json={"current_password": PASSWORD, "new_password": "a different long password"},
    )
    assert changed.status_code == 200, changed.text
    assert client.get("/api/v1/auth/me", headers=_auth(alice)).status_code == 401

    again = client.post(
        "/api/v1/auth/login",
        json={"email": "alice@example.com", "password": "a different long password"},
    )
    assert again.status_code == 200


# ─── What the error messages are allowed to say ─────────────────────────────


async def test_registration_does_not_reveal_that_an_email_is_taken(two_accounts):
    """An enumeration oracle on a signup form is how a credential-stuffing run picks its
    targets."""
    client, _alice, _bob = two_accounts
    response = client.post(
        "/api/v1/auth/register",
        json={"email": "alice@example.com", "password": "another long password"},
    )
    assert response.status_code == 400
    body = response.text.lower()
    assert "exists" not in body and "taken" not in body and "already" not in body


async def test_login_says_the_same_thing_for_a_wrong_password_and_an_unknown_account(
    two_accounts,
):
    client, _alice, _bob = two_accounts
    wrong = client.post(
        "/api/v1/auth/login", json={"email": "alice@example.com", "password": "not her password"}
    )
    unknown = client.post(
        "/api/v1/auth/login", json={"email": "nobody@example.com", "password": "not her password"}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json()["detail"] == unknown.json()["detail"]


async def test_no_response_anywhere_contains_a_password_hash(two_accounts):
    """Asserted on the wire rather than on the response models. A model can grow a field;
    this catches it the moment it does."""
    client, alice, _bob = two_accounts
    for path in ("/api/v1/auth/me", "/api/v1/candidate", "/api/v1/candidate/facts"):
        body = client.get(path, headers=_auth(alice)).text
        assert "scrypt$" not in body
        assert "password_hash" not in body


# ─── The refusal that replaces the old fallback ─────────────────────────────


async def test_unowned_work_raises_instead_of_reading_the_first_row(multi_user):
    """The heart of it.

    In multi-user mode, database work that reaches the repository with nobody bound must
    raise. A silent fallback to `limit(1)` would pass every test in this file — the request
    path binds a principal — and would serve the wrong person's data from every background
    task, CLI invocation and scheduled job that forgot to.
    """
    from careeragent.auth.tenancy import NoPrincipal
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo

    await init_db(drop=True)
    with pytest.raises(NoPrincipal):
        async with session_scope() as session:
            await repo.load_candidate(session)


async def test_single_user_mode_still_falls_back(tmp_path, monkeypatch):
    """The other half of the same decision. With identity off there is one profile, falling
    back to it is correct, and demanding a login to read your own resume on your own laptop
    would be theatre with a real cost."""
    monkeypatch.setenv("CAREERAGENT_ENV", "test")
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 's.db'}")
    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("CAREERAGENT_MULTI_USER", "false")

    from careeragent import config as config_module
    from careeragent import storage

    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
    try:
        from careeragent.storage import init_db, session_scope
        from careeragent.storage import repository as repo

        await init_db(drop=True)
        async with session_scope() as session:
            row = await repo.get_or_create_candidate(session, name="Solo")
            assert row.name == "Solo"
        async with session_scope() as session:
            again = await repo.get_or_create_candidate(session)
            assert again.id == row.id, "single-user mode created a second profile"
    finally:
        config_module.get_settings.cache_clear()
        storage.db._engine = None  # type: ignore[attr-defined]
        storage.db._session_factory = None  # type: ignore[attr-defined]


# ─── Relevance belongs to whoever it was computed for ───────────────────────


async def test_two_candidates_get_independent_relevance_for_the_same_person(multi_user):
    """The Phase 5 leak, closed.

    `PersonRecord.relevance` is a single slot on a shared row, so the last ranking to run
    overwrote everybody else's. With one user that is untidy; with two it means opening the
    People screen and reading a 96 that was computed for somebody else's career against
    somebody else's target role.

    The people themselves are genuinely shared corpus — Karya's engineering manager is the
    same human for everybody. How relevant they are *to you* is not.
    """
    from careeragent.models.person import Person
    from careeragent.people import store as people_store
    from careeragent.people.relevance import rank
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo
    from careeragent.storage.models import Company

    await init_db(drop=True)
    async with session_scope() as session:
        session.add(Company(id="c-shared", name="Shared Co", normalized_name="shared co"))

    people = [Person(name="Dana Roy", title="Engineering Manager, ML", company="Shared Co")]
    async with session_scope() as session:
        await people_store.save(session, "c-shared", "Shared Co", people)

    owners: dict[str, str] = {}
    for email, role in [
        ("ml@example.com", "Machine Learning Engineer"),
        ("sales@example.com", "Enterprise Account Executive"),
    ]:
        from careeragent.auth import service, tenancy

        async with session_scope() as session:
            user = await service.register(session, email, PASSWORD)
            principal = tenancy.Principal(user_id=user.id, email=user.email)

        with tenancy.bind(principal):
            async with session_scope() as session:
                row = await repo.get_or_create_candidate(session, name=email.split("@")[0])
                owners[role] = row.id
                ranked = rank(people, target_role=role)
                await people_store.record_relevance(
                    session, "c-shared", ranked, candidate_id=row.id
                )

    async with session_scope() as session:
        person_id = (await people_store.search(session, limit=1))[0].id
        ml = await people_store.relevance_for(
            session, person_id, owners["Machine Learning Engineer"]
        )
        sales = await people_store.relevance_for(
            session, person_id, owners["Enterprise Account Executive"]
        )

    assert ml is not None and sales is not None
    # An engineering manager is not equally relevant to an ML candidate and a sales one.
    # The exact numbers belong to the ranker's tests; what matters here is that there are
    # two of them and they did not overwrite each other.
    assert ml["relevance"] != sales["relevance"], (
        "both candidates read the same score — relevance is still shared"
    )


async def test_a_person_nobody_ranked_for_you_reads_as_unknown_not_zero(multi_user):
    """None rather than 0. "Nobody has ranked this person for you" and "this person is
    irrelevant to you" are different facts, and the whole system is built on not confusing
    them."""
    from careeragent.auth import service, tenancy
    from careeragent.models.person import Person
    from careeragent.people import store as people_store
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo
    from careeragent.storage.models import Company

    await init_db(drop=True)
    async with session_scope() as session:
        session.add(Company(id="c-q", name="Quiet Co", normalized_name="quiet co"))
    async with session_scope() as session:
        await people_store.save(
            session, "c-q", "Quiet Co", [Person(name="Eli Vance", title="CTO", company="Quiet Co")]
        )
        person_id = (await people_store.search(session, limit=1))[0].id

    async with session_scope() as session:
        user = await service.register(session, "newcomer@example.com", PASSWORD)
        principal = tenancy.Principal(user_id=user.id, email=user.email)

    with tenancy.bind(principal):
        async with session_scope() as session:
            row = await repo.get_or_create_candidate(session, name="Newcomer")
            assert await people_store.relevance_for(session, person_id, row.id) is None
