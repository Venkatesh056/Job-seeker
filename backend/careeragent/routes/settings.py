"""Settings endpoints: autonomy, weights, and LLM configuration.

**No endpoint here returns an API key, and no endpoint accepts one.** Keys resolve
from the environment or the OS keyring (`config.get_api_key`), and the API reports
only whether each provider is *reachable*. A key that can be read back over HTTP is
a key that leaks through a screenshot, a log, or a browser cache.

Autonomy is settable at runtime for the current process only. Making it persistent
would mean a value in the database quietly overriding the one in the environment,
and for a setting that governs whether the agent submits applications unattended,
that ambiguity is not worth the convenience.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from ..config import get_api_key, get_settings
from ..logging import get_logger
from ..models.scoring import PriorityThresholds, ScoreWeights
from ..storage import repository as repo
from ..storage import session_scope

log = get_logger("routes.settings")

router = APIRouter(prefix="/api/v1/settings", tags=["settings"])

AUTONOMY_LEVELS = {
    0: "Search only",
    1: "Search, score and rank",
    2: "Tailor resumes and draft answers",
    3: "Open a browser and fill forms; never submits",
    4: "Queue for approval; submits when you approve (default)",
    5: "Auto-submit above the threshold, on allow-listed ATS platforms only",
}


class AutonomyRequest(BaseModel):
    level: int = Field(ge=0, le=5)
    auto_submit_threshold: float | None = Field(default=None, ge=0.5, le=1.0)
    daily_application_cap: int | None = Field(default=None, ge=1, le=100)


class WeightsRequest(BaseModel):
    weights: dict[str, float]
    rationale: str = ""


@router.get("/autonomy")
async def autonomy() -> dict[str, Any]:
    settings = get_settings()
    return {
        "level": settings.autonomy_level,
        "description": AUTONOMY_LEVELS[settings.autonomy_level],
        "levels": AUTONOMY_LEVELS,
        "auto_submit_threshold": settings.auto_submit_threshold,
        "daily_application_cap": settings.daily_application_cap,
        "portal_adapters_enabled": settings.enable_portal_adapters,
        "hard_stops": [
            "CAPTCHA",
            "MFA / one-time codes",
            "login or account creation",
            "identity or document verification",
            "demographic questions",
            "work authorisation and sponsorship declarations",
        ],
        "note": (
            "The hard stops are not configurable at any level. Level 5 additionally "
            "requires an allow-listed ATS, an empty pre-submit verdict, and no "
            "unanswered required field."
        ),
    }


@router.put("/autonomy")
async def set_autonomy(body: AutonomyRequest) -> dict[str, Any]:
    """Change autonomy for this process.

    Deliberately not persisted: `CAREERAGENT_AUTONOMY_LEVEL` in the environment is
    the durable setting, and a database value silently overriding it would make "why
    did it submit that?" unanswerable.
    """
    settings = get_settings()
    settings.autonomy_level = body.level
    if body.auto_submit_threshold is not None:
        settings.auto_submit_threshold = body.auto_submit_threshold
    if body.daily_application_cap is not None:
        settings.daily_application_cap = body.daily_application_cap

    log.info(
        "autonomy_changed",
        level=body.level,
        threshold=settings.auto_submit_threshold,
        cap=settings.daily_application_cap,
    )
    return {
        "level": settings.autonomy_level,
        "description": AUTONOMY_LEVELS[settings.autonomy_level],
        "auto_submit_threshold": settings.auto_submit_threshold,
        "daily_application_cap": settings.daily_application_cap,
        "persisted": False,
        "note": (
            "This applies until the server restarts. Set CAREERAGENT_AUTONOMY_LEVEL "
            "in .env to make it permanent."
        ),
    }


@router.get("/weights")
async def weights() -> dict[str, Any]:
    """The active ranking weights, with their version."""
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        active = await repo.active_weights(session, row.id)
        defaults = ScoreWeights()
        return {
            "version": active.version if active else defaults.version,
            "weights": active.weights if active else defaults.model_dump(exclude={"version"}),
            "rationale": active.rationale if active else "defaults",
            "thresholds": PriorityThresholds().model_dump(),
            "components": {
                "job_fit": "blended deterministic + semantic + LLM match",
                "candidate_match": "skill overlap alone",
                "company_fit": "researched company suitability",
                "role_relevance": "does the title match your target roles",
                "freshness": "how recently it was posted",
                "location": "against your preferred locations",
                "application_feasibility": "how reliably this ATS can be applied to",
                "career_potential": "does the role stretch you slightly",
            },
            "note": (
                "A component with no evidence is excluded and its weight redistributed, "
                "not scored zero — an unresearched company must not rank below a "
                "researched bad one."
            ),
        }


@router.put("/weights")
async def set_weights(body: WeightsRequest) -> dict[str, Any]:
    """Set ranking weights explicitly, as a new version."""
    known = set(ScoreWeights().model_dump(exclude={"version"}))
    unknown = set(body.weights) - known
    if unknown:
        raise HTTPException(422, f"unknown weight(s): {', '.join(sorted(unknown))}")
    if any(v < 0 for v in body.weights.values()):
        raise HTTPException(422, "weights cannot be negative")
    if not body.weights:
        raise HTTPException(422, "no weights given")

    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        current = await repo.active_weights(session, row.id)
        merged = dict(
            current.weights if current else ScoreWeights().model_dump(exclude={"version"})
        )
        merged.update(body.weights)
        saved = await repo.save_weights(session, row.id, merged, body.rationale or "set by user")
        return {"version": saved.version, "weights": saved.weights}


@router.get("/llm")
async def llm() -> dict[str, Any]:
    """Which providers are reachable, the model chains, and the budget.

    Reports reachability, never the keys themselves. `configured` means a key was
    found in the environment or the keyring — not that it is valid, which is only
    knowable by making a call.
    """
    from ..llm.router import Tier, get_router

    settings = get_settings()
    router_instance = get_router()

    providers: dict[str, dict[str, Any]] = {}
    for name in ("groq", "gemini", "openai", "anthropic", "xai"):
        providers[name] = {"configured": bool(get_api_key(name))}
    providers["ollama"] = {"configured": True, "note": "local; needs no key"}

    return {
        "available": router_instance.available(),
        "providers": providers,
        "chains": {
            "cheap": settings.llm_chain_cheap,
            "strong": settings.llm_chain_strong,
        },
        "usable_now": {
            "cheap": router_instance.chain(Tier.CHEAP),
            "strong": router_instance.chain(Tier.STRONG),
        },
        "budget": {
            "daily_limit_usd": settings.llm_daily_budget_usd,
            "spent_today_usd": round(router_instance.budget.spent, 6),
            "remaining_usd": round(router_instance.budget.remaining, 6),
        },
        "cache_enabled": settings.llm_cache_enabled,
        "embedding_model": settings.llm_embedding_model,
        "note": (
            "Keys are read from the environment or the OS keyring and are never "
            "returned by this API. Everything degrades to deterministic scoring when "
            "no model is reachable."
        ),
    }


# ─── Database health, snapshots and restore ─────────────────────────────────
#
# Exposed because the corpus was lost once and rebuilding it costs hours of other people's
# servers plus a chunk of several metered free tiers. A snapshot is cheap; discovering you
# needed one is not.


@router.get("/database")
async def database_health() -> dict[str, Any]:
    """Whether the database is intact, and what protection it currently has."""
    from ..storage import backup
    from ..storage.db import resolve_url
    from ..storage.safety import describe

    url = resolve_url()
    return {**backup.health(url), "safety": describe(url)}


@router.post("/database/snapshot")
async def database_snapshot(label: str = Query("", max_length=60)) -> dict[str, Any]:
    """Copy the database to a timestamped file. Never destructive."""
    from ..storage import backup
    from ..storage.db import resolve_url

    try:
        snap = backup.snapshot(resolve_url(), label=label)
    except backup.BackupError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {**snap.to_dict(), "note": "Snapshot taken. Nothing else was changed."}


@router.get("/database/snapshots")
async def database_snapshots() -> dict[str, Any]:
    from ..storage import backup
    from ..storage.db import resolve_url

    snapshots = backup.list_snapshots(resolve_url())
    return {"snapshots": [s.to_dict() for s in snapshots], "count": len(snapshots)}


@router.post("/database/restore")
async def database_restore(
    name: str = Query(..., description="Snapshot filename, from /database/snapshots"),
    confirm: bool = Query(False),
) -> dict[str, Any]:
    """Replace the database with a snapshot.

    Destructive, so it takes an explicit confirm and goes through the same guard as every
    other destructive operation. It snapshots the current state before replacing it.
    """
    from ..storage import backup, dispose
    from ..storage.db import resolve_url
    from ..storage.safety import UnsafeDatabaseOperation

    if not confirm:
        raise HTTPException(400, "pass confirm=true — this replaces the current database")

    url = resolve_url()
    await dispose()
    try:
        chosen = backup.restore(url, name, force=True)
    except UnsafeDatabaseOperation as exc:
        raise HTTPException(409, str(exc)) from exc
    except backup.BackupError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {
        "restored": chosen.to_dict(),
        "note": (
            "Restored. The state it replaced was snapshotted first, labelled "
            "'before restore'. Restart the server so every connection sees the new file."
        ),
    }
