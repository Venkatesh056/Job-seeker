"""Company intelligence, analytics honesty, learning proposals, and outreach limits.

The recurring property here is *refusing to assert more than the evidence supports*:
an unresearched company is excluded from a fit score rather than scored zero, a rate on
three applications is withheld rather than rendered, and a learning proposal states its
sample size and is never applied by itself.
"""

from __future__ import annotations

import pytest

from careeragent import company as company_mod
from careeragent.analytics import MIN_SAMPLE, Funnel, Rate
from careeragent.applications import outreach as ox
from careeragent.learning import MIN_OBSERVATIONS, Proposal, confidence_for
from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind


def fact(kind: FactKind, label: str, **kwargs) -> Fact:
    return Fact(
        kind=kind,
        label=label,
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#1",
        **kwargs,
    )


@pytest.fixture
def candidate() -> Candidate:
    return Candidate(
        name="Asha",
        location="Bangalore, India",
        preferred_locations=["Bangalore", "Remote"],
        facts=[
            fact(FactKind.SKILL, "python"),
            fact(FactKind.SKILL, "pytorch"),
            fact(FactKind.SKILL, "nlp"),
            fact(FactKind.SKILL, "transformers"),
            fact(FactKind.SKILL, "docker"),
        ],
    )


# ─── Page reading ───────────────────────────────────────────────────────────


def test_meta_description_is_preferred_over_a_summary():
    """The company's own one-line self-description beats any paraphrase of it."""
    html = '<meta property="og:description" content="We build speech models for Indian languages.">'
    assert company_mod.meta_description(html).startswith("We build speech models")


def test_strip_html_removes_scripts_and_styles():
    html = "<script>var x = 'secret';</script><style>b{}</style><p>Real content here.</p>"
    text = company_mod._strip_html(html)
    assert "Real content here." in text
    assert "secret" not in text


def test_tech_mentioned_is_taxonomy_bounded():
    """Only recognised technology names — a free-text guess invents plausible ones."""
    found = company_mod.tech_mentioned("We use PyTorch, Transformers and Kubernetes daily.")
    assert {"pytorch", "transformers", "kubernetes"} <= set(found)
    assert "daily" not in found


def test_tech_mentioned_ignores_ambiguous_single_letters():
    found = company_mod.tech_mentioned("Our R&D team ships a lot. Go and see.")
    assert "r" not in found
    assert "go" not in found


@pytest.mark.parametrize(
    ("text", "industry"),
    [
        ("We build machine learning infrastructure.", "ai/ml"),
        ("A payments platform for lending.", "fintech"),
        ("Clinical diagnostics for patients.", "healthtech"),
        ("Cyber security and threat detection.", "security"),
    ],
)
def test_industry_classification(text, industry):
    assert company_mod.classify(text)[0] == industry


def test_staffing_language_is_recorded_not_hidden():
    """An early-career candidate is the person most likely to be routed into one."""
    _industry, _size, signals = company_mod.classify(
        "We provide resource augmentation and contract-to-hire placement services."
    )
    assert signals.get("staffing_language")


def test_ai_relevance_scales_with_evidence():
    low = company_mod.ai_relevance([], "We sell office furniture.")
    high = company_mod.ai_relevance(
        ["pytorch", "transformers", "llm", "nlp"],
        "We train deep learning models and machine learning systems.",
    )
    assert low == 0
    assert high > 50


# ─── Company fit ────────────────────────────────────────────────────────────


def test_unresearched_company_scores_zero_with_a_reason(candidate):
    """Zero with zero confidence, which a caller reads as "not assessed" rather than
    "assessed badly" — the distinction the evidence model exists to preserve."""
    profile = company_mod.CompanyProfile(name="Unknown Co")
    result = company_mod.observe_fit(profile, candidate)

    assert result.score == 0.0
    assert result.confidence == 0.0
    assert result.evidence_coverage == 0.0
    assert any("not assessed" in r for r in result.reasons)


def test_fit_rewards_technology_overlap(candidate):
    overlapping = company_mod.CompanyProfile(
        name="A",
        tech_focus=["python", "pytorch", "nlp", "transformers", "docker"],
        ai_relevance=80,
    )
    unrelated = company_mod.CompanyProfile(
        name="B", tech_focus=["php", "wordpress"], ai_relevance=0
    )
    assert (
        company_mod.score_fit(overlapping, candidate)[0]
        > company_mod.score_fit(unrelated, candidate)[0]
    )


def test_fit_explains_itself(candidate):
    profile = company_mod.CompanyProfile(
        name="A", tech_focus=["python", "pytorch"], ai_relevance=60
    )
    _score, reasons = company_mod.score_fit(profile, candidate)
    assert any("technolog" in r for r in reasons)


def test_staffing_language_lowers_the_culture_component(candidate):
    base = company_mod.CompanyProfile(
        name="A", tech_focus=["python"], signals={"engineering_blog": True, "open_source": True}
    )
    shop = company_mod.CompanyProfile(
        name="B",
        tech_focus=["python"],
        signals={"engineering_blog": True, "open_source": True, "staffing_language": "c2c"},
    )
    assert company_mod.score_fit(shop, candidate)[0] < company_mod.score_fit(base, candidate)[0]


def test_notes_for_prompt_is_empty_when_unresearched():
    """The only company detail a generator may use. Empty is the safe default."""
    assert company_mod.CompanyProfile(name="X").notes_for_prompt() == ""


# ─── Rate honesty ───────────────────────────────────────────────────────────


def test_rate_is_withheld_below_the_sample_threshold():
    rate = Rate(numerator=1, denominator=3)
    assert rate.value is None
    assert rate.indicative == pytest.approx(0.3333, abs=1e-4)
    assert not rate.to_dict()["reportable"]


def test_rate_is_reported_at_the_threshold():
    rate = Rate(numerator=2, denominator=MIN_SAMPLE)
    assert rate.value is not None
    assert rate.to_dict()["reportable"]


def test_rate_with_no_observations_is_none_not_zero():
    """Zero would read as "you get no responses" rather than "we do not know"."""
    rate = Rate(numerator=0, denominator=0)
    assert rate.value is None
    assert rate.indicative is None


def test_funnel_conversion_is_relative_to_the_previous_stage():
    funnel = Funnel(discovered=1000, scored=1000, past_gate=100, strong_matches=10)
    stages = {s["stage"]: s for s in funnel.to_dict()["stages"]}
    assert stages["Past relevance gate"]["from_previous"] == pytest.approx(0.1)


# ─── Learning ───────────────────────────────────────────────────────────────


def test_confidence_never_reaches_high():
    """No sample size one job search produces would justify calling it high."""
    assert confidence_for(0) == "none"
    assert confidence_for(MIN_OBSERVATIONS) == "low"
    assert confidence_for(10_000) == "moderate"


def test_a_proposal_with_no_change_is_not_actionable():
    proposal = Proposal(
        kind="ranking_weights",
        observation="nothing",
        sample_size=50,
        confidence="moderate",
        current={"location": 0.05},
    )
    assert not proposal.actionable


def test_proposal_diff_lists_only_what_changed():
    proposal = Proposal(
        kind="ranking_weights",
        observation="x",
        sample_size=30,
        confidence="low",
        current={"location": 0.05, "job_fit": 0.35},
        proposed={"location": 0.08, "job_fit": 0.35},
    )
    diff = proposal.diff()
    assert len(diff) == 1
    assert diff[0]["key"] == "location"
    assert diff[0]["delta"] == pytest.approx(0.03)


# ─── Outreach limits ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "source",
    [
        "careers_page",
        "job_posting",
        "company_contact_page",
        "press_page",
        "user_entered",
        "careers_page:https://acme.com/careers",
    ],
)
def test_permitted_contact_sources(source):
    assert ox.PERMITTED_SOURCES.match(source)


@pytest.mark.parametrize(
    "source",
    ["", "guessed", "linkedin_scrape", "email_pattern", "enrichment_vendor", "inferred"],
)
def test_contact_sources_that_are_refused(source):
    """A contact with no recorded provenance cannot have come from a published page."""
    assert not ox.PERMITTED_SOURCES.match(source)


def test_outreach_limits_are_small_by_design():
    """Outreach that scales is spam. These numbers are what personalized can sustain."""
    assert ox.MAX_PER_DAY <= 10
    assert ox.MAX_PER_WEEK <= 25
    assert ox.MIN_DAYS_BETWEEN_SAME_COMPANY >= 14


def test_linkedin_channel_is_draft_only():
    """LinkedIn prohibits automated messaging; an account ban costs the user their network."""
    assert "linkedin_draft" in ox.CHANNELS
    assert "linkedin" not in ox.CHANNELS


# ─── Unreadable sites ───────────────────────────────────────────────────────


def test_an_unreadable_site_yields_no_ai_relevance(candidate):
    """ "We could not read the site" must not score the same as "it does no AI work".

    A JavaScript-only page returns a near-empty shell. Reporting that as 0/100 AI
    relevance is the same silent-wrong failure as a wrong ATS slug returning an empty
    list and looking like "no openings".
    """
    profile = company_mod.CompanyProfile(
        name="Shell Co",
        readable_chars=40,
        signals={"read_quality": "uninformative", "readable_chars": 40},
    )
    score, reasons = company_mod.score_fit(profile, candidate)
    assert profile.ai_relevance is None
    assert score == 0.0
    assert "not assessed" in reasons[0]


def test_uninformative_signals_are_excluded_rather_than_scored_zero(candidate):
    """Absent signals from an unreadable page are unknown, not negative.

    The consequence is deliberate and worth stating: excluding a component and
    redistributing its weight can *raise* the score relative to a company whose
    signals were read and found weak. That is the correct direction — an
    unresearched company must not rank below a researched bad one — and the test
    pins it so nobody later "fixes" it into a penalty.
    """
    no_signals = company_mod.CompanyProfile(name="A", tech_focus=["python"], ai_relevance=50)
    weak_signals = company_mod.CompanyProfile(
        name="B", tech_focus=["python"], ai_relevance=50, signals={"engineering_blog": False}
    )
    unreadable = company_mod.CompanyProfile(
        name="C",
        tech_focus=["python"],
        ai_relevance=50,
        signals={"read_quality": "uninformative"},
    )
    unscored, _ = company_mod.score_fit(no_signals, candidate)
    weak, _ = company_mod.score_fit(weak_signals, candidate)
    unread, _ = company_mod.score_fit(unreadable, candidate)

    assert unread == pytest.approx(unscored), "an unreadable page contributes nothing"
    assert unread > weak, "a read-and-weak signal is evidence; an unread one is not"
