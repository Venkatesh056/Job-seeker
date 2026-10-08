"""Generalized career taxonomy: job families across every sector.

The system started with eight AI/ML resume archetypes, which was right for one
candidate and wrong as a product — a civil engineer's resume would have been scored
against "PyTorch, Transformers, RAG" and rejected by its own relevance gate.

`families.py` is the vocabulary; `detect.py` works out which of it a given resume
speaks, from evidence rather than from the heading at the top of the page.
"""

from .detect import Detection, FamilyMatch, archetype_for, detect, suggest_families
from .families import FAMILIES, GROUPS, Family, by_group, expand, for_title, get, search_queries

__all__ = [
    "FAMILIES",
    "GROUPS",
    "Detection",
    "Family",
    "FamilyMatch",
    "archetype_for",
    "by_group",
    "detect",
    "expand",
    "for_title",
    "get",
    "search_queries",
    "suggest_families",
]
