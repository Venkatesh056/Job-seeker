"""Company people intelligence: who works there, who matters, and how to reach them.

Three modules, each answering one question:

    discover.py   who works there, from what the company itself publishes
    relevance.py  which of them matters for this candidate and this opening
    store.py      remembering them, and not contacting the same person twice

The rule running through all of it is that a contact detail cannot exist without a source.
Given a name and a domain, `firstname.lastname@company.com` is a plausible guess — and a
plausible guess sitting beside real data is indistinguishable from it. So guessing is not
implemented rather than merely discouraged.
"""

from . import store
from .discover import (
    DiscoveryResult,
    deduplicate,
    discover,
    extract_people,
    looks_like_a_name,
    looks_like_a_title,
)
from .relevance import (
    CAREER_PRIORITIES,
    FUNCTION_MARKERS,
    PersonRelevance,
    career_family,
    function_of,
    has_hiring_authority,
    rank,
    score_person,
    seniority_of,
)

__all__ = [
    "CAREER_PRIORITIES",
    "FUNCTION_MARKERS",
    "DiscoveryResult",
    "PersonRelevance",
    "career_family",
    "deduplicate",
    "discover",
    "extract_people",
    "function_of",
    "has_hiring_authority",
    "looks_like_a_name",
    "looks_like_a_title",
    "rank",
    "score_person",
    "seniority_of",
    "store",
]
