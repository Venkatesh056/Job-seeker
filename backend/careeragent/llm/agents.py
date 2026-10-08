"""LLM agents: job understanding and match reasoning.

Two rules govern everything here, both from docs/01-architecture.md §4:

  1. **No LLM output reaches an irreversible action without a pure function in
     between.** These agents produce structured data that the deterministic
     layer validates and can override. They advise; they do not decide.
  2. **Every agent degrades to None.** No key, no budget, a dead provider, an
     unparseable reply — all return None, and the caller keeps its stage-1
     result. The system stays useful with no LLM at all.

Cost discipline is in the call sites, not the prompts: extraction runs on the
cheap tier, scoring runs batched, and both only ever see jobs that already
cleared the deterministic gate.
"""

from __future__ import annotations

import contextlib
from typing import Any

from ..agents.trust import harden, safe_job_text
from ..logging import get_logger
from ..models.candidate import Candidate
from ..models.job import NormalizedJob
from ..models.scoring import LLMScore
from ..prompts import get as get_prompt
from .router import LLMRouter, Tier, get_router, tokens_for

log = get_logger("llm.agents")

# Jobs per scoring call. Enough to amortise prompt overhead, small enough that
# one malformed reply does not cost a whole run's worth of scores.
SCORE_BATCH_SIZE = 8

MAX_JD_CHARS = 6000


# ─── Job understanding ──────────────────────────────────────────────────────

# The text lives in `prompts/`, versioned and sharing one copy of the
# anti-fabrication rules with every other prompt. Held as module constants so the
# version is resolved once at import rather than per job.
EXTRACTION = get_prompt("extract_job")
SCORING = get_prompt("score_jobs")


def extract_job(job: NormalizedJob, router: LLMRouter | None = None) -> dict[str, Any] | None:
    """Structured facts from a job description, or None.

    Runs on the cheap tier: this is high-volume and mechanical, and a stronger
    model adds nothing to reading a list of requirements back out.
    """
    router = router or get_router()
    if not job.description.strip():
        return None

    # The description is the one piece of genuinely external text in this prompt, so it
    # is neutralised and fenced before it reaches the model (spec §75).
    fenced, untrusted = safe_job_text(job.description[:MAX_JD_CHARS])
    if untrusted.suspicious:
        log.warning(
            "job_description_injection_attempt",
            job_id=job.job_id,
            company=job.company,
            findings=untrusted.findings,
        )

    prompt = EXTRACTION.render(
        title=job.title,
        company=job.company,
        location=job.location or "not stated",
        description=fenced,
    )
    data = router.complete_json(
        prompt,
        tier=Tier.CHEAP,
        system=harden(EXTRACTION.system),
        temperature=0.0,
        # Thirteen fields, several of them short lists.
        max_tokens=tokens_for(800),
        agent="job_extraction",
    )
    if not isinstance(data, dict):
        return None
    return _clean_extraction(data)


def _clean_extraction(data: dict[str, Any]) -> dict[str, Any]:
    """Coerce the model's reply into the shape the rest of the system expects.

    Models return "3-5" for a year count, strings where lists belong, and
    "N/A" where null belongs. Normalising here keeps every consumer from
    having to defend against it.
    """

    def as_list(value: Any, limit: int = 20) -> list[str]:
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            return []
        out = []
        for item in value[:limit]:
            text = str(item).strip()
            if text and text.lower() not in {"n/a", "none", "null", "not stated"}:
                out.append(text)
        return out

    def as_number(value: Any) -> float | None:
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            import re

            if m := re.search(r"\d+(?:\.\d+)?", value):
                return float(m.group(0))
        return None

    def as_enum(value: Any, allowed: set[str]) -> str | None:
        text = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
        return text if text in allowed else None

    return {
        "required_skills": as_list(data.get("required_skills")),
        "preferred_skills": as_list(data.get("preferred_skills")),
        "responsibilities": as_list(data.get("responsibilities"), 6),
        "education": as_list(data.get("education"), 5),
        "min_years_experience": as_number(data.get("min_years_experience")),
        "max_years_experience": as_number(data.get("max_years_experience")),
        "seniority": as_enum(data.get("seniority"), {"intern", "entry", "mid", "senior", "lead"}),
        "employment_type": as_enum(
            data.get("employment_type"),
            {"internship", "full_time", "part_time", "contract"},
        ),
        "work_mode": as_enum(data.get("work_mode"), {"remote", "hybrid", "onsite"}),
        "sponsorship_required": data.get("sponsorship_required")
        if isinstance(data.get("sponsorship_required"), bool)
        else None,
        "domain": (str(data.get("domain")).strip() or None) if data.get("domain") else None,
        "hidden_constraints": as_list(data.get("hidden_constraints"), 6),
        "keywords": as_list(data.get("keywords"), 12),
    }


# ─── Match reasoning (stage 3) ──────────────────────────────────────────────


def _candidate_summary(candidate: Candidate) -> dict[str, str]:
    from ..models.candidate import FactKind

    def joined(kind: FactKind, limit: int, with_body: bool = False) -> str:
        items = [f for f in candidate.facts if f.kind is kind][:limit]
        if with_body:
            return " | ".join(f"{f.label}: {f.body[:140]}" for f in items) or "none stated"
        return " | ".join(f.label for f in items) or "none stated"

    years = candidate.total_years_experience()
    return {
        "skills": ", ".join(sorted(candidate.skill_labels())) or "none stated",
        "experience": joined(FactKind.EXPERIENCE, 5, with_body=True),
        "projects": joined(FactKind.PROJECT, 5, with_body=True),
        "education": joined(FactKind.EDUCATION, 3),
        "years": f"{years} years" if years is not None else "not stated",
    }


def score_jobs(
    candidate: Candidate,
    jobs: list[NormalizedJob],
    router: LLMRouter | None = None,
    batch_size: int = SCORE_BATCH_SIZE,
) -> dict[str, LLMScore]:
    """Stage-3 reasoning for jobs that already cleared the cheap gate.

    Returns a score per job id; jobs the model skipped or mangled are simply
    absent, and the caller keeps their stage-1/2 result.
    """
    router = router or get_router()
    if not jobs:
        return {}

    profile = _candidate_summary(candidate)
    out: dict[str, LLMScore] = {}

    for start in range(0, len(jobs), batch_size):
        batch = jobs[start : start + batch_size]
        rendered = "\n\n".join(
            f"[{i}] {job.title} at {job.company} ({job.location or 'location not stated'})\n"
            f"{safe_job_text((job.description or 'No description available.')[:1200])[0]}"
            for i, job in enumerate(batch)
        )
        data = router.complete_json(
            SCORING.render(**profile, jobs=rendered),
            tier=Tier.CHEAP,
            system=harden(SCORING.system),
            temperature=0.1,
            # Eight jobs in one reply, each with four short lists and a sentence. A
            # truncated batch loses every score in it, not just the last one.
            max_tokens=tokens_for(SCORE_BATCH_SIZE * 220),
            agent="job_scoring",
        )
        if not isinstance(data, dict):
            continue

        for entry in data.get("scores") or []:
            if not isinstance(entry, dict):
                continue
            index = entry.get("i")
            if not isinstance(index, int) or not 0 <= index < len(batch):
                continue
            try:
                raw = float(entry.get("score", 0.0))
            except (TypeError, ValueError):
                continue
            job = batch[index]
            out[job.job_id] = LLMScore(
                score=max(0.0, min(1.0, raw)),
                strengths=_strings(entry.get("strengths")),
                weaknesses=_strings(entry.get("weaknesses")),
                missing_skills=_strings(entry.get("missing")),
                concerns=_strings(entry.get("concerns")),
                strategy=str(entry.get("strategy") or "")[:400],
            )

    log.info("llm_scored", requested=len(jobs), returned=len(out))
    return out


def _strings(value: Any, limit: int = 4) -> list[str]:
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    return [str(v).strip()[:200] for v in value[:limit] if str(v).strip()]


def apply_extraction(job: NormalizedJob, extracted: dict[str, Any]) -> NormalizedJob:
    """Fold extracted facts into a job — only where we had nothing.

    The deterministic parse wins on every field it already resolved. The model
    fills blanks; it does not overwrite evidence.
    """
    from ..models.job import EmploymentType, Seniority, WorkMode

    if not job.requirements and extracted.get("required_skills"):
        job.requirements = extracted["required_skills"]
    if not job.preferred_skills and extracted.get("preferred_skills"):
        job.preferred_skills = extracted["preferred_skills"]
    if not job.education_requirements and extracted.get("education"):
        job.education_requirements = extracted["education"]
    if job.min_years_experience is None:
        job.min_years_experience = extracted.get("min_years_experience")
    if job.max_years_experience is None:
        job.max_years_experience = extracted.get("max_years_experience")

    if job.seniority is Seniority.UNKNOWN and extracted.get("seniority"):
        with contextlib.suppress(ValueError):
            job.seniority = Seniority(extracted["seniority"])
    if job.employment_type is EmploymentType.UNKNOWN and extracted.get("employment_type"):
        with contextlib.suppress(ValueError):
            job.employment_type = EmploymentType(extracted["employment_type"])
    if job.work_mode is WorkMode.UNKNOWN and extracted.get("work_mode"):
        with contextlib.suppress(ValueError):
            job.work_mode = WorkMode(extracted["work_mode"])
    return job
