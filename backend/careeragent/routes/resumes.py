"""Resume endpoints: archetypes, tailoring, validation reports, downloads.

`POST /tailor` is the one that matters. It returns the validation report alongside
the document — including the violations, when there are any — because a tailored
resume the user cannot inspect is a tailored resume they have to trust blindly.

A render with evidence violations is stored and returned, but no PDF is written for
it and `/download` will not serve one. The user sees exactly what was wrong.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel, Field

from ..logging import get_logger
from ..resume import archetypes as arch
from ..storage import repository as repo
from ..storage import session_scope
from ..storage.models import Job

log = get_logger("routes.resumes")

router = APIRouter(prefix="/api/v1/resumes", tags=["resumes"])


class TailorRequest(BaseModel):
    job_id: str
    archetype: str | None = Field(
        default=None, description="Force an archetype; omit to choose from the job title"
    )
    use_llm: bool = True
    render_pdf: bool = True


@router.get("/variants")
async def variants() -> list[dict[str, Any]]:
    """The archetypes available, and what each emphasises.

    Not separate documents: an archetype is an ordering over the one Fact Bank, which
    is why a variant cannot contain anything the master resume does not.
    """
    return [
        {
            "id": a.id,
            "label": a.label,
            "framing": a.framing,
            "title_markers": list(a.title_markers),
            "priority_skills": list(a.priority_skills),
            "de_emphasize": list(a.de_emphasize),
        }
        for a in arch.ARCHETYPES
    ]


@router.post("/tailor")
async def tailor_resume(body: TailorRequest) -> dict[str, Any]:
    """Tailor, validate, render and store a resume for one job."""
    from ..applications import service
    from ..applications.service import ApplicationError

    async with session_scope() as session:
        candidate = await repo.load_candidate(session)
        if candidate is None:
            raise HTTPException(409, "Upload your resume first — tailoring needs a Fact Bank")
        row = await repo.get_or_create_candidate(session)

        job = await session.get(Job, body.job_id)
        if job is None:
            raise HTTPException(404, "job not found")

        try:
            application = await service.shortlist(session, body.job_id, row.id)
            render_id, validation = await service.tailor_for(
                session,
                application,
                candidate,
                archetype=body.archetype,
                use_llm=body.use_llm,
                render_pdf=body.render_pdf,
                # An explicit request to tailor must tailor. Reuse is for the internal
                # callers that would otherwise duplicate each other's work.
                force=True,
            )
        except ApplicationError as exc:
            raise HTTPException(409, str(exc)) from exc

        render = await repo.get_resume_render(session, render_id) if render_id else None
        content = (render.content if render else {}) or {}
        return {
            "render_id": render_id,
            "application_id": application.id,
            "archetype": render.archetype if render else None,
            "validation": validation,
            "downloadable": bool(render and render.storage_key),
            # `notes` carries the rejection when a model rewrite failed the evidence
            # check. That is the single most important thing to show the user about a
            # tailored resume, so it travels with the response rather than only
            # existing in the log.
            "generated_by": content.get("generated_by", "deterministic"),
            "notes": content.get("notes", []),
            "gaps": content.get("gaps", []),
            "resume": content,
        }


@router.get("/renders")
async def renders(limit: int = Query(50, ge=1, le=200)) -> list[dict[str, Any]]:
    """Every generated resume, newest first — the Resume Center list."""
    async with session_scope() as session:
        row = await repo.get_or_create_candidate(session)
        rows = await repo.list_renders(session, row.id, limit)
        jobs: dict[str, Job] = {}
        for render in rows:
            if render.job_id and render.job_id not in jobs:
                job = await session.get(Job, render.job_id)
                if job is not None:
                    jobs[render.job_id] = job

        return [
            {
                "id": r.id,
                "archetype": r.archetype,
                "job_id": r.job_id,
                "job_title": jobs[r.job_id].title if r.job_id in jobs else None,
                "company": jobs[r.job_id].company if r.job_id in jobs else None,
                "ats_score": r.ats_score,
                "evidence_integrity": r.evidence_integrity,
                "keyword_match": r.keyword_match,
                "ok": bool((r.validation or {}).get("ok")),
                "violations": len((r.validation or {}).get("violations") or []),
                "fact_count": len(r.fact_ids or []),
                "downloadable": bool(r.storage_key),
                "generated_by": (r.content or {}).get("generated_by", "deterministic"),
                "notes": (r.content or {}).get("notes", []),
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]


@router.get("/renders/{render_id}")
async def render_detail(render_id: str) -> dict[str, Any]:
    """One render, with its content, evidence and validation report."""
    async with session_scope() as session:
        render = await repo.get_resume_render(session, render_id)
        if render is None:
            raise HTTPException(404, "render not found")
        facts = {}
        candidate = await repo.load_candidate(session)
        if candidate:
            facts = {str(f.id): f for f in candidate.facts}

        return {
            "id": render.id,
            "archetype": render.archetype,
            "job_id": render.job_id,
            "content": render.content,
            "validation": render.validation,
            "storage_key": render.storage_key,
            "downloadable": bool(render.storage_key),
            "created_at": render.created_at.isoformat(),
            # The point of recording fact_ids: when a recruiter asks about a bullet,
            # the evidence behind it is retrievable rather than remembered.
            "evidence": [
                {
                    "id": fid,
                    "kind": facts[fid].kind.value,
                    "label": facts[fid].label,
                    "locator": facts[fid].evidence_locator,
                    "verified": facts[fid].verified,
                }
                for fid in (render.fact_ids or [])
                if fid in facts
            ],
        }


@router.get("/renders/{render_id}/download")
async def download_render(render_id: str) -> FileResponse:
    """Serve the rendered file. Refuses a render that failed validation."""
    async with session_scope() as session:
        render = await repo.get_resume_render(session, render_id)
        if render is None:
            raise HTTPException(404, "render not found")
        if not (render.validation or {}).get("ok", False):
            raise HTTPException(
                409,
                "This resume has unresolved evidence violations and was not rendered. "
                "Fix the Fact Bank or tailor it again.",
            )
        if not render.storage_key:
            raise HTTPException(404, "no file was written for this render")

        from ..storage.objects import get_store

        # A key, or a legacy absolute path from before the object store existed. 410
        # rather than 404 for a key that no longer resolves: the render is recorded and
        # the bytes are gone, which is a different fact from "no such render".
        path = await get_store().materialise(str(render.storage_key))
        if path is None:
            raise HTTPException(410, "the rendered file is no longer in the store")
        return FileResponse(path, filename=path.name)


@router.get("/renders/{render_id}/preview")
async def preview_render(render_id: str) -> HTMLResponse:
    """The render as HTML, for reviewing in the dashboard without a download."""
    from ..resume.documents import TailoredResume
    from ..resume.render import to_html

    async with session_scope() as session:
        render = await repo.get_resume_render(session, render_id)
        if render is None:
            raise HTTPException(404, "render not found")
        resume = TailoredResume.from_dict(render.content or {})
        return HTMLResponse(to_html(resume))


@router.get("/cover-letters/{letter_id}")
async def cover_letter(letter_id: str) -> dict[str, Any]:
    async with session_scope() as session:
        row = await repo.get_cover_letter(session, letter_id)
        if row is None:
            raise HTTPException(404, "cover letter not found")
        return {
            "id": row.id,
            "job_id": row.job_id,
            "body": row.body,
            "fact_ids": row.fact_ids or [],
            "validation": row.validation or {},
            "model": row.model,
            "created_at": row.created_at.isoformat(),
        }
