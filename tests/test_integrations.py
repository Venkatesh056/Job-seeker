"""The API-key status page.

Two things matter more than the rest here. A key must never leave the process — not in a
response, not in a log, not in an error string. And a number shown as "remaining" must be
one a provider actually said, because a made-up balance is worse than no balance at all:
it is the difference between "you have 249 searches left" and "we counted 249 of our own
calls and have no idea what else used this key."
"""

from __future__ import annotations

from datetime import date

import httpx
import pytest

from careeragent import integrations

pytestmark = pytest.mark.anyio


@pytest.fixture
def anyio_backend():
    return "asyncio"


# ─── Keys never leave ───────────────────────────────────────────────────────


def test_a_fingerprint_is_four_characters_and_never_the_key():
    secret = "sk-live-abcdefghijklmnop1234"
    printed = integrations.fingerprint(secret)

    assert printed == "…1234"
    assert secret not in printed
    assert len(printed) == 5


def test_a_short_key_does_not_leak_by_being_short():
    # A 4-character key fingerprinted naively would be the whole key.
    assert integrations.fingerprint("abcd") == "…"
    assert integrations.fingerprint("ab") == "…"


def test_an_absent_key_fingerprints_to_nothing():
    assert integrations.fingerprint("") == ""
    assert integrations.fingerprint("   ") == ""


async def test_no_key_appears_anywhere_in_the_status_response(isolated, monkeypatch):
    """The whole point of the page. Asserted against the serialised response, not a field."""
    import json

    from careeragent.storage import init_db, session_scope

    secrets = {
        "SERPAPI_API_KEY": "serp-SECRETVALUE-0001",
        "TAVILY_API_KEY": "tvly-SECRETVALUE-0002",
        "CAREERAGENT_JOOBLE_KEY": "jooble-SECRETVALUE-0003",
        "APOLLO_API_KEY": "apollo-SECRETVALUE-0004",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)

    await init_db(drop=True)
    async with session_scope() as session:
        # ensure_ascii=False so the ellipsis in a fingerprint stays comparable.
        body = json.dumps(await integrations.status(session, probe=False), ensure_ascii=False)

    for value in secrets.values():
        assert value not in body
        assert value[:-4] not in body  # not even the non-fingerprint half
    assert "…0001" in body  # the fingerprint is there, so a key can still be told apart


# ─── Probing costs something, so it is opt-in ───────────────────────────────


async def test_loading_the_page_makes_no_network_calls(isolated, monkeypatch):
    """A status page that spends the budget it reports on would be absurd."""
    from careeragent.storage import init_db, session_scope

    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key-9999")

    def explode(*args, **kwargs):
        raise AssertionError("probe=False must not touch the network")

    monkeypatch.setattr(httpx.AsyncClient, "get", explode)
    monkeypatch.setattr(httpx.AsyncClient, "post", explode)

    await init_db(drop=True)
    async with session_scope() as session:
        result = await integrations.status(session, probe=False)

    serpapi = next(e for e in result["integrations"] if e["id"] == "serpapi")
    assert serpapi["status"] == "configured"
    assert serpapi["checked_at"] is None


def test_a_provider_that_can_only_be_checked_by_spending_a_call_is_never_probed():
    """`costs_a_call` and a probe function are mutually exclusive by construction."""
    for integration in integrations.INTEGRATIONS:
        if integration.costs_a_call:
            assert integration.id not in integrations.PROBES, (
                f"{integration.id} would be probed automatically, but checking it costs "
                "a call against the very quota being reported"
            )


async def test_a_provider_that_is_down_is_reported_as_unreachable_not_as_a_crash(
    isolated, monkeypatch
):
    from careeragent.storage import init_db, session_scope

    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key-9999")

    async def refuse(client, key):
        raise httpx.ConnectTimeout("nope")

    monkeypatch.setitem(integrations.PROBES, "serpapi", refuse)

    await init_db(drop=True)
    async with session_scope() as session:
        result = await integrations.status(session, probe=True)

    serpapi = next(e for e in result["integrations"] if e["id"] == "serpapi")
    assert serpapi["status"] == "unreachable"
    assert "serpapi" in result["problems"]
    assert "serp-key-9999" not in str(serpapi)


# ─── A local count is a floor, not a balance ────────────────────────────────


async def test_our_own_call_count_is_labelled_local(isolated):
    from careeragent.storage import init_db, session_scope

    await init_db(drop=True)
    async with session_scope() as session:
        await integrations.record_call(session, "jooble", calls=3)
    async with session_scope() as session:
        result = await integrations.status(session, probe=False)

    jooble = next(e for e in result["integrations"] if e["id"] == "jooble")
    assert jooble["quota"]["used"] == 3
    assert jooble["quota"]["source"] == "local"
    assert jooble["quota"]["limit"] is None
    assert jooble["quota"]["remaining"] is None


async def test_a_local_count_never_triggers_a_running_low_warning(isolated, monkeypatch):
    """We cannot know a key is nearly spent from our own count alone — other machines,
    other scripts and the provider's own dashboard all use the same key."""
    from careeragent.storage import init_db, session_scope

    monkeypatch.setenv("CAREERAGENT_JOOBLE_KEY", "jooble-key-1234")

    await init_db(drop=True)
    async with session_scope() as session:
        await integrations.record_call(session, "jooble", calls=100_000)
    async with session_scope() as session:
        result = await integrations.status(session, probe=False)

    assert result["running_low"] == []


async def test_a_provider_reported_balance_does_trigger_it(isolated, monkeypatch):
    from careeragent.storage import init_db, session_scope

    monkeypatch.setenv("SERPAPI_API_KEY", "serp-key-9999")

    async def nearly_out(client, key):
        return "ok", integrations.Quota(
            used=245, limit=250, remaining=5, unit="searches", source="provider"
        )

    monkeypatch.setitem(integrations.PROBES, "serpapi", nearly_out)

    await init_db(drop=True)
    async with session_scope() as session:
        result = await integrations.status(session, probe=True)

    assert result["running_low"] == ["serpapi"]
    serpapi = next(e for e in result["integrations"] if e["id"] == "serpapi")
    assert serpapi["quota"]["remaining"] == 5
    assert serpapi["quota"]["source"] == "provider"


async def test_counting_calls_accumulates_within_a_day(isolated):
    from careeragent.storage import init_db, session_scope
    from careeragent.storage.models import ProviderCall

    await init_db(drop=True)
    for _ in range(5):
        async with session_scope() as session:
            await integrations.record_call(session, "tavily")

    async with session_scope() as session:
        from sqlalchemy import select

        rows = (
            (await session.execute(select(ProviderCall).where(ProviderCall.provider == "tavily")))
            .scalars()
            .all()
        )

    assert len(rows) == 1, "one row per provider per day, not one per call"
    assert rows[0].calls == 5
    assert rows[0].day == date.today()


async def test_accounting_failure_never_breaks_the_work_it_is_counting(isolated):
    """Counting a call is bookkeeping. It must not be able to fail a search."""

    class Broken:
        async def execute(self, *a, **k):
            raise RuntimeError("database gone")

        def add(self, *a, **k):
            raise RuntimeError("database gone")

        async def flush(self):
            raise RuntimeError("database gone")

    await integrations.record_call(Broken(), "serpapi")  # must not raise


# ─── The quota arithmetic ───────────────────────────────────────────────────


def test_a_fraction_needs_both_a_count_and_a_limit():
    assert integrations.Quota(used=5, limit=10).fraction_used == 0.5
    assert integrations.Quota(used=5).fraction_used is None
    assert integrations.Quota(limit=10).fraction_used is None
    assert integrations.Quota(used=5, limit=0).fraction_used is None


def test_a_fraction_is_clamped_because_providers_overshoot():
    # Overage plans really do report used > limit.
    assert integrations.Quota(used=300, limit=250).fraction_used == 1.0
    assert integrations.Quota(used=-1, limit=250).fraction_used == 0.0


# ─── The registry itself ────────────────────────────────────────────────────


def test_every_integration_is_uniquely_addressable():
    ids = [i.id for i in integrations.INTEGRATIONS]
    assert len(ids) == len(set(ids))
    assert set(integrations.BY_ID) == set(ids)


def test_every_integration_says_what_breaks_without_it_and_where_to_get_one():
    for integration in integrations.INTEGRATIONS:
        assert integration.powers, f"{integration.id} does not say what it powers"
        assert integration.signup.startswith("https://"), f"{integration.id} has no signup link"
        assert integration.env_vars, f"{integration.id} names no environment variable"


def test_every_probe_belongs_to_a_real_integration():
    assert set(integrations.PROBES) <= set(integrations.BY_ID)


def test_a_key_is_read_from_the_first_variable_that_has_one(monkeypatch):
    monkeypatch.delenv("CAREERAGENT_JOOBLE_KEY", raising=False)
    monkeypatch.setenv("JOOBLE_KEY", "fallback-0001")
    jooble = integrations.BY_ID["jooble"]
    assert jooble.configured
    assert jooble.to_dict()["key"] == "…0001"

    monkeypatch.setenv("CAREERAGENT_JOOBLE_KEY", "primary-0002")
    assert jooble.to_dict()["key"] == "…0002"


def test_whitespace_only_configuration_counts_as_absent(monkeypatch):
    """A key left as an empty value in .env must read as missing, not as configured."""
    monkeypatch.setenv("CAREERAGENT_JOOBLE_KEY", "   ")
    monkeypatch.delenv("JOOBLE_KEY", raising=False)
    assert not integrations.BY_ID["jooble"].configured


# ─── Fixtures ───────────────────────────────────────────────────────────────


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'i.db'}")
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
