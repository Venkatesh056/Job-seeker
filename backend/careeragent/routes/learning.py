"""Learning endpoints: observations, proposals, and an explicit apply.

`POST /proposals/{id}/apply` is the only way a weight ever changes, and it takes a
deliberate call from the user. There is no scheduler behind these endpoints and no
confidence threshold at which the agent applies its own proposal — see the module
docstring in `learning.py` for why that is the honest design at these data volumes.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .. import learning
from ..storage import repository as repo
from ..storage import session_scope

router = APIRouter(prefix="/api/v1/learning", tags=["learning"])


@router.get("/observations")
async def observations() -> dict[str, Any]:
    """What the outcomes actually show. Descriptive only — no recommendations."""
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        items = await learning.observations(session, row.id)
        return {
            "observations": items,
            "min_observations": learning.MIN_OBSERVATIONS,
        }


@router.post("/propose")
async def propose() -> dict[str, Any]:
    """Generate a weight proposal from the outcomes and store it for review."""
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        proposal = await learning.propose_weights(session, row.id)
        stored = await learning.save_proposal(session, row.id, proposal)
        return {
            "proposal_id": stored.id,
            **proposal.to_dict(),
            "note": (
                "Nothing has changed. Apply it explicitly if you agree with it — and "
                "note that response rates cannot tell 'this role suits you' apart from "
                "'these companies happened to be hiring'."
            ),
        }


@router.get("/proposals")
async def proposals(limit: int = Query(20, ge=1, le=100)) -> list[dict[str, Any]]:
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        rows = await learning.list_proposals(session, row.id, limit)
        return [
            {
                "id": p.id,
                "kind": p.kind,
                "observation": p.observation,
                "sample_size": p.sample_size,
                "confidence": p.confidence,
                "current": p.current,
                "proposed": p.proposed,
                "status": p.status,
                "created_at": p.created_at.isoformat(),
                "applied_at": p.applied_at.isoformat() if p.applied_at else None,
            }
            for p in rows
        ]


@router.post("/proposals/{proposal_id}/apply")
async def apply_proposal(proposal_id: str, confirm: bool = Query(False)) -> dict[str, Any]:
    if not confirm:
        raise HTTPException(400, "pass confirm=true to change your ranking weights")
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        try:
            return await learning.apply_proposal(session, row.id, proposal_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc


@router.post("/revert")
async def revert() -> dict[str, Any]:
    """Go back to the previous weight version. The reason versions exist."""
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        try:
            return await learning.revert_weights(session, row.id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
