"""Stage-2 semantic matching.

Runs entirely on-device via fastembed (ONNX, no torch, no API key), so the
resume never leaves the machine and the stage costs nothing per job. That is
also why it can afford to run on every job that clears stage 1.

What it adds over keyword matching: a JD asking for "building retrieval systems
over document corpora" and a project described as "RAG pipeline with ChromaDB"
share almost no tokens but are the same capability. Stage 1 cannot see that;
this can.

The model loads lazily and is cached on disk after the first download (~80 MB).
Everything degrades to zero rather than raising — a machine that cannot download
the model still gets stage-1 and stage-3 scoring.
"""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import json
import threading
from pathlib import Path

from ..config import get_settings
from ..logging import get_logger
from ..models.candidate import Candidate, FactKind
from ..models.job import NormalizedJob
from ..models.scoring import SemanticScore

log = get_logger("pipeline.embeddings")

_model = None
_model_lock = threading.Lock()
_unavailable = False


def _get_model():
    """Load the embedding model once, tolerating absence.

    A missing model must not take down a discovery run: semantic scoring is an
    enhancement to stage 1, not a prerequisite for it.
    """
    global _model, _unavailable
    if _model is not None or _unavailable:
        return _model

    with _model_lock:
        if _model is not None or _unavailable:
            return _model
        try:
            from fastembed import TextEmbedding

            settings = get_settings()
            cache = Path(settings.cache_dir) / "fastembed"
            cache.mkdir(parents=True, exist_ok=True)
            _model = TextEmbedding(model_name=settings.llm_embedding_model, cache_dir=str(cache))
            log.info("embedding_model_loaded", model=settings.llm_embedding_model)
        except Exception as exc:
            _unavailable = True
            log.warning(
                "embedding_model_unavailable",
                error=str(exc)[:200],
                effect="semantic scoring disabled; stage 1 and 3 unaffected",
            )
    return _model


def available() -> bool:
    """Can semantic matching run? Answered without loading the model.

    This used to be `_get_model() is not None`, which meant a caller merely *asking*
    triggered an 80 MB download and blocked whatever thread asked. `/api/v1/status` asks,
    so a health check on a cold machine hung for minutes inside the event loop.

    So the question is answered from what can be checked cheaply: is the library
    importable, and have we already established that it is not usable? Whether the
    weights are on disk is not part of it — a first discovery run is allowed to download
    them. `loaded()` is the narrower question, for callers that want to know if the cost
    has already been paid.
    """
    if _model is not None:
        return True
    if _unavailable:
        return False
    return importlib.util.find_spec("fastembed") is not None


def loaded() -> bool:
    """Whether the model is in memory right now — no loading, no downloading."""
    return _model is not None


# ─── Cache ──────────────────────────────────────────────────────────────────


def _cache_path() -> Path:
    path = Path(get_settings().cache_dir) / "embeddings"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _key(text: str) -> str:
    settings = get_settings()
    return hashlib.sha256(f"{settings.llm_embedding_model}|{text}".encode()).hexdigest()[:32]


def embed(texts: list[str], use_cache: bool = True) -> list[list[float]] | None:
    """Embed a batch, reusing anything already computed.

    Job descriptions are re-embedded on every run otherwise, and the same
    posting is seen repeatedly across runs — caching is what keeps this stage
    effectively free after the first pass.
    """
    model = _get_model()
    if model is None or not texts:
        return None

    cache_dir = _cache_path()
    results: list[list[float] | None] = [None] * len(texts)
    pending: list[tuple[int, str]] = []

    for i, text in enumerate(texts):
        if not text.strip():
            continue
        if use_cache:
            path = cache_dir / f"{_key(text)}.json"
            if path.exists():
                try:
                    results[i] = json.loads(path.read_text(encoding="utf-8"))
                    continue
                except (json.JSONDecodeError, OSError):
                    pass  # a corrupt cache entry is just a miss
        pending.append((i, text))

    if pending:
        try:
            vectors = list(model.embed([t for _, t in pending]))
        except Exception as exc:
            log.warning("embedding_failed", error=str(exc)[:200])
            return None
        for (i, text), vector in zip(pending, vectors, strict=True):
            values = [float(x) for x in vector]
            results[i] = values
            if use_cache:
                # An unwritable cache is a slowdown, not a failure.
                with contextlib.suppress(OSError):
                    (cache_dir / f"{_key(text)}.json").write_text(
                        json.dumps(values), encoding="utf-8"
                    )

    return [r if r is not None else [] for r in results]


def cosine(a: list[float], b: list[float]) -> float:
    """Cosine similarity, clamped to [0, 1].

    Negative similarity is not meaningful as a match score here — "less related
    than unrelated" is still just unrelated — so it floors at zero rather than
    dragging a weighted average below what a missing signal would give.
    """
    if not a or not b or len(a) != len(b):
        return 0.0

    import numpy as np

    va, vb = np.asarray(a, dtype=np.float32), np.asarray(b, dtype=np.float32)
    denominator = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denominator == 0.0:
        return 0.0
    return max(0.0, min(1.0, float(np.dot(va, vb) / denominator)))


# ─── Calibration ────────────────────────────────────────────────────────────

# Raw cosine from a BGE-family model does not span [0, 1]. Measured on this
# model: two unrelated texts (a RAG project vs a forklift job ad) sit at ~0.46,
# and a genuine match at ~0.53-0.85. Feeding raw cosine into a weighted score
# would hand every irrelevant job half marks for free.
#
# These bounds rescale the useful band to [0, 1]. They are model-specific — a
# different embedding model needs them re-measured, not assumed.
COSINE_FLOOR = 0.45
COSINE_CEILING = 0.85


def calibrate(similarity: float) -> float:
    """Rescale raw cosine onto [0, 1] so 'unrelated' actually scores near zero."""
    if similarity <= COSINE_FLOOR:
        return 0.0
    if similarity >= COSINE_CEILING:
        return 1.0
    return (similarity - COSINE_FLOOR) / (COSINE_CEILING - COSINE_FLOOR)


# ─── Candidate and job texts ────────────────────────────────────────────────

MAX_CHARS = 4000  # the model truncates anyway; this keeps the cache key stable


def candidate_texts(candidate: Candidate) -> dict[str, str]:
    """The three candidate views compared against a job.

    Kept separate rather than concatenated: "your projects match this JD's
    requirements but your experience does not" is a far more useful signal than
    one blended number, and it is what the explanation surfaces.
    """
    skills = ", ".join(sorted(candidate.skill_labels()))
    projects = "\n".join(
        f"{f.label}. {f.body}" for f in candidate.facts if f.kind is FactKind.PROJECT
    )
    experience = "\n".join(
        f"{f.label}. {f.body}" for f in candidate.facts if f.kind is FactKind.EXPERIENCE
    )
    return {
        "profile": f"Skills: {skills}\n{projects}\n{experience}"[:MAX_CHARS],
        "projects": (projects or skills)[:MAX_CHARS],
        "experience": (experience or projects or skills)[:MAX_CHARS],
    }


def job_texts(job: NormalizedJob) -> dict[str, str]:
    description = job.description or job.title
    requirements = "\n".join(job.requirements) if job.requirements else description
    return {
        "full": f"{job.title}\n{description}"[:MAX_CHARS],
        "requirements": f"{job.title}\n{requirements}"[:MAX_CHARS],
        "responsibilities": description[:MAX_CHARS],
    }


def score_semantic(candidate: Candidate, jobs: list[NormalizedJob]) -> dict[str, SemanticScore]:
    """Semantic score per job id. Empty when the model is unavailable.

    All job texts are embedded in one batch — per-job calls dominate runtime
    otherwise, since model overhead is per-call, not per-token.
    """
    if not jobs or not available():
        return {}

    cand = candidate_texts(candidate)
    cand_vectors = embed([cand["profile"], cand["projects"], cand["experience"]])
    if not cand_vectors:
        return {}
    v_profile, v_projects, v_experience = cand_vectors

    flat: list[str] = []
    for job in jobs:
        jt = job_texts(job)
        flat.extend([jt["full"], jt["requirements"], jt["responsibilities"]])

    job_vectors = embed(flat)
    if not job_vectors:
        return {}

    out: dict[str, SemanticScore] = {}
    for index, job in enumerate(jobs):
        v_full, v_reqs, v_resp = job_vectors[index * 3 : index * 3 + 3]
        resume_to_jd = calibrate(cosine(v_profile, v_full))
        projects_to_reqs = calibrate(cosine(v_projects, v_reqs))
        experience_to_resp = calibrate(cosine(v_experience, v_resp))
        out[job.job_id] = SemanticScore(
            resume_to_jd=round(resume_to_jd, 4),
            projects_to_requirements=round(projects_to_reqs, 4),
            experience_to_responsibilities=round(experience_to_resp, 4),
            total=round(0.5 * resume_to_jd + 0.3 * projects_to_reqs + 0.2 * experience_to_resp, 4),
        )

    log.info("semantic_scored", jobs=len(jobs))
    return out
