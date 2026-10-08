"""Which API keys are configured, whether they still work, and how much is left.

A free tier that runs out mid-campaign fails in the most confusing way available: the run
finishes, reports fewer companies, and nothing says why. This makes the quota visible before
it bites, so a key can be swapped while there is still time.

**Two kinds of number, never mixed.** Some providers report their own remaining quota, and
that is authoritative — SerpAPI says "249 of 250 searches left", Tavily says "0 of 1000",
Apify reports credits against a billing cycle. Others report nothing, so the only honest
figure is our own call count, which is a floor rather than a total: it does not know about
calls made from anywhere else. Every number carries `source: provider | local` so the two
are never read as the same thing.

**Keys are never returned.** Only a masked fingerprint — last four characters — which is
enough to tell two keys apart when swapping one, and useless to anyone reading the response.

**Probing costs quota.** Checking SerpAPI's balance is itself free, but a provider without a
balance endpoint can only be checked by making a real call. Those are marked `costs_a_call`
and are skipped unless explicitly asked for, because a status page that silently spends the
budget it is reporting on would be absurd.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import httpx

from .logging import get_logger

log = get_logger("integrations")

PROBE_TIMEOUT = 15.0


def _key(*names: str) -> str:
    """First non-empty environment value among these names."""
    for name in names:
        if value := os.getenv(name, "").strip():
            return value
    return ""


def fingerprint(value: str) -> str:
    """A key's last four characters, enough to tell two apart. Never the key."""
    clean = (value or "").strip()
    if not clean:
        return ""
    return f"…{clean[-4:]}" if len(clean) > 4 else "…"


@dataclass
class Quota:
    used: int | float | None = None
    limit: int | float | None = None
    remaining: int | float | None = None
    unit: str = "calls"
    resets_at: str | None = None
    # "provider" — the service told us. "local" — our own count, which is a floor.
    source: str = "local"
    note: str = ""

    @property
    def fraction_used(self) -> float | None:
        if self.limit in (None, 0) or self.used is None:
            return None
        try:
            return max(0.0, min(1.0, float(self.used) / float(self.limit)))
        except (TypeError, ZeroDivisionError):
            return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "used": self.used,
            "limit": self.limit,
            "remaining": self.remaining,
            "unit": self.unit,
            "resets_at": self.resets_at,
            "source": self.source,
            "fraction_used": self.fraction_used,
            "note": self.note,
        }


@dataclass
class Integration:
    """One external service, and what stops working without it."""

    id: str
    label: str
    env_vars: tuple[str, ...]
    powers: str
    signup: str = ""
    optional: bool = True
    # True when the only way to check it is to spend a call on it.
    costs_a_call: bool = False

    @property
    def configured(self) -> bool:
        return bool(_key(*self.env_vars))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "label": self.label,
            "env_vars": list(self.env_vars),
            "powers": self.powers,
            "signup": self.signup,
            "optional": self.optional,
            "configured": self.configured,
            "key": fingerprint(_key(*self.env_vars)),
            "costs_a_call": self.costs_a_call,
        }


# ─── The registry ───────────────────────────────────────────────────────────

INTEGRATIONS: list[Integration] = [
    Integration(
        "adzuna",
        "Adzuna",
        ("CAREERAGENT_ADZUNA_APP_ID", "CAREERAGENT_ADZUNA_APP_KEY"),
        "Regional job discovery — the best India coverage of any free source.",
        "https://developer.adzuna.com/signup",
        costs_a_call=True,
    ),
    Integration(
        "jsearch",
        "JSearch (RapidAPI)",
        ("CAREERAGENT_RAPIDAPI_KEY",),
        "Google Jobs index — reaches LinkedIn, Indeed and Naukri postings without scraping.",
        "https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch",
        costs_a_call=True,
    ),
    Integration(
        "jooble",
        "Jooble",
        ("CAREERAGENT_JOOBLE_KEY", "JOOBLE_KEY"),
        "Job aggregation with no published monthly cap — the breadth source.",
        "https://jooble.org/api/about",
        costs_a_call=True,
    ),
    Integration(
        "tavily",
        "Tavily",
        ("TAVILY_API_KEY",),
        "Web search for company discovery — finding employers that are not on a job board.",
        "https://tavily.com",
    ),
    Integration(
        "serpapi",
        "SerpAPI",
        ("SERPAPI_API_KEY",),
        "Google results for company and careers-page discovery.",
        "https://serpapi.com",
    ),
    Integration(
        "apify",
        "Apify",
        ("APIFY_API_TOKEN",),
        "Crawling careers pages that have no structured feed.",
        "https://apify.com",
    ),
    Integration(
        "apollo",
        "Apollo",
        ("APOLLO_API_KEY",),
        "Company facts and job postings. Not contacts — the free plan refuses people "
        "search, and the enrichment it does allow is what company fit was missing.",
        "https://apollo.io",
    ),
    Integration(
        "google",
        "Google / Gmail",
        ("GOOGLE_CLIENT_ID",),
        "Sending approved outreach from your own mailbox.",
        "https://console.cloud.google.com",
    ),
    Integration(
        "gemini",
        "Gemini",
        ("GEMINI_API_KEY",),
        "Resume tailoring, answers, letters, and the supervisor's reasoning.",
        "https://aistudio.google.com/apikey",
        optional=False,
    ),
    Integration(
        "groq",
        "Groq",
        ("GROQ_API_KEY",),
        "Fast, cheap extraction and scoring. The fallback when Gemini rate-limits.",
        "https://console.groq.com/keys",
    ),
    Integration(
        "openai",
        "OpenAI",
        ("OPENAI_API_KEY",),
        "Alternative model provider.",
        "https://platform.openai.com/api-keys",
    ),
    Integration(
        "anthropic",
        "Anthropic",
        ("ANTHROPIC_API_KEY",),
        "Alternative model provider.",
        "https://console.anthropic.com",
    ),
    Integration(
        "xai",
        "xAI (Grok)",
        ("XAI_API_KEY",),
        "Alternative model provider.",
        "https://console.x.ai",
    ),
]

BY_ID = {i.id: i for i in INTEGRATIONS}


# ─── Provider-reported quota ────────────────────────────────────────────────
#
# One probe per provider that publishes a balance. Each returns (status, Quota) and never
# raises — a status page that falls over because one provider is down is not a status page.


async def _probe_serpapi(client: httpx.AsyncClient, key: str) -> tuple[str, Quota]:
    r = await client.get("https://serpapi.com/account", params={"api_key": key})
    if r.status_code != 200:
        return ("invalid" if r.status_code in (401, 403) else "unreachable"), Quota()
    b = r.json()
    limit = b.get("searches_per_month")
    used = b.get("this_month_usage")
    left = b.get("total_searches_left")
    return "ok", Quota(
        used=used,
        limit=limit,
        remaining=left,
        unit="searches",
        source="provider",
        note=str(b.get("plan_name") or ""),
    )


async def _probe_tavily(client: httpx.AsyncClient, key: str) -> tuple[str, Quota]:
    r = await client.get("https://api.tavily.com/usage", headers={"Authorization": f"Bearer {key}"})
    if r.status_code != 200:
        return ("invalid" if r.status_code in (401, 403) else "unreachable"), Quota()
    account = r.json().get("account", {}) or {}
    used = account.get("plan_usage")
    limit = account.get("plan_limit")
    remaining = (
        (limit - used) if isinstance(limit, int | float) and isinstance(used, int | float) else None
    )
    return "ok", Quota(
        used=used,
        limit=limit,
        remaining=remaining,
        unit="credits",
        source="provider",
        note=str(account.get("current_plan") or ""),
    )


async def _probe_apify(client: httpx.AsyncClient, key: str) -> tuple[str, Quota]:
    r = await client.get("https://api.apify.com/v2/users/me", params={"token": key})
    if r.status_code != 200:
        return ("invalid" if r.status_code in (401, 403) else "unreachable"), Quota()
    plan = (r.json().get("data", {}) or {}).get("plan", {}) or {}
    limit = plan.get("monthlyUsageCreditsUsd")

    used: float | None = None
    resets: str | None = None
    usage = await client.get(
        "https://api.apify.com/v2/users/me/usage/monthly", params={"token": key}
    )
    if usage.status_code == 200:
        data = usage.json().get("data", {}) or {}
        used = data.get("totalUsageCreditsUsdAfterVolumeDiscount")
        resets = (data.get("usageCycle") or {}).get("endAt")

    remaining = (
        (limit - used) if isinstance(limit, int | float) and isinstance(used, int | float) else None
    )
    return "ok", Quota(
        used=round(used, 4) if isinstance(used, int | float) else None,
        limit=limit,
        remaining=round(remaining, 4) if isinstance(remaining, int | float) else None,
        unit="USD credits",
        resets_at=resets,
        source="provider",
        note=str(plan.get("id") or ""),
    )


async def _probe_apollo(client: httpx.AsyncClient, key: str) -> tuple[str, Quota]:
    """Health, then one real call — because health lies about the thing that matters.

    `auth/health` returns `{"healthy": true}` for a key whose billing-cycle credits are
    completely spent, so a status page built on it would report "working" for an account
    that cannot answer a single question. The cheapest honest test is an actual enrichment,
    which also returns Apollo's published rate limits in its headers.
    """
    headers = {"x-api-key": key, "content-type": "application/json"}
    r = await client.get("https://api.apollo.io/api/v1/auth/health", headers=headers)
    if r.status_code != 200:
        return ("invalid" if r.status_code in (401, 403) else "unreachable"), Quota()
    if not bool(r.json().get("healthy")):
        return "invalid", Quota()

    probe = await client.post(
        "https://api.apollo.io/api/v1/organizations/enrich",
        headers=headers,
        json={"domain": "apollo.io"},
    )
    try:
        body = probe.json()
    except ValueError:
        body = {}

    from .sources.apollo import _exhausted, _plan_gated, rate_limits

    limits = rate_limits(probe)
    note_parts = []
    if limits.get("per_day"):
        note_parts.append(f"{limits['per_day']}/day")
    if limits.get("per_hour"):
        note_parts.append(f"{limits['per_hour']}/hour")
    note = "rate limit " + ", ".join(note_parts) if note_parts else ""

    if _exhausted(body):
        return "exhausted", Quota(
            source="provider",
            note=(note + " · " if note else "")
            + "credits spent for this billing cycle; resets with the cycle",
        )
    if _plan_gated(body):
        return "ok", Quota(source="local", note="enrichment is not included in your plan")
    if probe.status_code >= 400:
        return "unreachable", Quota(source="local", note=f"HTTP {probe.status_code}")

    return "ok", Quota(
        used=limits.get("day_used"),
        limit=limits.get("per_day"),
        remaining=(
            limits["per_day"] - limits["day_used"]
            if limits.get("per_day") is not None and limits.get("day_used") is not None
            else None
        ),
        unit="requests",
        source="provider" if limits else "local",
        note=note or "Apollo publishes no usage endpoint on this plan",
    )


async def _probe_xai(client: httpx.AsyncClient, key: str) -> tuple[str, Quota]:
    r = await client.get("https://api.x.ai/v1/models", headers={"Authorization": f"Bearer {key}"})
    if r.status_code == 200:
        return "ok", Quota(source="local")
    if r.status_code == 403 and "credit" in r.text.lower():
        return "exhausted", Quota(source="provider", note="no credits on this team")
    return ("invalid" if r.status_code in (401, 403) else "unreachable"), Quota()


async def _probe_google(_client: httpx.AsyncClient, _key: str) -> tuple[str, Quota]:
    """Config only. A real check needs the interactive consent, which is not a status probe."""
    from .storage.db import resolve_url  # noqa: F401  (kept local; token store lands here)

    token = os.getenv("GOOGLE_TOKEN_PATH", "data/google_token.json")
    from pathlib import Path

    authorised = Path(token).exists()
    return ("ok" if authorised else "needs_consent"), Quota(
        source="local",
        note="authorised" if authorised else "client configured; needs your one-time consent",
    )


PROBES = {
    "serpapi": _probe_serpapi,
    "tavily": _probe_tavily,
    "apify": _probe_apify,
    "apollo": _probe_apollo,
    "xai": _probe_xai,
    "google": _probe_google,
}


# ─── Local usage ────────────────────────────────────────────────────────────


async def _local_usage(session: Any, provider: str) -> Quota:
    """Our own call count this month, for providers that publish nothing.

    A floor, not a total: it counts what this installation did and knows nothing about
    calls made from anywhere else with the same key. Labelled `local` so it is never read
    as an authoritative balance.
    """
    from sqlalchemy import func, select

    from .storage.models import ProviderCall

    month_start = date.today().replace(day=1)
    total = (
        await session.execute(
            select(func.coalesce(func.sum(ProviderCall.calls), 0)).where(
                ProviderCall.provider == provider, ProviderCall.day >= month_start
            )
        )
    ).scalar_one()
    return Quota(used=int(total or 0), source="local", note="counted by this installation")


async def record_call(session: Any, provider: str, calls: int = 1, errors: int = 0) -> None:
    """Count one provider call. Never raises — accounting must not break the work."""
    from sqlalchemy import select

    from .storage.models import ProviderCall

    try:
        today = date.today()
        row = (
            (
                await session.execute(
                    select(ProviderCall).where(
                        ProviderCall.provider == provider, ProviderCall.day == today
                    )
                )
            )
            .scalars()
            .first()
        )
        if row is None:
            row = ProviderCall(provider=provider, day=today, calls=0, errors=0)
            session.add(row)
        row.calls += max(0, calls)
        row.errors += max(0, errors)
        await session.flush()
    except Exception as exc:
        log.debug("provider_call_not_recorded", provider=provider, error=f"{type(exc).__name__}")


# ─── The status page's data ─────────────────────────────────────────────────


async def status(session: Any, *, probe: bool = True) -> dict[str, Any]:
    """Every integration: configured, reachable, and how much is left.

    `probe=False` answers from configuration and local counts alone, making no network
    calls at all — the right choice for a page that loads often.
    """
    out: list[dict[str, Any]] = []

    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT, follow_redirects=True) as client:
        for integration in INTEGRATIONS:
            entry = integration.to_dict()
            entry["status"] = "no_key" if not integration.configured else "unknown"
            entry["quota"] = (await _local_usage(session, integration.id)).to_dict()
            entry["checked_at"] = None

            if integration.configured and probe and integration.id in PROBES:
                try:
                    state, quota = await PROBES[integration.id](client, _key(*integration.env_vars))
                    entry["status"] = state
                    if quota.source == "provider" or quota.note:
                        entry["quota"] = quota.to_dict()
                    entry["checked_at"] = datetime.now(UTC).isoformat()
                except Exception as exc:
                    entry["status"] = "unreachable"
                    entry["error"] = f"{type(exc).__name__}"[:60]
                    log.debug("probe_failed", provider=integration.id, error=str(exc)[:100])
            elif integration.configured:
                # Configured but only checkable by spending a call, so it is not checked.
                entry["status"] = "configured"

            out.append(entry)

    configured = [e for e in out if e["configured"]]
    problems = [
        e for e in out if e["configured"] and e["status"] in {"invalid", "exhausted", "unreachable"}
    ]
    low = [
        e
        for e in out
        if (e["quota"].get("fraction_used") or 0) >= 0.8 and e["quota"].get("source") == "provider"
    ]

    return {
        "integrations": out,
        "configured": len(configured),
        "total": len(out),
        "problems": [e["id"] for e in problems],
        "running_low": [e["id"] for e in low],
        "note": (
            "Quota marked 'provider' comes from the service. Quota marked 'local' is this "
            "installation's own call count and is a floor, not a balance."
        ),
    }
