"""The application state machine.

Pure and table-driven, so these are exhaustive rather than representative: every
declared state is checked for reachability and terminality, and the two invariants
that protect the user — APPLIED needs evidence, terminal means terminal — are tested
directly rather than inferred from a happy path.
"""

from __future__ import annotations

import itertools

import pytest

from careeragent.applications.states import (
    BOARD_COLUMNS,
    MAX_ATTEMPTS,
    NEEDS_HUMAN,
    RESPONDED_STATES,
    SUBMITTED_STATES,
    TERMINAL,
    TRANSITIONS,
    IllegalTransition,
    board_column,
    can_transition,
    is_open,
    transition,
)
from careeragent.storage.models import ApplicationStatus as S
from careeragent.storage.safety import (
    Environment,
    UnsafeDatabaseOperation,
    assert_destructive_allowed,
    current_environment,
    is_disposable,
)

# ─── Table integrity ────────────────────────────────────────────────────────


def test_every_status_has_a_row():
    """A status missing from the table would silently have no legal moves."""
    assert set(TRANSITIONS) == set(S)


def test_every_target_is_a_real_status():
    for source, targets in TRANSITIONS.items():
        for target in targets:
            assert isinstance(target, S), f"{source} -> {target}"


def test_terminal_states_have_no_exits():
    for status in TERMINAL:
        assert TRANSITIONS[status] == frozenset(), status


def test_every_status_appears_on_the_board():
    """An application in a status no column covers would vanish from the tracker."""
    covered = {status for _, members in BOARD_COLUMNS for status in members}
    assert covered == set(S)


def test_board_column_is_total():
    for status in S:
        assert board_column(status) in {name for name, _ in BOARD_COLUMNS}


# ─── The evidence invariant ─────────────────────────────────────────────────


def test_applied_requires_evidence():
    with pytest.raises(IllegalTransition, match="requires submission evidence"):
        transition(S.APPLYING, S.APPLIED)


def test_applied_with_evidence_is_allowed():
    result = transition(S.APPLYING, S.APPLIED, evidence={"confirmation_text": "thank you"})
    assert result.status is S.APPLIED
    assert result.payload["evidence"]["confirmation_text"] == "thank you"


def test_unverified_needs_no_evidence():
    """The whole point of the state: we clicked, we saw nothing, we say so."""
    assert transition(S.APPLYING, S.SUBMISSION_UNVERIFIED).status is S.SUBMISSION_UNVERIFIED


def test_unverified_can_be_confirmed_by_a_human():
    result = transition(S.SUBMISSION_UNVERIFIED, S.APPLIED, evidence={"confirmed_by": "user"})
    assert result.status is S.APPLIED


def test_unverified_is_not_counted_as_submitted():
    """Counting it would overstate the funnel and hide a submission that never landed."""
    assert S.SUBMISSION_UNVERIFIED not in SUBMITTED_STATES
    assert S.SUBMISSION_UNVERIFIED in NEEDS_HUMAN


# ─── Terminality ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", sorted(TERMINAL, key=str))
def test_terminal_states_refuse_to_move(status):
    with pytest.raises(IllegalTransition, match="terminal"):
        transition(status, S.APPLYING)


def test_terminal_state_can_reassert_itself():
    """Idempotent: re-asserting the current state is not an error."""
    assert transition(S.OFFER, S.OFFER).status is S.OFFER


def test_is_open():
    assert is_open(S.AWAITING_APPROVAL)
    assert not is_open(S.OFFER)
    assert not is_open(S.REJECTED)


# ─── Retries ────────────────────────────────────────────────────────────────


def test_retry_below_the_cap_is_allowed():
    assert transition(S.FAILED, S.APPLYING, attempts=1).status is S.APPLYING


def test_retry_at_the_cap_becomes_terminal():
    """Redirected rather than refused: the caller's intent is unambiguous."""
    result = transition(S.FAILED, S.APPLYING, attempts=MAX_ATTEMPTS)
    assert result.status is S.FAILED_TERMINAL
    assert "gave up" in result.payload["reason"]


# ─── Illegal moves ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "target"),
    [
        (S.DISCOVERED, S.APPLIED),  # no skipping straight to applied
        (S.DISCOVERED, S.APPLYING),  # no applying to something never prepared
        (S.SHORTLISTED, S.OFFER),  # no offer without an application
        (S.APPLIED, S.APPLYING),  # no re-submitting a submitted application
        (S.REJECTED_BY_COMPANY, S.INTERVIEW),
    ],
)
def test_illegal_transitions_raise(source, target):
    assert not can_transition(source, target)
    with pytest.raises(IllegalTransition):
        transition(source, target)


def test_illegal_transition_names_the_legal_options():
    """The error is what a user sees; it has to say what they can do instead."""
    with pytest.raises(IllegalTransition) as info:
        transition(S.DISCOVERED, S.APPLIED)
    assert "shortlisted" in str(info.value)


def test_rejected_can_be_reconsidered():
    """A user who changes their mind should not have to re-discover the job."""
    assert can_transition(S.REJECTED, S.SHORTLISTED)


def test_responded_states_are_a_subset_of_submitted():
    assert RESPONDED_STATES <= SUBMITTED_STATES


def test_happy_path_end_to_end():
    """The full sequence the brief describes, each hop validated by the table."""
    path = [
        S.DISCOVERED,
        S.SHORTLISTED,
        S.RESUME_TAILORED,
        S.READY,
        S.AWAITING_APPROVAL,
        S.APPLYING,
    ]
    for source, target in itertools.pairwise(path):
        assert transition(source, target).status is target
    assert transition(S.APPLYING, S.APPLIED, evidence={"url_after": "/thanks"}).status is S.APPLIED
    for source, target in ((S.APPLIED, S.INTERVIEW), (S.INTERVIEW, S.OFFER)):
        assert transition(source, target).status is target


# ─── Dropping a database you did not mean to drop ───────────────────────────
#
# This guard exists because it was needed. A test that touched storage was written without
# the fixture that redirects CAREERAGENT_DATABASE_URL at a temp file, so a routine test run
# called init_db(drop=True) against a real database and destroyed 1,756 jobs, 67 researched
# companies and 81 boards. Nothing in the test's own code looked wrong — the fixture was
# simply absent, and absence is what a reviewer does not see.
#
# Two conditions must now both hold, because either alone is reachable by accident: a stale
# CAREERAGENT_ENV=test in a shell pointed at the real file, or a temp path on a real host.


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///:memory:",
        "sqlite://",
        "sqlite+aiosqlite:///file:x?mode=memory&cache=shared",
    ],
)
def test_an_in_memory_database_is_disposable(url):
    assert is_disposable(url)


def test_a_database_under_the_temp_directory_is_disposable(tmp_path):
    assert is_disposable(f"sqlite+aiosqlite:///{tmp_path / 'anything.db'}")


@pytest.mark.parametrize(
    "url",
    [
        "sqlite+aiosqlite:///data/careeragent.db",
        "sqlite+aiosqlite:///D:/Joby agent/data/careeragent.db",
        "postgresql+asyncpg://user:pass@localhost/careeragent",
        "postgresql+asyncpg://user:pass@localhost/careeragent_prod",
        "sqlite+aiosqlite:///",
        "",
    ],
)
def test_a_real_database_is_not_disposable(url):
    assert not is_disposable(url)


def test_a_postgres_database_named_test_is_disposable():
    """The name is the only honest signal. A production database is routinely reached
    over localhost, so host and credentials prove nothing."""
    assert is_disposable("postgresql+asyncpg://u:p@localhost/careeragent_test")
    assert not is_disposable("postgresql+asyncpg://u:p@localhost/careeragent")


def test_an_undeclared_environment_still_cannot_be_dropped(monkeypatch):
    """The incident path is an *unforced* drop, and that is refused everywhere but `test`.
    So an undeclared environment does not need to read as production to be safe — it needs
    to read as something that is not `test`, which development is."""
    monkeypatch.delenv("CAREERAGENT_ENV", raising=False)

    assert current_environment() is Environment.DEVELOPMENT
    assert not current_environment().destructive_allowed


def test_an_unreadable_declaration_is_treated_as_production(monkeypatch):
    """A value nobody can parse is a sign something is misconfigured, and that is the
    setting where the most is refused."""
    monkeypatch.setenv("CAREERAGENT_ENV", "nonsense")
    assert current_environment() is Environment.PRODUCTION


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        ("dev", Environment.DEVELOPMENT),
        ("prod", Environment.PRODUCTION),
        ("test", Environment.TEST),
        ("staging", Environment.STAGING),
        ("PROD", Environment.PRODUCTION),
    ],
)
def test_both_spellings_of_each_environment_are_accepted(declared, expected, monkeypatch):
    """`dev` and `prod` are what the settings file has always used."""
    monkeypatch.setenv("CAREERAGENT_ENV", declared)
    assert current_environment() is expected


def test_only_a_test_environment_may_be_dropped():
    assert Environment.TEST.destructive_allowed
    for other in (Environment.PRODUCTION, Environment.STAGING, Environment.DEVELOPMENT):
        assert not other.destructive_allowed, (
            "development is deliberately excluded — a developer's own database holds the "
            "corpus they have been building for weeks, which is exactly what was lost"
        )


def test_a_test_environment_pointed_at_a_real_database_is_still_refused(monkeypatch):
    """The stale-shell case. Declaring 'test' does not make the file disposable."""
    monkeypatch.setenv("CAREERAGENT_ENV", "test")

    with pytest.raises(UnsafeDatabaseOperation, match="not in-memory"):
        assert_destructive_allowed("sqlite+aiosqlite:///data/careeragent.db", "drop")


def test_a_temp_database_in_a_production_environment_is_still_refused(monkeypatch, tmp_path):
    """The mirror case: a temp path on a production host."""
    monkeypatch.setenv("CAREERAGENT_ENV", "production")

    with pytest.raises(UnsafeDatabaseOperation, match="production"):
        assert_destructive_allowed(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", "drop")


def test_both_conditions_together_are_allowed(monkeypatch, tmp_path):
    monkeypatch.setenv("CAREERAGENT_ENV", "test")
    assert_destructive_allowed(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", "drop")


def test_force_still_refuses_a_declared_production_environment(monkeypatch, tmp_path):
    """A prompt the user answered with a stale CAREERAGENT_ENV is not informed consent."""
    monkeypatch.setenv("CAREERAGENT_ENV", "production")

    with pytest.raises(UnsafeDatabaseOperation):
        assert_destructive_allowed(f"sqlite+aiosqlite:///{tmp_path / 'x.db'}", "drop", force=True)


def test_force_works_for_a_deliberate_reset_outside_production(monkeypatch):
    """`careeragent init --reset` asks the user first, then forces."""
    monkeypatch.setenv("CAREERAGENT_ENV", "development")
    assert_destructive_allowed("sqlite+aiosqlite:///data/careeragent.db", "drop", force=True)


def test_the_refusal_never_prints_a_password(monkeypatch):
    monkeypatch.setenv("CAREERAGENT_ENV", "development")
    url = "postgresql+asyncpg://admin:hunter2@db.internal/careeragent"

    with pytest.raises(UnsafeDatabaseOperation) as caught:
        assert_destructive_allowed(url, "drop")

    assert "hunter2" not in str(caught.value)
    assert "admin" not in str(caught.value)


async def test_init_db_refuses_a_real_database(tmp_path, monkeypatch, anyio_backend):
    """The whole point, exercised through the function that caused the incident."""
    from careeragent import config as config_module
    from careeragent import storage

    monkeypatch.setenv("CAREERAGENT_ENV", "development")
    monkeypatch.setenv(
        "CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'careeragent.db'}"
    )
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]

    try:
        with pytest.raises(UnsafeDatabaseOperation):
            await storage.init_db(drop=True)
    finally:
        monkeypatch.setenv("CAREERAGENT_ENV", "test")
        config_module.get_settings.cache_clear()
        storage.db._engine = None  # type: ignore[attr-defined]
        storage.db._session_factory = None  # type: ignore[attr-defined]


async def test_init_db_allows_a_test_database(tmp_path, anyio_backend, monkeypatch):
    from careeragent import config as config_module
    from careeragent import storage

    monkeypatch.setenv("CAREERAGENT_ENV", "test")
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'r.db'}")
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]

    try:
        await storage.init_db(drop=True)
    finally:
        config_module.get_settings.cache_clear()
        storage.db._engine = None  # type: ignore[attr-defined]
        storage.db._session_factory = None  # type: ignore[attr-defined]
