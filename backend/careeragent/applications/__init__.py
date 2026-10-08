"""Applications: the state machine, the pipeline, the tracker and outreach.

Layering, and the reason for it:

    states.py      pure transition table — no DB, no browser, exhaustively testable
    service.py     the only module that can cause an irreversible external action
    tracker.py     read-only projections; looking at the board never changes it
    answers.py     open-question answering, grounded in the Fact Bank
    cover_letter.py  prose generation, with no deterministic fallback by design
    outreach.py    per-message approval and rate limits enforced in code
    sessions.py    live browser sessions held between prepare and approve

The invariant worth restating: `states.transition()` refuses APPLIED without
submission evidence, and `service` is the only caller. Nothing in this package can
report an application as submitted on the strength of a click.
"""

from .service import (
    ApplicationError,
    PrepareResult,
    approve,
    cover_letter_for,
    prepare,
    prepare_batch,
    reject,
    set_status,
    shortlist,
    tailor_for,
    takeover,
)
from .states import (
    BOARD_COLUMNS,
    MAX_ATTEMPTS,
    NEEDS_HUMAN,
    RESPONDED_STATES,
    SUBMITTED_STATES,
    TERMINAL,
    TRANSITIONS,
    IllegalTransition,
    board_column,
    can_transition,
    is_open,
    transition,
)

__all__ = [
    "BOARD_COLUMNS",
    "MAX_ATTEMPTS",
    "NEEDS_HUMAN",
    "RESPONDED_STATES",
    "SUBMITTED_STATES",
    "TERMINAL",
    "TRANSITIONS",
    "ApplicationError",
    "IllegalTransition",
    "PrepareResult",
    "approve",
    "board_column",
    "can_transition",
    "cover_letter_for",
    "is_open",
    "prepare",
    "prepare_batch",
    "reject",
    "set_status",
    "shortlist",
    "tailor_for",
    "takeover",
    "transition",
]
