"""Persistence.

SQLite by default so the agent runs with no infrastructure; Postgres + pgvector
via `docker compose up -d` when semantic search at scale is wanted. The only
difference between the two is handled in `types.py`.
"""

from .db import (
    dispose,
    get_engine,
    get_session_factory,
    init_db,
    is_postgres,
    resolve_url,
    session_scope,
)
from .models import (
    Application,
    ApplicationStatus,
    ATSBoard,
    Base,
    Candidate,
    Company,
    Fact,
    Gap,
    Job,
    JobListing,
    JobScore,
)

__all__ = [
    "ATSBoard",
    "Application",
    "ApplicationStatus",
    "Base",
    "Candidate",
    "Company",
    "Fact",
    "Gap",
    "Job",
    "JobListing",
    "JobScore",
    "dispose",
    "get_engine",
    "get_session_factory",
    "init_db",
    "is_postgres",
    "resolve_url",
    "session_scope",
]
