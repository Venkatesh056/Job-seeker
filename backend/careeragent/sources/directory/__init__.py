"""Company discovery by region: who is hiring, before what is open.

The existing ATS readers answer "what is open at Stripe?" — exact and free, but only
once you know to ask about Stripe. These sources answer the question that comes first:
"which companies in Bangalore are hiring for data roles?"

The two compose, and that composition is the point:

    directory source  →  leads   (broad, metered free tiers, any sector)
    ATS reader        →  apply   (exact, unmetered, the company's own form)
"""

from .base import (
    Company,
    CompanyHit,
    DirectorySource,
    SourceResult,
    aggregate,
    looks_like_recruiter,
    normalize_company,
)
from .providers import SOURCES, SOURCES_BY_ID, available, describe, http_client

__all__ = [
    "SOURCES",
    "SOURCES_BY_ID",
    "Company",
    "CompanyHit",
    "DirectorySource",
    "SourceResult",
    "aggregate",
    "available",
    "describe",
    "http_client",
    "looks_like_recruiter",
    "normalize_company",
]
