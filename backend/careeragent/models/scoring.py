"""Scoring models.

The scoring pipeline is three stages with very different costs, and the models
keep them separable so a cheap stage can stand alone when the budget is spent:

    deterministic (free)  →  semantic (cheap, cached)  →  LLM (expensive)
"""

from __future__ import annotations

import enum

from pydantic import BaseModel, Field


class Priority(enum.StrEnum):
    VERY_HIGH = "very_high"
    HIGH = "high"
    REVIEW = "review"
    LOW = "low"
    REJECT = "reject"

    @classmethod
    def from_score(cls, score: float, thresholds: PriorityThresholds | None = None) -> Priority:
        t = thresholds or PriorityThresholds()
        if score >= t.very_high:
            return cls.VERY_HIGH
        if score >= t.high:
            return cls.HIGH
        if score >= t.review:
            return cls.REVIEW
        if score >= t.low:
            return cls.LOW
        return cls.REJECT


class PriorityThresholds(BaseModel):
    very_high: float = 0.92
    high: float = 0.85
    review: float = 0.75
    low: float = 0.60


class DeterministicScore(BaseModel):
    """Stage 1: pure, free, explainable. Also the gate that decides which jobs are
    worth spending LLM tokens on."""

    skills: float = 0.0
    role: float = 0.0
    experience: float = 0.0
    education: float = 0.0
    location: float = 0.0
    work_mode: float = 0.0
    seniority: float = 0.0
    employment_type: float = 0.0
    keywords: float = 0.0

    total: float = 0.0
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    hard_fail: str | None = None  # set when a blocking rule rejected the job outright


class SemanticScore(BaseModel):
    """Stage 2: embedding cosine similarity per dimension."""

    resume_to_jd: float = 0.0
    projects_to_requirements: float = 0.0
    experience_to_responsibilities: float = 0.0
    total: float = 0.0


class LLMScore(BaseModel):
    """Stage 3: reasoning, only for jobs that survived stages 1 and 2."""

    score: float = 0.0
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    concerns: list[str] = Field(default_factory=list)
    strategy: str = ""
    model: str = ""
    cost_usd: float = 0.0


class ScoreWeights(BaseModel):
    """Configurable per docs/01-architecture.md §11. Versioned, because a score
    is meaningless to the learning engine without knowing which weights made it."""

    version: int = 1
    job_fit: float = 0.35
    candidate_match: float = 0.20
    company_fit: float = 0.15
    role_relevance: float = 0.10
    freshness: float = 0.05
    location: float = 0.05
    application_feasibility: float = 0.05
    career_potential: float = 0.05

    def normalized(self) -> dict[str, float]:
        raw = self.model_dump(exclude={"version"})
        total = sum(raw.values()) or 1.0
        return {k: v / total for k, v in raw.items()}


class JobScore(BaseModel):
    job_id: str
    deterministic: DeterministicScore = Field(default_factory=DeterministicScore)
    semantic: SemanticScore | None = None
    llm: LLMScore | None = None

    job_fit: float = 0.0
    company_fit: float = 0.0
    final_score: float = 0.0
    priority: Priority = Priority.REJECT
    weights_version: int = 1

    @property
    def explanation(self) -> list[str]:
        """Why this job ranked where it did, in the order a human would ask."""
        out = list(self.deterministic.reasons)
        if self.deterministic.hard_fail:
            out.insert(0, f"Rejected: {self.deterministic.hard_fail}")
        if self.llm:
            out.extend(self.llm.strengths)
            out.extend(f"Concern: {c}" for c in self.llm.concerns)
        return out
