"""API keys: what is configured, what still works, and how much is left.

    GET /integrations            configuration and local counts — no network calls
    GET /integrations?probe=true ask each provider for its own balance
    GET /integrations/{id}       one provider, always probed

Probing is opt-in because it costs something. Most providers answer a balance query for
free, but a few can only be checked by making a real call against the quota being reported —
a status page that quietly spends the budget it is reporting on would be absurd. Those are
marked `costs_a_call` and are never probed automatically.

**No response here contains a key.** Only the last four characters, which is enough to tell
two keys apart when swapping one and useless to anyone else.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import integrations
from ..logging import get_logger
from ..storage import session_scope

log = get_logger("routes.integrations")

router = APIRouter(prefix="/api/v1/integrations", tags=["integrations"])


@router.get("")
async def read(
    probe: bool = Query(
        False,
        description="Ask each provider for its own remaining quota. Slower, and a few "
        "providers can only be checked by spending a call.",
    ),
) -> dict[str, Any]:
    """Every integration, with quota where the provider publishes one."""
    async with session_scope() as session:
        return await integrations.status(session, probe=probe)


@router.get("/{integration_id}")
async def read_one(integration_id: str) -> dict[str, Any]:
    """One provider, always freshly probed.

    Separate from the list so checking a single key you have just rotated does not mean
    re-probing everything else.
    """
    if integration_id not in integrations.BY_ID:
        raise HTTPException(404, f"unknown integration {integration_id!r}")

    async with session_scope() as session:
        everything = await integrations.status(session, probe=True)

    for entry in everything["integrations"]:
        if entry["id"] == integration_id:
            return entry
    raise HTTPException(404, f"unknown integration {integration_id!r}")
