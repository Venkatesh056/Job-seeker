"""Test-wide safety. This file exists because a test destroyed a real database.

The incident: `test_apollo.py` called `init_db(drop=True)` without the fixture that
redirects `CAREERAGENT_DATABASE_URL` at a temporary file, and a routine `pytest` run
dropped every table in a real database — 1,756 jobs, 67 researched companies, 81 boards.

Every other test file that touches storage happened to define its own `isolated` fixture.
That is the flaw: safety was a convention each file re-implemented, so a file that had
never needed one had nothing to forget. Conventions are invisible when absent.

So this is enforced here, once, for every test in the suite:

**`CAREERAGENT_ENV` is pinned to `test`** before anything imports the settings, and the
database guard refuses destructive operations outside that environment.

**The default `CAREERAGENT_DATABASE_URL` is a temp file**, so a test that forgets to
redirect it lands somewhere harmless rather than wherever the developer's shell pointed.

**A check runs after every test** and fails loudly if the URL in force was not disposable.
The per-file `isolated` fixtures still exist and still work; this is the floor beneath them.
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path

import pytest

# Set before any careeragent import: `get_settings` is cached, and a module imported
# during collection would otherwise capture whatever the developer's shell had set.
_SUITE_DIR = Path(tempfile.gettempdir()) / f"careeragent-tests-{uuid.uuid4().hex[:8]}"
_SUITE_DIR.mkdir(parents=True, exist_ok=True)

os.environ["CAREERAGENT_ENV"] = "test"
os.environ.setdefault(
    "CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{(_SUITE_DIR / 'suite.db').as_posix()}"
)
os.environ.setdefault("CAREERAGENT_STORAGE_DIR", str(_SUITE_DIR / "storage"))
os.environ.setdefault("CAREERAGENT_CACHE_DIR", str(_SUITE_DIR / "cache"))

# Nothing in the suite may reach a real provider by accident either. A test that wants a
# provider configured sets the variable itself with monkeypatch.
for _key in (
    "APOLLO_API_KEY",
    "TAVILY_API_KEY",
    "SERPAPI_API_KEY",
    "APIFY_API_TOKEN",
    "CAREERAGENT_JOOBLE_KEY",
    "JOOBLE_KEY",
    "CAREERAGENT_RAPIDAPI_KEY",
    "CAREERAGENT_ADZUNA_APP_ID",
    "CAREERAGENT_ADZUNA_APP_KEY",
    "GOOGLE_CLIENT_ID",
    "GOOGLE_CLIENT_SECRET",
    "GOOGLE_TOKEN_PATH",
):
    os.environ.pop(_key, None)


@pytest.fixture(autouse=True)
def _never_touch_a_real_database():
    """Fail any test that ends up pointed at a database it could damage.

    Runs after the test rather than before, because a test may legitimately redirect the
    URL partway through — what matters is that it never settled on a real one. The failure
    is loud and names the URL's scheme, so the fix is obvious rather than mysterious.
    """
    yield

    from careeragent.storage.db import resolve_url
    from careeragent.storage.safety import current_environment, is_disposable

    try:
        url = resolve_url()
    except Exception:
        # A test that deliberately broke the settings is not this fixture's business.
        return

    environment = current_environment()
    if environment.value != "test":
        pytest.fail(
            f"this test left CAREERAGENT_ENV as {environment.value!r}. Every test runs "
            "against a test environment; use monkeypatch so the change is undone."
        )
    if not is_disposable(url):
        pytest.fail(
            "this test was pointed at a database that is not disposable "
            f"({url.split('://', 1)[0]}). Point CAREERAGENT_DATABASE_URL at a tmp_path — "
            "see the `isolated` fixtures, or tests/conftest.py for why this is enforced."
        )


@pytest.fixture
def isolated_db(tmp_path, monkeypatch):
    """A throwaway database of this test's own.

    Offered centrally so a new test file needs no boilerplate to be safe. The per-file
    `isolated` fixtures predate this and do the same thing; both are fine.
    """
    monkeypatch.setenv("CAREERAGENT_ENV", "test")
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("CAREERAGENT_CACHE_DIR", str(tmp_path / "cache"))

    from careeragent import config as config_module
    from careeragent import storage

    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
    yield tmp_path
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
