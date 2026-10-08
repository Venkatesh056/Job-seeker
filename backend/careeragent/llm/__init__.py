"""LLM layer.

Everything here is optional. With no API key configured the router reports
unavailable, every agent returns None, and the pipeline keeps its deterministic
results — the system is designed to be useful without an LLM at all.
"""

from .agents import apply_extraction, extract_job, score_jobs
from .router import Budget, LLMResult, LLMRouter, Tier, extract_json, get_router

__all__ = [
    "Budget",
    "LLMResult",
    "LLMRouter",
    "Tier",
    "apply_extraction",
    "extract_job",
    "extract_json",
    "get_router",
    "score_jobs",
]
