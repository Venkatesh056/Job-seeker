"""Agents that guard correctness, closing the gaps found in the §84 audit.

Four of them, and each exists because something the rest of the system did *not* check
could cost the user a real application:

  * `freshness`            — is this posting still live? Checked before spending a
                             strong-tier LLM call and a browser on it.
  * `verification`         — does the filled form contradict the resume being attached
                             to it? "Resume says 1 year, form says 2" was the spec's own
                             example and nothing caught it.
  * `source_verification`  — which of these three listings is canonical, and which door
                             should the application actually go through?
  * `recovery`             — what kind of failure was that, and does retrying it make
                             any sense?

Plus two that are not agents:

  * `trust`  — the boundary that makes external text a document rather than an
               instruction.
  * `audit`  — where an agent records *why* it decided what it decided, so the choice
               can still be explained weeks later.

All five are **pure** apart from `freshness`, which makes exactly one HTTP request. That
is deliberate and matches the existing validators: these decide whether an irreversible
action proceeds, so they must not be able to fail for an unrelated reason, and they must
be exhaustively testable without a browser or a network.
"""

from . import audit
from .freshness import Freshness, FreshnessResult
from .freshness import check as check_freshness
from .freshness import classify as classify_freshness
from .recovery import Action, FailureKind, Prescription, RecoveryLog, classify, prescribe
from .source_verification import CanonicalJob, SourceType, classify_source
from .source_verification import summarize as summarize_sources
from .source_verification import verify as verify_sources
from .trust import Untrusted, harden, neutralize, safe_job_text, safe_page_text, scan
from .verification import Conflict, Severity, VerificationResult, blocks_submission, verify

__all__ = [
    "Action",
    "CanonicalJob",
    "Conflict",
    "FailureKind",
    "Freshness",
    "FreshnessResult",
    "Prescription",
    "RecoveryLog",
    "Severity",
    "SourceType",
    "Untrusted",
    "VerificationResult",
    "audit",
    "blocks_submission",
    "check_freshness",
    "classify",
    "classify_freshness",
    "classify_source",
    "harden",
    "neutralize",
    "prescribe",
    "safe_job_text",
    "safe_page_text",
    "scan",
    "summarize_sources",
    "verify",
    "verify_sources",
]
