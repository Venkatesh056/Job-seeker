"""The application state machine.

Pure, table-driven, and separate from anything that touches a browser or a
database — so the legal transitions are one readable table and every one of them
is unit-testable without infrastructure.

Two rules are enforced here rather than by convention, because convention is
exactly what fails at 2am in an unattended run:

  * **APPLIED requires evidence.** `transition()` refuses to enter APPLIED
    without a non-empty evidence payload. A clicked submit with no confirmation
    is SUBMISSION_UNVERIFIED, which is a first-class state and never collapsed
    into APPLIED — see docs/01-architecture.md §8.
  * **Terminal states are terminal.** Nothing leaves OFFER, WITHDRAWN or
    FAILED_TERMINAL, so a stale background task cannot resurrect an application
    the user closed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..storage.models import ApplicationStatus as S

# Legal transitions. Read this table, not the code that consumes it.
#
# Deliberate inclusions:
#   HUMAN_REQUIRED → APPLYING            after the user takes over and hands back
#   FAILED → APPLYING                    bounded retry (see MAX_ATTEMPTS)
#   SUBMISSION_UNVERIFIED → APPLIED      the user confirmed it manually
#   APPLIED → REJECTED_BY_COMPANY        outcome tracking, distinct from a user
#                                        rejecting the job before applying
TRANSITIONS: dict[S, frozenset[S]] = {
    # FAILED and FAILED_TERMINAL are reachable from every pre-submission state.
    #
    # Preparation genuinely fails — a form that will not parse, a browser that crashes —
    # and the Recovery Agent can prescribe "give up" without a retry ever happening, for
    # a failure that retrying cannot fix. Before these were added, an ordinary timeout
    # during preparation raised IllegalTransition instead of being recorded, which is the
    # opposite of the "never silently fail" rule the recovery design is built on.
    #
    # APPLYING is deliberately excluded from FAILED_TERMINAL: a failure mid-submit must
    # pass through FAILED so that the submission-verification path sees it and the
    # application cannot be written off while a submit may have landed.
    S.DISCOVERED: frozenset({S.SHORTLISTED, S.REJECTED, S.WITHDRAWN, S.FAILED, S.FAILED_TERMINAL}),
    S.SHORTLISTED: frozenset(
        {
            S.RESUME_TAILORED,
            S.READY,
            S.REJECTED,
            S.WITHDRAWN,
            S.FAILED,
            S.FAILED_TERMINAL,
            S.HUMAN_REQUIRED,
        }
    ),
    S.RESUME_TAILORED: frozenset(
        {S.READY, S.REJECTED, S.WITHDRAWN, S.FAILED, S.FAILED_TERMINAL, S.HUMAN_REQUIRED}
    ),
    S.READY: frozenset(
        {
            S.AWAITING_APPROVAL,
            S.APPLYING,
            S.HUMAN_REQUIRED,
            S.REJECTED,
            S.WITHDRAWN,
            S.FAILED,
            S.FAILED_TERMINAL,
        }
    ),
    # FAILED here too: an approval can be answered after the held browser session has
    # expired, and re-preparing at that point can fail like any other preparation.
    S.AWAITING_APPROVAL: frozenset(
        {
            S.APPLYING,
            S.REJECTED,
            S.WITHDRAWN,
            S.HUMAN_REQUIRED,
            S.READY,
            S.FAILED,
            S.FAILED_TERMINAL,
        }
    ),
    S.HUMAN_REQUIRED: frozenset(
        {S.APPLYING, S.READY, S.REJECTED, S.WITHDRAWN, S.FAILED, S.FAILED_TERMINAL}
    ),
    S.APPLYING: frozenset(
        {
            S.APPLIED,
            S.SUBMISSION_UNVERIFIED,
            S.FAILED,
            S.HUMAN_REQUIRED,
            S.AWAITING_APPROVAL,
        }
    ),
    S.APPLIED: frozenset(
        {S.FOLLOW_UP_DUE, S.INTERVIEW, S.REJECTED_BY_COMPANY, S.OFFER, S.WITHDRAWN}
    ),
    S.SUBMISSION_UNVERIFIED: frozenset(
        {S.APPLIED, S.FAILED, S.WITHDRAWN, S.FOLLOW_UP_DUE, S.REJECTED_BY_COMPANY, S.INTERVIEW}
    ),
    S.FAILED: frozenset({S.APPLYING, S.READY, S.FAILED_TERMINAL, S.WITHDRAWN, S.HUMAN_REQUIRED}),
    S.FOLLOW_UP_DUE: frozenset(
        {S.INTERVIEW, S.REJECTED_BY_COMPANY, S.OFFER, S.WITHDRAWN, S.APPLIED}
    ),
    S.INTERVIEW: frozenset({S.OFFER, S.REJECTED_BY_COMPANY, S.WITHDRAWN, S.FOLLOW_UP_DUE}),
    # Terminal.
    S.OFFER: frozenset(),
    S.REJECTED: frozenset({S.SHORTLISTED}),  # a rejection the user changed their mind about
    S.REJECTED_BY_COMPANY: frozenset(),
    S.WITHDRAWN: frozenset(),
    S.FAILED_TERMINAL: frozenset(),
}

TERMINAL: frozenset[S] = frozenset({S.OFFER, S.REJECTED_BY_COMPANY, S.WITHDRAWN, S.FAILED_TERMINAL})

# States that count as "the application reached the company". SUBMISSION_UNVERIFIED
# is excluded on purpose: counting it would overstate the funnel and mask a
# submission that never landed.
SUBMITTED_STATES: frozenset[S] = frozenset(
    {S.APPLIED, S.FOLLOW_UP_DUE, S.INTERVIEW, S.OFFER, S.REJECTED_BY_COMPANY}
)

# A company responded in some direction.
RESPONDED_STATES: frozenset[S] = frozenset({S.INTERVIEW, S.OFFER, S.REJECTED_BY_COMPANY})

# States a human must act on before the agent can continue.
NEEDS_HUMAN: frozenset[S] = frozenset(
    {S.AWAITING_APPROVAL, S.HUMAN_REQUIRED, S.SUBMISSION_UNVERIFIED}
)

# Kanban columns for the tracker, in order.
BOARD_COLUMNS: tuple[tuple[str, tuple[S, ...]], ...] = (
    ("Discovered", (S.DISCOVERED,)),
    ("Shortlisted", (S.SHORTLISTED, S.RESUME_TAILORED)),
    ("Ready", (S.READY, S.AWAITING_APPROVAL, S.HUMAN_REQUIRED, S.APPLYING)),
    ("Applied", (S.APPLIED, S.SUBMISSION_UNVERIFIED, S.FOLLOW_UP_DUE)),
    ("Interview", (S.INTERVIEW,)),
    ("Offer", (S.OFFER,)),
    ("Closed", (S.REJECTED, S.REJECTED_BY_COMPANY, S.WITHDRAWN, S.FAILED, S.FAILED_TERMINAL)),
)

MAX_ATTEMPTS = 3


class IllegalTransition(ValueError):
    """Raised rather than logged: an illegal transition means a caller has a bug
    about what state an application is in, and silently coercing it would make
    the tracker lie."""


@dataclass(frozen=True)
class TransitionResult:
    status: S
    event_type: str
    payload: dict[str, Any]


def can_transition(current: S | str, target: S | str) -> bool:
    current, target = S(current), S(target)
    if current == target:
        return True  # idempotent; re-asserting a state is not an error
    return target in TRANSITIONS.get(current, frozenset())


def transition(
    current: S | str,
    target: S | str,
    *,
    evidence: dict[str, Any] | None = None,
    attempts: int = 0,
    reason: str = "",
) -> TransitionResult:
    """Validate one transition and return the state to write.

    Raises `IllegalTransition` for a move the table does not allow, and for
    APPLIED without evidence. A retry past `MAX_ATTEMPTS` is redirected to
    FAILED_TERMINAL rather than refused, because the caller's intent (stop
    trying) is unambiguous.
    """
    current, target = S(current), S(target)

    if current in TERMINAL and target != current:
        raise IllegalTransition(f"{current.value} is terminal; cannot move to {target.value}")

    if not can_transition(current, target):
        allowed = ", ".join(sorted(s.value for s in TRANSITIONS.get(current, frozenset())))
        raise IllegalTransition(
            f"{current.value} → {target.value} is not a legal transition "
            f"(allowed: {allowed or 'none'})"
        )

    if target is S.APPLIED and not (evidence or {}):
        # The rule that keeps the tracker honest. A click is not a submission.
        raise IllegalTransition(
            "APPLIED requires submission evidence; use SUBMISSION_UNVERIFIED instead"
        )

    if target is S.APPLYING and current is S.FAILED and attempts >= MAX_ATTEMPTS:
        return TransitionResult(
            S.FAILED_TERMINAL,
            "failed_terminal",
            {"reason": f"gave up after {attempts} attempts", "previous": current.value},
        )

    payload: dict[str, Any] = {"from": current.value, "to": target.value}
    if reason:
        payload["reason"] = reason
    if evidence:
        payload["evidence"] = evidence

    return TransitionResult(target, _event_name(target), payload)


def _event_name(status: S) -> str:
    return f"status_{status.value}"


def board_column(status: S | str) -> str:
    status = S(status)
    for name, members in BOARD_COLUMNS:
        if status in members:
            return name
    return "Closed"


def is_open(status: S | str) -> bool:
    """Still in play — neither terminal nor rejected."""
    return S(status) not in TERMINAL and S(status) is not S.REJECTED
