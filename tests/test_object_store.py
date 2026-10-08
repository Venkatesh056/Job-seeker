"""The object store — where resumes, renders and submission screenshots live.

Two things are worth testing about a file store, and "can it write a file" is not really one
of them.

**It must not be escapable.** Keys are built by this codebase today, but a store that
accepts `../../etc/passwd` is one refactor away from accepting one built out of a job title.

**A half-written file must never be readable.** A screenshot is the *evidence* that an
application was submitted. Evidence that is sometimes a truncated PNG is not evidence.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from careeragent.storage.objects import LocalBackend, ObjectStore, UnsafeKey


@pytest.fixture
def store(tmp_path):
    return ObjectStore(LocalBackend(tmp_path / "objects"))


async def test_a_round_trip_returns_the_same_bytes(store):
    stored = await store.put("renders/abc.pdf", b"%PDF-1.4 ...", "application/pdf")
    assert stored.size_bytes == 12
    assert await store.get("renders/abc.pdf") == b"%PDF-1.4 ..."
    assert await store.exists("renders/abc.pdf")


@pytest.mark.parametrize(
    "key",
    [
        "../secrets.env",
        "renders/../../etc/passwd",
        "/etc/passwd",
        "renders/",
        "",
        "renders/a\\b.pdf",
        "renders/a b.pdf",
        "..",
    ],
    ids=lambda k: (k or "empty")[:24],
)
async def test_a_key_that_would_escape_the_store_is_refused(store, key):
    """Refused, not sanitised. Normalising a hostile path quietly produces a valid one, and
    the caller never learns it asked for something it should not have."""
    with pytest.raises(UnsafeKey):
        await store.put(key, b"x")


async def test_a_nested_key_creates_its_directories(store):
    await store.put("applications/run-1/step-3/screenshot.png", b"\x89PNG")
    assert await store.exists("applications/run-1/step-3/screenshot.png")


async def test_nothing_partial_is_ever_visible(tmp_path):
    """Written beside, then renamed. A reader sees the old bytes or the new ones, never
    half of a PDF — and a crash leaves a temp file rather than a truncated artifact."""
    backend = LocalBackend(tmp_path / "objects")
    store = ObjectStore(backend)

    await store.put("renders/v1.pdf", b"first version")
    await store.put("renders/v1.pdf", b"second version, longer")

    assert await store.get("renders/v1.pdf") == b"second version, longer"
    # The listing must not show the staging file, or a prune would count it as an object
    # and a download endpoint could be handed its key.
    assert [o.key for o in await store.list()] == ["renders/v1.pdf"]
    assert not list((tmp_path / "objects").rglob("*.partial"))


async def test_listing_is_scoped_to_a_prefix(store):
    await store.put("renders/a.pdf", b"a")
    await store.put("renders/b.pdf", b"b")
    await store.put("applications/c.png", b"c")

    assert {o.key for o in await store.list("renders")} == {"renders/a.pdf", "renders/b.pdf"}
    assert len(await store.list()) == 3


async def test_deleting_something_that_is_not_there_is_false_not_an_error(store):
    assert await store.delete("renders/never-existed.pdf") is False


# ─── Pruning ────────────────────────────────────────────────────────────────


async def _age(store, key: str, days: int) -> None:
    import os

    path = (await store.list())[0].local_path
    for stored in await store.list():
        if stored.key == key and stored.local_path:
            path = stored.local_path
    when = (datetime.now(UTC) - timedelta(days=days)).timestamp()
    os.utime(path, (when, when))


async def test_pruning_removes_old_objects(store):
    await store.put("applications/old.png", b"old")
    await store.put("applications/new.png", b"new")
    await _age(store, "applications/old.png", days=90)

    removed = await store.prune("applications", older_than_days=30)

    assert removed == 1
    assert {o.key for o in await store.list()} == {"applications/new.png"}


async def test_pruning_keeps_the_newest_regardless_of_age(store):
    """ "Delete everything older than 30 days" on a system nobody has used for 31 days
    deletes the only evidence it has."""
    for name in ("a.png", "b.png", "c.png"):
        await store.put(f"applications/{name}", b"x")
        await _age(store, f"applications/{name}", days=200)

    removed = await store.prune("applications", older_than_days=30, keep_at_least=2)

    assert removed == 1
    assert len(await store.list("applications")) == 2


async def test_usage_reports_what_is_there_and_which_backend(store):
    await store.put("renders/a.pdf", b"0123456789")
    usage = await store.usage()
    assert usage["objects"] == 1
    assert usage["bytes"] == 10
    assert "local" in str(usage["backend"])


# ─── Which backend, and saying so ───────────────────────────────────────────


def test_the_store_is_local_unless_supabase_is_fully_configured(monkeypatch, tmp_path):
    """Both the URL and the key, or neither. A URL with no key produces 401s at the moment
    somebody saves a resume, which is the worst time to discover a half-configuration."""
    from careeragent import config as config_module
    from careeragent.storage import objects

    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("CAREERAGENT_SUPABASE_URL", "https://example.supabase.co")
    monkeypatch.delenv("CAREERAGENT_SUPABASE_SERVICE_KEY", raising=False)
    config_module.get_settings.cache_clear()
    objects.reset_store()
    try:
        assert "local" in objects.get_store().describe()
    finally:
        objects.reset_store()
        config_module.get_settings.cache_clear()


def test_the_supabase_backend_admits_it_has_never_been_exercised():
    """There is no Supabase project attached to this deployment, so this code has never made
    a real request. Saying so in `describe()` keeps anything downstream from reporting it as
    working on the strength of it existing."""
    from careeragent.storage.objects import SupabaseBackend

    backend = SupabaseBackend("https://example.supabase.co", "service-key")
    assert "never exercised" in backend.describe()


def test_tracing_is_off_without_a_key_and_says_so(monkeypatch):
    """A tracing integration that silently does nothing is worse than none — it makes people
    stop looking for the reason their traces are missing."""
    from careeragent import config as config_module
    from careeragent.net import tracing

    monkeypatch.delenv("CAREERAGENT_LANGSMITH_API_KEY", raising=False)
    config_module.get_settings.cache_clear()
    tracing.reset()
    try:
        described = tracing.describe()
        assert described["enabled"] is False
        assert described["backend"] == "none"
        # And the thing that *is* true stays true: the audit trail does not depend on any
        # third party being reachable.
        assert "agent_decision" in described["durable_record"]
        assert tracing.configure() is False
    finally:
        tracing.reset()
        config_module.get_settings.cache_clear()


# ─── The upload endpoint, which takes a filename from a stranger ────────────


@pytest.fixture
def uploads(tmp_path, monkeypatch):
    """A throwaway database and storage directory for the upload endpoint."""
    monkeypatch.setenv("CAREERAGENT_ENV", "test")
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'u.db'}")
    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("CAREERAGENT_CACHE_DIR", str(tmp_path / "cache"))

    from careeragent import config as config_module
    from careeragent import storage
    from careeragent.storage import objects

    config_module.get_settings.cache_clear()
    objects.reset_store()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
    yield tmp_path
    config_module.get_settings.cache_clear()
    objects.reset_store()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "filename",
    ["../../../pwned.pdf", "../../.env.pdf", "..\..\..\pwned.pdf", "/etc/cron.d/evil.pdf"],
    ids=["up-three", "dot-env", "backslashes", "absolute"],
)
async def test_an_uploaded_filename_cannot_write_outside_the_store(uploads, filename):
    """The measured hole: `saved = target / (file.filename or "resume")`.

    `file.filename` is chosen by whoever is uploading, Starlette does not sanitise it, and
    the suffix allowlist does not help — `../../../pwned.pdf` ends in `.pdf` and passed
    every check the endpoint made. It wrote to `D:\Joby agent\pwned.pdf`.

    The fix is not a sanitiser. The stored key is generated and the client's name is never
    part of a path, so there is nothing left to get right.
    """
    from fastapi.testclient import TestClient

    from careeragent.api import app
    from careeragent.storage import init_db

    await init_db(drop=True)
    before = set(uploads.parent.rglob("*pwned*")) | set(uploads.parent.rglob("*evil*"))

    with TestClient(app) as client:
        # A real PDF is not needed: the parse failing is fine, the *write* is what matters,
        # and it happens before parsing.
        client.post(
            "/api/v1/resume/upload",
            files={"file": (filename, b"%PDF-1.4 not really", "application/pdf")},
        )

    after = set(uploads.parent.rglob("*pwned*")) | set(uploads.parent.rglob("*evil*"))
    assert after == before, f"upload escaped the store: {sorted(after - before)}"


async def test_an_uploaded_file_lands_under_the_store_with_a_generated_name(uploads):
    from fastapi.testclient import TestClient

    from careeragent.api import app
    from careeragent.storage import init_db

    await init_db(drop=True)
    with TestClient(app) as client:
        client.post(
            "/api/v1/resume/upload",
            files={"file": ("my cv (final) v2.pdf", b"%PDF-1.4 not really", "application/pdf")},
        )

    written = list((uploads / "storage" / "resumes").glob("*.pdf"))
    assert len(written) == 1, "the upload was not stored under the object store"
    # Spaces and brackets came from the client; the stored name is a generated hex key.
    assert written[0].stem.isalnum() and " " not in written[0].name
