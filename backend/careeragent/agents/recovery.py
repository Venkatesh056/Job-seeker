"""Recovery Agent — spec §20, §46.

What existed: a retry counter and a terminal state. `service.py` counted attempts and
gave up at three, which treats every failure as the same failure.

They are not the same. A timeout should be retried immediately. A CAPTCHA should never
be retried and must go to a human. A closed posting should be abandoned and the job
marked expired. A Workday tenant demanding an account is not a failure at all — it is a
route that needs a person. Retrying all four identically wastes three attempts on the
ones that cannot succeed and gives up on the one that would have.

So this classifies first, then prescribes. The classification is pure and table-driven,
which matters because it runs when something has already gone wrong and must not
introduce a second failure of its own.

    Failure  →  classify  →  prescribe  ─┬─ retry (same route, backoff)
                                         ├─ retry via a different route
                                         ├─ escalate to a human
                                         ├─ mark the job expired
                                         └─ give up, terminally

The spec's rule that shapes everything here: **never silently fail.** Every outcome
carries a reason the user can read, and "give up" is an explicit prescription rather
than a loop running out.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Any

from ..logging import get_logger

log = get_logger("agents.recovery")

MAX_ATTEMPTS = 3
# Exponential, in seconds. Short at the start: most transient ATS failures clear in
# under a minute, and a candidate watching a batch should not wait ten.
BACKOFF = (5, 20, 60)


class FailureKind(enum.StrEnum):
    TRANSIENT = "transient"  # timeout, 5xx, connection reset
    RATE_LIMITED = "rate_limited"  # 429, "too many requests"
    HUMAN_REQUIRED = "human_required"  # captcha, mfa, login, identity
    ACCOUNT_REQUIRED = "account_required"  # Workday-style per-tenant signup
    JOB_GONE = "job_gone"  # 404, closed, filled
    FORM_UNREADABLE = "form_unreadable"  # no fields found, JS-only, unknown shape
    FILL_REJECTED = "fill_rejected"  # value would not commit, verdict held
    EVIDENCE_BLOCKED = "evidence_blocked"  # our own validator refused
    BUDGET_EXHAUSTED = "budget_exhausted"  # LLM budget, API quota
    UNKNOWN = "unknown"


class Action(enum.StrEnum):
    RETRY = "retry"
    RETRY_ALTERNATE_ROUTE = "retry_alternate_route"
    ESCALATE_TO_HUMAN = "escalate_to_human"
    MARK_EXPIRED = "mark_expired"
    GIVE_UP = "give_up"


# Ordered: the first pattern that matches wins, so the specific ones come before the
# general. A CAPTCHA message often also contains the word "error".
SIGNATURES: tuple[tuple[FailureKind, str], ...] = (
    (
        FailureKind.HUMAN_REQUIRED,
        r"captcha|recaptcha|hcaptcha|turnstile|cf-challenge|"
        r"two-?factor|2fa|one-?time (code|password)|authenticator|"
        r"verification code|identity verification|sign in to (apply|continue)|"
        r"log ?in to (apply|continue)",
    ),
    (
        FailureKind.ACCOUNT_REQUIRED,
        r"create an account|register to apply|account required|"
        r"please sign up|email verification required",
    ),
    (
        FailureKind.JOB_GONE,
        r"404|410|no longer (accepting|available|open)|"
        r"position (has been )?(filled|closed)|posting has expired|"
        r"job not found|requisition (is )?closed",
    ),
    (FailureKind.RATE_LIMITED, r"\b429\b|too many requests|rate limit|slow down|throttl"),
    (
        FailureKind.BUDGET_EXHAUSTED,
        r"budget|quota (exceeded|exhausted)|insufficient credit|"
        r"billing|payment required|\b402\b",
    ),
    (
        FailureKind.EVIDENCE_BLOCKED,
        r"evidence violation|not in the fact bank|fabricat|"
        r"overstated|unsupported claim|would be invented",
    ),
    (
        FailureKind.FORM_UNREADABLE,
        r"no submit control|form could not be read|no fields|"
        r"no application form|unrecognis\w+ form",
    ),
    (
        FailureKind.FILL_REJECTED,
        r"wanted .* got |did not check|pre-submit verdict|"
        r"is not one of the options|placeholder option",
    ),
    (
        FailureKind.TRANSIENT,
        r"timeout|timed out|connection (reset|refused|closed)|"
        r"\b5\d\d\b|econnreset|temporarily unavailable|"
        r"net::err|target closed|browser (has )?(crashed|closed)",
    ),
)


@dataclass
class Prescription:
    """What to do about one failure, and why."""

    kind: FailureKind
    action: Action
    reason: str
    delay_seconds: int = 0
    alternate_route: str = ""
    user_message: str = ""
    attempt: int = 0

    @property
    def retryable(self) -> bool:
        return self.action in {Action.RETRY, Action.RETRY_ALTERNATE_ROUTE}

    @property
    def terminal(self) -> bool:
        return self.action in {Action.GIVE_UP, Action.MARK_EXPIRED}

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "action": self.action.value,
            "reason": self.reason,
            "delay_seconds": self.delay_seconds,
            "alternate_route": self.alternate_route,
            "user_message": self.user_message,
            "attempt": self.attempt,
            "retryable": self.retryable,
            "terminal": self.terminal,
        }


def classify(error: str, *, status: str = "", blocker: str = "") -> FailureKind:
    """What kind of failure this is. Pure and table-driven.

    `blocker` comes from the browser runner's own detection and is authoritative when
    set — it looked at the live page, which a string match on an error message cannot.
    """
    if blocker in {"captcha", "mfa", "identity", "login"}:
        return FailureKind.HUMAN_REQUIRED

    haystack = " ".join(f"{error} {status}".lower().split())
    if not haystack.strip():
        return FailureKind.UNKNOWN

    for kind, pattern in SIGNATURES:
        if re.search(pattern, haystack):
            return kind
    return FailureKind.UNKNOWN


def prescribe(
    error: str,
    *,
    attempt: int = 0,
    status: str = "",
    blocker: str = "",
    alternate_route: str = "",
) -> Prescription:
    """Decide what happens next.

    `attempt` is how many times this application has already been tried. The cap applies
    only to the kinds where retrying is meaningful — escalating to a human is not a retry
    and does not consume one.
    """
    kind = classify(error, status=status, blocker=blocker)
    delay = BACKOFF[min(attempt, len(BACKOFF) - 1)]

    if kind is FailureKind.HUMAN_REQUIRED:
        return Prescription(
            kind,
            Action.ESCALATE_TO_HUMAN,
            reason="a challenge that must not be automated around",
            user_message=(
                f"{blocker or 'A security challenge'} appeared. Open it yourself with "
                f"Take over — there is no setting that bypasses this."
            ),
            attempt=attempt,
        )

    if kind is FailureKind.ACCOUNT_REQUIRED:
        return Prescription(
            kind,
            Action.ESCALATE_TO_HUMAN,
            reason="this ATS requires creating an account, which is a human step by design",
            user_message=(
                "This employer requires an account before applying. Create one yourself, "
                "then use Take over — automating account creation is out of bounds."
            ),
            attempt=attempt,
        )

    if kind is FailureKind.JOB_GONE:
        return Prescription(
            kind,
            Action.MARK_EXPIRED,
            reason="the posting is closed; retrying cannot succeed",
            user_message="This job is no longer open. Marked expired so it stops being suggested.",
            attempt=attempt,
        )

    if kind is FailureKind.EVIDENCE_BLOCKED:
        return Prescription(
            kind,
            Action.GIVE_UP,
            reason="our own evidence validator refused; retrying would produce the same document",
            user_message=(
                "The tailored application made a claim your Fact Bank cannot support. "
                "Fix the underlying facts rather than retrying."
            ),
            attempt=attempt,
        )

    if kind is FailureKind.BUDGET_EXHAUSTED:
        return Prescription(
            kind,
            Action.GIVE_UP,
            reason="a spend ceiling was hit; retrying spends more of the same budget",
            user_message=(
                "An LLM budget or API quota is exhausted. Raise the ceiling or wait for "
                "it to reset — nothing here is broken."
            ),
            attempt=attempt,
        )

    if kind is FailureKind.FORM_UNREADABLE:
        if alternate_route and attempt < MAX_ATTEMPTS:
            return Prescription(
                kind,
                Action.RETRY_ALTERNATE_ROUTE,
                reason="this door could not be read; another listing offers a different one",
                delay_seconds=2,
                alternate_route=alternate_route,
                user_message="Trying this employer's other application route.",
                attempt=attempt,
            )
        return Prescription(
            kind,
            Action.ESCALATE_TO_HUMAN,
            reason="the form could not be read and there is no other route",
            user_message=(
                "The application page could not be read — it is probably rendered "
                "entirely in JavaScript. Open it with Take over."
            ),
            attempt=attempt,
        )

    if kind is FailureKind.FILL_REJECTED:
        return Prescription(
            kind,
            Action.ESCALATE_TO_HUMAN,
            reason="a value would not commit or the pre-submit verdict held it",
            user_message="A field needs a choice only you can make. Review it in the queue.",
            attempt=attempt,
        )

    if kind in {FailureKind.TRANSIENT, FailureKind.RATE_LIMITED, FailureKind.UNKNOWN}:
        if attempt >= MAX_ATTEMPTS:
            return Prescription(
                kind,
                Action.GIVE_UP,
                reason=f"still failing after {attempt} attempts",
                user_message=(f"Gave up after {attempt} attempts. Last error: {error[:120]}"),
                attempt=attempt,
            )
        # Rate limiting gets a longer wait than a plain timeout: retrying a 429 quickly
        # is how a short throttle becomes a long ban.
        wait = delay * 3 if kind is FailureKind.RATE_LIMITED else delay
        return Prescription(
            kind,
            Action.RETRY,
            reason=(
                "the other side was busy or unreachable"
                if kind is not FailureKind.UNKNOWN
                else "unrecognised failure; one retry is cheaper than giving up"
            ),
            delay_seconds=wait,
            user_message=f"Retrying in {wait}s (attempt {attempt + 1} of {MAX_ATTEMPTS}).",
            attempt=attempt,
        )

    return Prescription(
        FailureKind.UNKNOWN,
        Action.GIVE_UP,
        reason="unclassified failure",
        user_message=f"Stopped: {error[:160]}",
        attempt=attempt,
    )


# ─── Application-status mapping ─────────────────────────────────────────────

# What each action means to the application state machine. Kept here so the prescription
# and the persisted status cannot drift apart.
STATUS_FOR_ACTION = {
    Action.RETRY: "failed",
    Action.RETRY_ALTERNATE_ROUTE: "failed",
    Action.ESCALATE_TO_HUMAN: "human_required",
    # A closed requisition is not a failure of this system, and FAILED_TERMINAL is not
    # reachable from READY anyway — the state machine refuses it, which is how this was
    # found. REJECTED is legal from every state an application can be in when the news
    # arrives, and it is the honest description: we are no longer pursuing this one.
    Action.MARK_EXPIRED: "rejected",
    Action.GIVE_UP: "failed_terminal",
}


def target_status(prescription: Prescription) -> str:
    return STATUS_FOR_ACTION.get(prescription.action, "failed")


@dataclass
class RecoveryLog:
    """Every prescription for one application, so the timeline shows the reasoning."""

    application_id: str
    entries: list[Prescription] = field(default_factory=list)

    def record(self, prescription: Prescription) -> Prescription:
        self.entries.append(prescription)
        log.info(
            "recovery_prescribed",
            application_id=self.application_id,
            kind=prescription.kind.value,
            action=prescription.action.value,
            attempt=prescription.attempt,
        )
        return prescription

    def to_dict(self) -> dict[str, Any]:
        return {
            "application_id": self.application_id,
            "entries": [p.to_dict() for p in self.entries],
        }
