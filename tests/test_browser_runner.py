"""Browser runner against a recorded form, replayed offline.

Real Chromium, real DOM, no network. The fixture is a reduced capture of a
Greenhouse application form including the control types that actually cause
trouble.

The load-bearing assertion is the tripwire: `window.__submitActivations` must
stay at 0 through every test. A suite that could submit a real job application
is not one worth running, and "we were careful" is not a guarantee — this is.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from careeragent.browser.adapters import detect_ats, may_auto_submit
from careeragent.browser.catalog import Sensitivity, classify
from careeragent.browser.runner import BrowserRunner, Outcome
from careeragent.browser.verdict import presubmit_verdict
from careeragent.models.candidate import Candidate

FIXTURE = Path(__file__).parent / "fixtures" / "greenhouse_form.html"
FIXTURE_URL = FIXTURE.as_uri()

pytestmark = pytest.mark.browser


@pytest.fixture
def candidate() -> Candidate:
    return Candidate(
        name="Asha Raman",
        email="asha@example.com",
        phone="+91 98765 43210",
        location="Bangalore, India",
        links={"github": "https://github.com/asha", "linkedin": "https://linkedin.com/in/asha"},
    )


@pytest.fixture
def resume_file(tmp_path: Path) -> str:
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4\n% synthetic fixture resume\n")
    return str(path)


@pytest.fixture
async def runner(tmp_path: Path):
    async with BrowserRunner(headless=True, artifacts_dir=tmp_path / "artifacts") as r:
        yield r


async def _submit_count(page) -> int:
    return await page.evaluate("() => window.__submitActivations || 0")


# ─── Detection and extraction ───────────────────────────────────────────────


def test_detects_greenhouse_from_dom_markers() -> None:
    """An embedded board served from the company's own domain has no telltale
    URL, so detection has to fall back to DOM markers."""
    adapter, confidence = detect_ats(
        "https://acme.com/careers/apply", FIXTURE.read_text(encoding="utf-8")
    )
    assert adapter.name == "greenhouse"
    assert confidence >= 0.5


def test_unknown_system_falls_back_to_generic_and_cannot_auto_submit() -> None:
    adapter, _ = detect_ats(
        "https://acme.com/jobs/apply", "<html><body><form></form></body></html>"
    )
    assert adapter.name == "generic"
    assert not may_auto_submit("generic")


async def test_extracts_every_control(runner) -> None:
    page = await runner._context.new_page()
    await page.goto(FIXTURE_URL)
    spec, _ = await runner.read_form(page, FIXTURE_URL)

    found = {f.canonical_field for f in spec.fields}
    assert "contact.first_name" in found
    assert "contact.email" in found
    assert "resume.file" in found
    assert "profile.github" in found
    assert "authorization.sponsorship" in found

    assert await _submit_count(page) == 0
    await page.close()


async def test_radio_group_is_one_question_not_many(runner) -> None:
    """A five-option radio must not read as five unfilled required fields."""
    page = await runner._context.new_page()
    await page.goto(FIXTURE_URL)
    spec, _ = await runner.read_form(page, FIXTURE_URL)

    sponsorship = [f for f in spec.fields if f.canonical_field == "authorization.sponsorship"]
    assert len(sponsorship) == 1
    assert sponsorship[0].kind == "radio"
    await page.close()


async def test_asterisk_only_required_field_is_detected(runner) -> None:
    """Many frameworks never set `required`; the asterisk is the only signal."""
    page = await runner._context.new_page()
    await page.goto(FIXTURE_URL)
    spec, _ = await runner.read_form(page, FIXTURE_URL)

    why = next(f for f in spec.fields if "Why do you want" in f.label)
    assert why.required
    assert why.evidence == "asterisk"
    await page.close()


# ─── Filling ────────────────────────────────────────────────────────────────


async def test_fills_known_fields_and_verifies_each_write(runner, candidate, resume_file) -> None:
    page = await runner._context.new_page()
    await page.goto(FIXTURE_URL)
    spec, _ = await runner.read_form(page, FIXTURE_URL)
    result = await runner.fill_form(page, spec, candidate, resume_path=resume_file)

    filled = {r.canonical_field: r for r in result.filled}
    assert filled["contact.first_name"].ok
    assert filled["contact.email"].ok
    assert filled["resume.file"].ok

    assert await page.input_value("#first_name") == "Asha"
    assert await page.input_value("#email") == "asha@example.com"
    assert await _submit_count(page) == 0
    await page.close()


async def test_never_fills_sensitive_fields(runner, candidate, resume_file) -> None:
    """The rule that matters most. Demographics and sponsorship stay untouched
    however complete the rest of the form is."""
    page = await runner._context.new_page()
    await page.goto(FIXTURE_URL)
    spec, _ = await runner.read_form(page, FIXTURE_URL)
    await runner.fill_form(page, spec, candidate, resume_path=resume_file)

    assert await page.input_value("#gender") == ""
    assert await page.input_value("#veteran") == ""
    assert await page.input_value("#disability") == ""
    assert await page.locator("input[name=sponsorship]:checked").count() == 0
    assert await _submit_count(page) == 0
    await page.close()


async def test_combobox_expansion_counts_as_a_verified_write(
    runner, candidate, resume_file
) -> None:
    """The widget expands 'Bangalore' to 'Bangalore, Karnataka, India'. That is
    a match, not a mismatch — but it must be read back, not assumed."""
    page = await runner._context.new_page()
    await page.goto(FIXTURE_URL)
    spec, _ = await runner.read_form(page, FIXTURE_URL)
    result = await runner.fill_form(page, spec, candidate, resume_path=resume_file)

    located = [r for r in result.filled if r.canonical_field == "contact.location"]
    assert located, (
        f"location field was never filled; filled: {[r.canonical_field for r in result.filled]}"
    )
    assert located[0].ok
    assert "Bangalore" in await page.input_value("#location")
    await page.close()


# ─── Verdict ────────────────────────────────────────────────────────────────


async def test_unfilled_form_is_held_with_specific_reasons(runner) -> None:
    page = await runner._context.new_page()
    await page.goto(FIXTURE_URL)
    spec, _ = await runner.read_form(page, FIXTURE_URL)

    holds = presubmit_verdict([f.to_state() for f in spec.fields])
    labels = {h.label for h in holds}
    assert "First Name *" in labels
    assert any("Country" in label for label in labels)
    assert any("Resume" in label for label in labels)
    await page.close()


async def test_placeholder_selects_are_held_but_a_real_zero_value_is_not(runner) -> None:
    """'Select...' (disabled) and 'Pick one' (sentinel value) are placeholders.
    '0-1 years' with value="0" at index 0 is a genuine answer."""
    page = await runner._context.new_page()
    await page.goto(FIXTURE_URL)
    spec, _ = await runner.read_form(page, FIXTURE_URL)

    holds = {h.label: h.reason for h in presubmit_verdict([f.to_state() for f in spec.fields])}
    assert any("Country" in label for label in holds)
    assert any("hear about us" in label for label in holds)
    assert not any("Years of experience" in label for label in holds)
    await page.close()


async def test_apply_stops_at_needs_user_and_never_submits(runner, candidate, resume_file) -> None:
    """The default path. Unanswerable required fields mean a human decides, and
    the submit control is never touched."""
    result = await runner.apply(FIXTURE_URL, candidate, resume_path=resume_file, autonomy_level=4)
    assert result.outcome in (Outcome.NEEDS_USER, Outcome.HELD)
    assert not result.submitted
    assert result.never_filled  # the sensitive fields were skipped, and reported


async def test_auto_submit_is_refused_without_autonomy_five(runner, candidate, resume_file) -> None:
    result = await runner.apply(
        FIXTURE_URL, candidate, resume_path=resume_file, autonomy_level=4, auto_submit=True
    )
    assert not result.submitted


async def test_sensitive_fields_are_reported_not_silently_skipped(
    runner, candidate, resume_file
) -> None:
    """Skipping them quietly would leave the user unaware the application is
    incomplete."""
    result = await runner.apply(FIXTURE_URL, candidate, resume_path=resume_file)
    reported = " ".join(result.never_filled).lower()
    assert "gender" in reported or "sponsorship" in reported


# ─── The tripwire ───────────────────────────────────────────────────────────


async def test_full_run_never_activates_submit(runner, candidate, resume_file) -> None:
    """The assertion this whole file exists for.

    Runs the complete flow, then checks the fixture's own counter. If the runner
    ever learns to click submit when it should not, this fails here rather than
    on someone's real application.
    """
    page = await runner._context.new_page()
    await page.goto(FIXTURE_URL)
    spec, _ = await runner.read_form(page, FIXTURE_URL)
    await runner.fill_form(page, spec, candidate, resume_path=resume_file)
    await runner.verify(page, spec)

    assert await _submit_count(page) == 0, "the runner activated the submit control"
    await page.close()


def test_sensitive_catalog_entries_cannot_be_downgraded() -> None:
    """Guard against a future edit accidentally making one of these fillable."""
    for label in (
        "Gender",
        "Veteran Status",
        "Disability Status",
        "Will you now or in the future require visa sponsorship?",
    ):
        assert classify(label).sensitivity is Sensitivity.NEVER_FILL
