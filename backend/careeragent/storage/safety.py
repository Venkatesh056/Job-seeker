"""Which database is this, and what is it allowed to have done to it?

This module exists because of a specific incident, and the incident is worth stating
plainly: a test called `init_db(drop=True)` without the fixture that redirects
`CAREERAGENT_DATABASE_URL` at a temporary file. A routine `pytest` run therefore dropped
every table in a real database, destroying 1,756 jobs, 67 researched companies, 81 ATS
boards and every score.

The test's own code looked correct. Nothing about it was obviously wrong — the fixture was
simply *absent*, and absence is what neither a reviewer nor a linter sees.

So safety here is not advice. It is a refusal, and it is deliberately hard to satisfy by
accident:

**Only a `test` environment may be dropped without an explicit force.** `CAREERAGENT_ENV`
says `production`, `staging`, `development` or `test`. Development is deliberately *not*
permitted: a developer's own database holds the corpus they have been building for weeks,
which is exactly what was lost. An unreadable declaration is treated as production.

**A URL is disposable or it is not.** In-memory, or a file under a temp directory, or a
name that says `test`. Everything else is somebody's data, whatever the environment claims.

**Both must agree.** A destructive operation needs a non-production environment *and* a
disposable URL. Either alone is a state a developer can reach by accident: `CAREERAGENT_ENV
= test` in a stale shell pointed at the real file, or a temp path on a production host.
"""

from __future__ import annotations

import os
import tempfile
from enum import StrEnum
from pathlib import Path

from ..logging import get_logger

log = get_logger("storage.safety")


class Environment(StrEnum):
    PRODUCTION = "production"
    STAGING = "staging"
    DEVELOPMENT = "development"
    TEST = "test"

    @property
    def destructive_allowed(self) -> bool:
        """Only a test environment may have its tables dropped without an explicit force.

        Development is deliberately excluded. A developer's own database holds the corpus
        they have been building for weeks, which is exactly what was lost.
        """
        return self is Environment.TEST


class UnsafeDatabaseOperation(RuntimeError):
    """A destructive operation was refused. Not a bug — the guard working."""


# Names that say "this database is disposable" wherever it sits on disk.
_TEST_MARKERS = ("test", "tmp", "temp", "scratch", "pytest", "ci")


# `dev` and `prod` are the spellings the settings file has always used; the longer names
# are what this module reasons in. Both are accepted so an existing .env keeps working.
_ALIASES = {
    "dev": Environment.DEVELOPMENT,
    "prod": Environment.PRODUCTION,
    "stage": Environment.STAGING,
    "testing": Environment.TEST,
}


def current_environment() -> Environment:
    """The declared environment, defaulting to development.

    Development rather than production as the default, because every *unforced*
    destructive operation is refused in every environment except `test` — which is the
    incident path — and defaulting to production would refuse the deliberate
    `careeragent init --reset` on a fresh machine for no safety gain. A real deployment
    declares itself; that is the one case where the declaration carries weight.
    """
    raw = (os.getenv("CAREERAGENT_ENV") or "").strip().lower()
    if not raw:
        return Environment.DEVELOPMENT
    if alias := _ALIASES.get(raw):
        return alias
    try:
        return Environment(raw)
    except ValueError:
        # An unreadable declaration is treated as production: it is a sign that something
        # is misconfigured, and this is the setting where the most is refused.
        log.warning("unknown_environment", declared=raw, using="production")
        return Environment.PRODUCTION


def temp_roots() -> set[Path]:
    roots = {Path(tempfile.gettempdir())}
    for name in ("TMPDIR", "TEMP", "TMP", "RUNNER_TEMP", "PYTEST_TMPDIR"):
        if value := os.environ.get(name):
            roots.add(Path(value))
    resolved: set[Path] = set()
    for root in roots:
        try:
            resolved.add(root.resolve())
        except (OSError, ValueError):
            continue
    return resolved


def is_disposable(url: str) -> bool:
    """Is this a database it is safe to destroy?

    Answers only about the URL. A true answer here is necessary for a destructive
    operation and not sufficient — see `assert_destructive_allowed`.
    """
    lowered = (url or "").strip().lower()
    if not lowered:
        return False
    if ":memory:" in lowered or "mode=memory" in lowered:
        return True

    if lowered.startswith("postgresql"):
        # A Postgres database whose *name* says test. Host and credentials are not
        # evidence: a production database is routinely reached over localhost.
        name = lowered.rsplit("/", 1)[-1].split("?", 1)[0]
        return any(marker in name for marker in _TEST_MARKERS)

    if not lowered.startswith("sqlite"):
        return False

    # `sqlite://` with no path is in-memory in SQLAlchemy.
    if "///" not in lowered:
        return lowered.endswith("://")

    path_part = lowered.split("///", 1)[1].split("?", 1)[0]
    if not path_part:
        return False

    try:
        resolved = Path(path_part).resolve()
    except (OSError, ValueError):
        return False

    if any(resolved.is_relative_to(root) for root in temp_roots()):
        return True
    return any(marker in resolved.name for marker in _TEST_MARKERS)


def describe(url: str) -> dict[str, object]:
    """Everything the guard knows, for a health endpoint or an error message."""
    env = current_environment()
    disposable = is_disposable(url)
    return {
        "environment": env.value,
        "disposable": disposable,
        "destructive_allowed": env.destructive_allowed and disposable,
        # The scheme only. A URL can carry a password, and this is rendered in the UI.
        "scheme": (url or "").split("://", 1)[0],
    }


def assert_destructive_allowed(url: str, operation: str, *, force: bool = False) -> None:
    """Refuse a destructive operation unless this is unmistakably a throwaway database.

    `force=True` is for a caller who has already asked a human — the CLI's `init --reset`
    does. It still refuses against a declared production environment, because a prompt the
    user answered with a stale `CAREERAGENT_ENV` is not informed consent.
    """
    env = current_environment()
    disposable = is_disposable(url)
    scheme = (url or "").split("://", 1)[0] or "unknown"

    if force and env is not Environment.PRODUCTION:
        log.warning("destructive_forced", operation=operation, environment=env.value)
        return

    if env.destructive_allowed and disposable:
        return

    if force and env is Environment.PRODUCTION:
        raise UnsafeDatabaseOperation(
            f"refusing to {operation}: CAREERAGENT_ENV is 'production'. Even with force, "
            "a production database is not dropped by this code path. Set CAREERAGENT_ENV "
            "explicitly if this really is a disposable database."
        )

    reasons = []
    if not env.destructive_allowed:
        reasons.append(f"CAREERAGENT_ENV is {env.value!r} (needs 'test')")
    if not disposable:
        reasons.append(f"the database ({scheme}) is not in-memory, temporary or named test")

    raise UnsafeDatabaseOperation(
        f"refusing to {operation} — " + " and ".join(reasons) + ". "
        "A test needing a clean schema should point CAREERAGENT_DATABASE_URL at a "
        "tmp_path and set CAREERAGENT_ENV=test; a deliberate reset should use "
        "`careeragent init --reset`, which asks first."
    )
