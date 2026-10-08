"""Pre-submit verification, field classification and evidence-bound resolution.

These are the safety functions. They run with no browser, no network and no
database, which is the point: the decision to submit a real application must be
verifiable in a millisecond and never flaky.

The property under test throughout is that the system fails *safe* — holding a
valid application is recoverable; submitting a wrong one is not.
"""

from __future__ import annotations

import pytest

from careeragent.browser.catalog import FieldKind, Sensitivity, classify, is_freeform, never_fill
from careeragent.browser.resolver import Resolution, resolve, resolve_all
from careeragent.browser.verdict import (
    FieldState,
    is_placeholder_wording,
    presubmit_verdict,
    required_field_unfilled,
    safe_to_submit,
    value_matches,
    verdict_from_dicts,
)
from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind


def state(**kwargs) -> FieldState:
    kwargs.setdefault("label", "Field")
    kwargs.setdefault("kind", "text")
    kwargs.setdefault("required", True)
    return FieldState(**kwargs)


# ─── Field classification ───────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("label", "expected"),
    [
        ("First Name *", "contact.first_name"),
        ("Last name", "contact.last_name"),
        ("Email Address", "contact.email"),
        ("Phone number", "contact.phone"),
        ("LinkedIn Profile", "profile.linkedin"),
        ("GitHub URL", "profile.github"),
        ("Resume/CV", "resume.file"),
        ("Cover Letter", "cover_letter.file"),
    ],
)
def test_classify_common_fields(label: str, expected: str) -> None:
    assert classify(label).id == expected


@pytest.mark.parametrize(
    "label",
    [
        "Are you legally authorized to work in the United States?",
        "Will you now or in the future require sponsorship?",
        "Gender",
        "Race / Ethnicity",
        "Are you a protected veteran?",
        "Disability status",
        "Portfolio access code",
        "Have you worked for this company before?",
    ],
)
def test_sensitive_fields_are_never_filled(label: str) -> None:
    """The most important classification rule. A guessed answer to any of these
    is either a legal misrepresentation or offensive — always human."""
    canonical = classify(label)
    assert canonical.sensitivity is Sensitivity.NEVER_FILL, canonical.id
    assert never_fill(canonical.id)


def test_sensitive_patterns_win_over_generic_ones() -> None:
    """'Authorized to work' must not fall through to a generic radio and be
    auto-answered because something else matched first."""
    assert classify("Work authorization status").sensitivity is Sensitivity.NEVER_FILL


def test_unknown_labels_become_stable_freeform_ids() -> None:
    a = classify("Describe a time you shipped something under pressure")
    b = classify("Describe a time you shipped something under pressure")
    c = classify("Why do you want to work here?")
    assert is_freeform(a.id)
    assert a.id == b.id  # stable, so the answer is reusable across applications
    assert a.id != c.id


def test_empty_label_does_not_crash() -> None:
    assert is_freeform(classify("").id)


# ─── Placeholder detection ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    "text",
    ["Select...", "Please select", "-- Choose --", "Choose an option", "Select your country", "--"],
)
def test_placeholder_wording(text: str) -> None:
    assert is_placeholder_wording(text)


@pytest.mark.parametrize(
    "text", ["India", "0-1 years", "Yes", "Bangalore, India", "Select Committee"]
)
def test_real_answers_are_not_placeholders(text: str) -> None:
    assert not is_placeholder_wording(text)


# ─── Required-field verdict ─────────────────────────────────────────────────


def test_filled_text_passes() -> None:
    assert required_field_unfilled(state(kind="text", value="Asha")) is None


def test_empty_text_fails() -> None:
    assert required_field_unfilled(state(kind="text", value="   ")) == "empty"


def test_select_with_disabled_placeholder_fails() -> None:
    """`<option disabled selected>` is the HTML idiom for a placeholder, and it
    is structural — it catches non-English placeholders the regex misses."""
    reason = required_field_unfilled(
        state(kind="select", value="x", selected_label="Choisir une option", selected_disabled=True)
    )
    assert reason and "placeholder" in reason


def test_select_with_sentinel_value_at_index_zero_fails() -> None:
    reason = required_field_unfilled(
        state(kind="select", value="none", selected_label="Pick one", selected_index=0)
    )
    assert reason and "placeholder" in reason


def test_select_with_legitimate_zero_value_passes() -> None:
    """Regression guard: '0' and '1' are real answers ('0-1 years'). Treating an
    index-0 '0' as a placeholder would block valid submissions."""
    assert (
        required_field_unfilled(
            state(kind="select", value="0", selected_label="0-1 years", selected_index=0)
        )
        is None
    )


def test_select_with_real_selection_passes() -> None:
    assert (
        required_field_unfilled(
            state(kind="select", value="in", selected_label="India", selected_index=3)
        )
        is None
    )


def test_combobox_showing_its_own_placeholder_fails() -> None:
    """The readback equals the placeholder attribute: nothing was committed."""
    reason = required_field_unfilled(
        state(kind="combobox", combo_text="Start typing...", placeholder="Start typing...")
    )
    assert reason and "placeholder" in reason


def test_combobox_with_committed_value_passes() -> None:
    assert (
        required_field_unfilled(
            state(kind="combobox", combo_text="Bangalore, India", placeholder="Start typing...")
        )
        is None
    )


def test_unattached_file_fails() -> None:
    assert required_field_unfilled(state(kind="file", file_count=0)) == "no file attached"
    assert required_field_unfilled(state(kind="file", file_count=1)) is None


def test_unchecked_box_fails() -> None:
    assert required_field_unfilled(state(kind="checkbox", checked=False)) == "not checked"


def test_aria_invalid_always_fails() -> None:
    """The page itself says the value is wrong; believe it over our own parse."""
    assert required_field_unfilled(state(kind="text", value="abc", aria_invalid=True))


def test_unknown_control_type_does_not_fabricate_a_failure() -> None:
    """A false hold on a valid application is still a bug."""
    assert required_field_unfilled(state(kind="star-rating-widget", value="")) is None


# ─── Verdict over a whole form ──────────────────────────────────────────────


def test_optional_empty_fields_do_not_block_submission() -> None:
    assert safe_to_submit(
        [
            state(kind="text", value="Asha", required=True),
            state(label="Middle name", kind="text", value="", required=False),
        ]
    )


def test_verdict_lists_every_problem_not_just_the_first() -> None:
    failures = presubmit_verdict(
        [
            state(label="First name", kind="text", value=""),
            state(label="Resume", kind="file", file_count=0),
            state(label="Country", kind="select", value="", selected_label=""),
            state(label="Email", kind="text", value="a@b.com"),
        ]
    )
    assert len(failures) == 3
    assert {f.label for f in failures} == {"First name", "Resume", "Country"}


def test_failure_renders_a_readable_line() -> None:
    failures = presubmit_verdict(
        [state(label="Resume", kind="file", file_count=0, evidence="attribute")]
    )
    rendered = str(failures[0])
    assert "Resume" in rendered and "no file attached" in rendered


def test_verdict_from_dicts_at_the_browser_boundary() -> None:
    assert verdict_from_dicts([{"label": "Name", "kind": "text", "required": True, "value": ""}])
    assert not verdict_from_dicts([])
    assert not verdict_from_dicts(None)  # type: ignore[arg-type]


def test_empty_form_is_trivially_safe() -> None:
    assert safe_to_submit([])


# ─── Write verification ─────────────────────────────────────────────────────


def test_value_matches_accepts_widget_expansion() -> None:
    """Comboboxes expand what you type; that is a match, not a mismatch."""
    assert value_matches("Bangalore", "Bangalore, India")
    assert value_matches("Bangalore, India", "Bangalore")


def test_value_matches_rejects_an_unrelated_commit() -> None:
    """The bug this exists for: typing 'Bangalore' and the widget committing
    'Bangui, Central African Republic'."""
    assert not value_matches("Bangalore", "Bangui, Central African Republic")


def test_value_matches_rejects_empty_readback() -> None:
    assert not value_matches("Bangalore", "")


def test_strict_mode_demands_exact_equality() -> None:
    assert not value_matches("Bangalore", "Bangalore, India", strict=True)
    assert value_matches("Bangalore", "bangalore", strict=True)  # case/space only


# ─── Evidence-bound resolution ──────────────────────────────────────────────


def make_candidate(**kwargs) -> Candidate:
    base = {
        "name": "Asha Raman",
        "email": "asha@example.com",
        "phone": "+91 98765 43210",
        "location": "Bangalore, India",
        "links": {"github": "https://github.com/asha"},
    }
    return Candidate(**{**base, **kwargs})


def test_resolves_contact_fields_from_the_profile() -> None:
    candidate = make_candidate()
    assert resolve("contact.first_name", candidate).value == "Asha"
    assert resolve("contact.last_name", candidate).value == "Raman"
    assert resolve("contact.email", candidate).value == "asha@example.com"
    assert resolve("profile.github", candidate).value == "https://github.com/asha"


def test_missing_profile_field_asks_rather_than_inventing() -> None:
    resolved = resolve("profile.linkedin", make_candidate(links={}))
    assert resolved.resolution is Resolution.NEEDS_USER
    assert not resolved.value


def test_sensitive_fields_resolve_to_never_fill() -> None:
    for field_id in (
        "authorization.sponsorship",
        "demographic.gender",
        "demographic.disability",
        "security.access_code",
    ):
        assert resolve(field_id, make_candidate()).resolution is Resolution.NEVER_FILL


def test_years_of_experience_without_dates_is_never_guessed() -> None:
    """The claim most likely to be fabricated. With no dated experience there is
    no truthful answer, so it must escalate rather than produce a number."""
    resolved = resolve("experience.years_total", make_candidate())
    assert resolved.resolution is Resolution.NEEDS_USER
    assert "invented" in resolved.reason


def test_years_of_experience_is_computed_from_evidence() -> None:
    from datetime import date

    candidate = make_candidate(
        facts=[
            Fact(
                kind=FactKind.EXPERIENCE,
                label="ML Intern",
                evidence_source=EvidenceSource.MASTER_RESUME,
                evidence_locator="cv.pdf#page=1,line=10",
                first_used_at=date(2023, 1, 1),
                last_used_at=date(2025, 1, 1),
            )
        ]
    )
    resolved = resolve("experience.years_total", candidate)
    assert resolved.resolution is Resolution.FILL
    assert resolved.value.startswith("2")
    assert resolved.fact_ids  # traceable back to the evidence


def test_freeform_questions_escalate() -> None:
    canonical = classify("Why do you want to work at Acme?", FieldKind.TEXTAREA)
    assert resolve(canonical.id, make_candidate()).resolution is Resolution.NEEDS_USER


def test_relocation_is_always_confirmed_per_application() -> None:
    """A standing 'yes' to relocation is a commitment the agent must not make
    on the candidate's behalf."""
    assert resolve("preference.relocate", make_candidate()).resolution is Resolution.NEEDS_USER


def test_resolve_all_summarizes_a_form() -> None:
    resolved = resolve_all(
        ["contact.first_name", "contact.email", "demographic.gender", "profile.linkedin"],
        make_candidate(links={}),
        resume_path=None,
    )
    assert resolved["contact.first_name"].fillable
    assert resolved["demographic.gender"].resolution is Resolution.NEVER_FILL
    assert resolved["profile.linkedin"].resolution is Resolution.NEEDS_USER


def test_resume_file_needs_a_selected_resume() -> None:
    candidate = make_candidate()
    assert resolve("resume.file", candidate).resolution is Resolution.NEEDS_USER
    assert resolve("resume.file", candidate, resume_path="/tmp/cv.pdf").fillable
