"""Browser automation.

The design rule, from docs/01-architecture.md §7: JavaScript *gathers* DOM state,
Python *decides* what to do about it. Every decision that can block a submission
lives in a pure function here, testable without a browser.
"""

from .catalog import CATALOG, CanonicalField, FieldKind, Sensitivity, classify, never_fill
from .resolver import Resolution, ResolvedField, resolve, resolve_all
from .runner import matching_option, option_labels
from .verdict import Failure, FieldState, presubmit_verdict, safe_to_submit, value_matches

__all__ = [
    "CATALOG",
    "CanonicalField",
    "Failure",
    "FieldKind",
    "FieldState",
    "Resolution",
    "ResolvedField",
    "Sensitivity",
    "classify",
    "matching_option",
    "never_fill",
    "option_labels",
    "presubmit_verdict",
    "resolve",
    "resolve_all",
    "safe_to_submit",
    "value_matches",
]
