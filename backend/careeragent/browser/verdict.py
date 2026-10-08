"""Pre-submit verification.

The single most important safety mechanism in the system. JavaScript *gathers*
raw field state from the page; this module *decides* whether that state is safe
to submit. The split is deliberate: the decision is then a pure function over
plain dicts, unit-testable exhaustively with no browser, no network and no
flakiness.

Reimplemented from the approach in `Malik1942/job-agent`, which is unlicensed —
the behaviour is described in docs/00-research.md §6, the code is our own.

The failure it exists to prevent is silent-wrong, not loud-broken: a form that
submits with a placeholder still selected, a combobox that never committed, or a
required file that never attached. Those look like successful applications and
are discovered weeks later, if ever.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Option values used as placeholders in the wild. Deliberately EXCLUDES "0" and
# "1": those are legitimate answers ("0-1 years"), and treating an index-0 "0"
# as a placeholder would block valid submissions.
PLACEHOLDER_VALUES = frozenset(
    {"", "none", "null", "select", "choose", "placeholder", "-1", "undefined", "default"}
)

# A placeholder is a PROMPT, not an answer, and the distinction is narrow:
# "Select" and "Select an option" are prompts, but "Select Committee" is a real
# answer to "Which committee?". A bare prefix match would reject valid
# submissions, so each alternative below anchors on the whole label.
_PLACEHOLDER_WORDING = re.compile(
    r"""
      ^\s*-{2,}\s*$                                             # "--", "-----"
    | ^\s*-*\s*(please\s+)?(select|choose|pick)[\s.*-]*$         # "Select", "Select...", "-- Choose --"
    | ^\s*(please\s+)?(select|choose|pick)\s+(an?|your|one|option|from)\b
    | ^\s*(none|n/?a)\s*$
    """,
    re.IGNORECASE | re.VERBOSE,
)


@dataclass(frozen=True)
class FieldState:
    """Raw state of one control, exactly as gathered from the DOM.

    No decisions are baked in here — this is evidence, and `presubmit_verdict`
    is the judge.
    """

    label: str
    kind: str
    required: bool = False
    value: str = ""
    selected_label: str = ""
    selected_index: int | None = None
    selected_disabled: bool = False
    checked: bool = False
    combo_text: str = ""
    placeholder: str = ""
    file_count: int = 0
    aria_invalid: bool = False
    evidence: str = "none"  # how we know it is required: attribute | aria | asterisk
    canonical_field: str = ""

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FieldState:
        return cls(
            label=str(data.get("label") or ""),
            kind=str(data.get("kind") or ""),
            required=bool(data.get("required")),
            value=str(data.get("value") or ""),
            selected_label=str(data.get("selected_label") or ""),
            selected_index=data.get("selected_index"),
            selected_disabled=bool(data.get("selected_disabled")),
            checked=bool(data.get("checked")),
            combo_text=str(data.get("combo_text") or ""),
            placeholder=str(data.get("placeholder") or ""),
            file_count=int(data.get("file_count") or 0),
            aria_invalid=bool(data.get("aria_invalid")),
            evidence=str(data.get("evidence") or "none"),
            canonical_field=str(data.get("canonical_field") or ""),
        )


@dataclass(frozen=True)
class Failure:
    label: str
    kind: str
    reason: str
    evidence: str = "none"
    canonical_field: str = ""

    def __str__(self) -> str:
        return (
            f"{self.label or '(unlabeled)'} [{self.kind or '?'}, "
            f"required via {self.evidence}] ({self.reason})"
        )


def is_placeholder_wording(text: str) -> bool:
    """Does this option label read as a prompt rather than an answer?"""
    return bool(text) and bool(_PLACEHOLDER_WORDING.match(text.strip()))


def _norm(text: str) -> str:
    return " ".join(text.split()).lower()


def required_field_unfilled(state: FieldState) -> str | None:
    """Is this one required field still unfilled? Returns a short reason, or None.

    Pure: touches nothing, fills nothing. This is the unit-testable core of the
    whole pre-submit invariant.
    """
    if state.aria_invalid:
        return "marked invalid by the page"

    kind = state.kind

    if kind in ("text", "textarea", "email", "phone", "url", "date"):
        return None if state.value.strip() else "empty"

    if kind == "select":
        value = state.value.strip()
        label = state.selected_label.strip()
        if not value:
            return "no option selected"
        # `<option disabled selected>Choose…</option>` is the HTML idiom for a
        # placeholder. Structural, so it catches placeholders whose wording the
        # regex would miss — including non-English ones.
        if state.selected_disabled:
            return f"placeholder option selected ({label!r})"
        if is_placeholder_wording(label):
            return f"placeholder option selected ({label!r})"
        # The first option carrying a sentinel value is a placeholder even when
        # its label is worded oddly ("Pick one", "Choisir") — the class the
        # wording check alone misses. Explicit None check: `0 or -1` would
        # wrongly collapse index 0.
        index = -1 if state.selected_index is None else int(state.selected_index)
        if index == 0 and value.lower() in PLACEHOLDER_VALUES:
            return f"placeholder option selected ({label!r})"
        return None

    if kind == "combobox":
        text = state.combo_text.strip()
        if not text:
            return "empty"
        # Structural backstop: a readback equal to the input's own placeholder
        # attribute means nothing was chosen, whatever the wording says.
        placeholder = _norm(state.placeholder)
        if placeholder and _norm(text) == placeholder:
            return f"placeholder text ({text!r})"
        if is_placeholder_wording(text):
            return f"placeholder text ({text!r})"
        return None

    if kind in ("checkbox", "radio", "yesno"):
        return None if state.checked else "not checked"

    if kind == "file":
        return None if state.file_count > 0 else "no file attached"

    # An unknown control type: do not fabricate a failure for something we
    # cannot reason about — that would be a false hold on a valid application.
    return None


def presubmit_verdict(states: list[FieldState]) -> list[Failure]:
    """Every still-unfilled required field. An empty list means safe to submit."""
    failures: list[Failure] = []
    for state in states:
        if not (state.required or state.aria_invalid):
            continue
        if reason := required_field_unfilled(state):
            failures.append(
                Failure(
                    label=state.label,
                    kind=state.kind,
                    reason=reason,
                    evidence=state.evidence,
                    canonical_field=state.canonical_field,
                )
            )
    return failures


def verdict_from_dicts(raw: list[dict[str, Any]]) -> list[Failure]:
    """Convenience for the browser boundary, where state arrives as plain dicts."""
    return presubmit_verdict([FieldState.from_dict(d) for d in raw or []])


def safe_to_submit(states: list[FieldState]) -> bool:
    return not presubmit_verdict(states)


def value_matches(expected: str, actual: str, strict: bool = False) -> bool:
    """Did what we typed actually land in the control?

    Comboboxes routinely accept a keystroke sequence and commit something else
    entirely. Typing into one and not checking the readback is the top source of
    silently wrong application data, so every write is verified against this.
    """
    expected_norm, actual_norm = _norm(expected), _norm(actual)
    if not expected_norm:
        return True
    if not actual_norm:
        return False
    if strict:
        return expected_norm == actual_norm
    if expected_norm == actual_norm:
        return True
    # A widget commonly expands what was typed or abbreviates it. Substring
    # containment alone is not enough: "Bangalore, India" is NOT a substring of
    # "Bangalore, Karnataka, India", yet they are plainly the same answer. So
    # fall back to token containment, which survives an inserted component.
    if expected_norm in actual_norm or actual_norm in expected_norm:
        return True
    expected_tokens = set(re.findall(r"[a-z0-9]+", expected_norm))
    actual_tokens = set(re.findall(r"[a-z0-9]+", actual_norm))
    if not expected_tokens or not actual_tokens:
        return False
    return expected_tokens <= actual_tokens or actual_tokens <= expected_tokens
