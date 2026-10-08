"""Resume tailoring and the evidence validator.

The adversarial tests are the point of this file. The brief's §20 and §21 ask for a
resume tailored to a job that never invents anything, and the way that fails in
practice is not a wholesale fabrication — it is one plausible extra skill, or a
sentence that quietly implies five years of experience. Each of those has a test here
that fails if the validator stops catching it.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from careeragent.models.candidate import (
    Candidate,
    EvidenceSource,
    Fact,
    FactKind,
)
from careeragent.models.job import NormalizedJob
from careeragent.resume import archetypes as arch
from careeragent.resume.documents import Bullet, TailoredResume
from careeragent.resume.render import NotValidated, to_html, to_text
from careeragent.resume.tailor import tailor, tailor_deterministic
from careeragent.resume.validate import check_ats, keyword_coverage, validate


def fact(kind: FactKind, label: str, body: str = "", **kwargs) -> Fact:
    return Fact(
        kind=kind,
        label=label,
        body=body,
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#page=1,line=1",
        **kwargs,
    )


@pytest.fixture
def candidate() -> Candidate:
    return Candidate(
        name="Asha Raman",
        email="asha@example.com",
        phone="+91 98765 43210",
        location="Bangalore, India",
        links={"github": "https://github.com/asha"},
        facts=[
            fact(FactKind.SKILL, "python"),
            fact(FactKind.SKILL, "pytorch"),
            fact(FactKind.SKILL, "nlp"),
            fact(FactKind.SKILL, "transformers"),
            fact(FactKind.SKILL, "sql"),
            fact(FactKind.SKILL, "power bi"),
            fact(
                FactKind.EXPERIENCE,
                "Machine Learning Intern, Nexora Labs | Jun 2024 - Dec 2024",
                body="Fine-tuned Whisper for Indic speech recognition.\n"
                "Built training pipelines in Python and PyTorch.",
                first_used_at=date(2024, 6, 1),
                last_used_at=date(2024, 12, 1),
            ),
            fact(
                FactKind.PROJECT,
                "Resume Ranker | 2024",
                body="Built an NLP pipeline with Transformers to rank resumes.\n"
                "Served predictions from a Python service.",
                first_used_at=date(2024, 1, 1),
            ),
            fact(
                FactKind.PROJECT,
                "Sales Dashboard | 2023",
                body="Modelled sales data in SQL and built Power BI reports.",
                first_used_at=date(2023, 1, 1),
            ),
            fact(FactKind.EDUCATION, "B.E. Computer Science, PES University | 2021 - 2025"),
            fact(FactKind.CERTIFICATION, "AWS Certified Cloud Practitioner"),
        ],
    )


@pytest.fixture
def nlp_job() -> NormalizedJob:
    return NormalizedJob(
        job_id="j1",
        company="Acme",
        title="NLP Engineer",
        location="Bangalore",
        description="Build speech and language models.",
        requirements=["Python", "PyTorch", "Transformers", "NLP"],
        preferred_skills=["Kubernetes", "AWS"],
    )


@pytest.fixture
def analyst_job() -> NormalizedJob:
    return NormalizedJob(
        job_id="j2",
        company="Globex",
        title="Data Analyst",
        location="Chennai",
        description="SQL, dashboards and reporting.",
        requirements=["SQL", "Power BI", "Excel"],
    )


# ─── Archetype selection ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("NLP Engineer", "nlp_engineer"),
        ("Senior Machine Learning Engineer", "ml_engineer"),
        ("Data Analyst", "data_analyst"),
        ("AI Engineer", "ai_engineer"),
        ("Generative AI Engineer", "genai_engineer"),
        ("Backend Software Engineer", "software_engineer"),
        ("Underwater Basket Weaver", "custom"),
    ],
)
def test_archetype_from_title(title, expected):
    assert arch.for_title(title).id == expected


def test_longest_marker_wins():
    """ "machine learning engineer" must not be captured by a shorter marker."""
    assert arch.for_title("machine learning engineer").id == "ml_engineer"


def test_rank_skills_puts_what_the_job_asked_for_first(candidate):
    ranked = arch.rank_skills(
        ["power bi", "python", "pytorch"], arch.get("nlp_engineer"), ["pytorch"]
    )
    assert ranked[0] == "pytorch"


def test_de_emphasized_skills_sink_but_survive():
    """Dropping a true skill to fit a template is still editing the truth."""
    ranked = arch.rank_skills(["power bi", "pytorch"], arch.get("nlp_engineer"), [])
    assert ranked[-1] == "power bi"
    assert "power bi" in ranked


def test_de_emphasized_skill_resurfaces_when_the_job_asks_for_it():
    ranked = arch.rank_skills(["power bi", "pytorch"], arch.get("nlp_engineer"), ["power bi"])
    assert ranked[0] == "power bi"


# ─── Deterministic tailoring ────────────────────────────────────────────────


def test_deterministic_tailoring_is_evidence_clean(candidate, nlp_job):
    """The baseline promise: with no LLM at all, the document validates."""
    result = tailor(candidate, nlp_job, use_llm=False)
    assert result.report.ok, [str(v) for v in result.report.violations]
    assert result.report.evidence_integrity == 1.0


def test_tailoring_orders_projects_by_relevance(candidate, nlp_job, analyst_job):
    """The same Fact Bank, two jobs, different leading project."""
    nlp_resume = tailor_deterministic(candidate, nlp_job)
    analyst_resume = tailor_deterministic(candidate, analyst_job)
    assert "Resume Ranker" in nlp_resume.projects[0].title
    assert "Sales Dashboard" in analyst_resume.projects[0].title


def test_tailoring_records_gaps_instead_of_claiming_them(candidate, nlp_job):
    """Kubernetes is asked for and unevidenced. It becomes a gap, not a skill."""
    resume = tailor_deterministic(candidate, nlp_job)
    assert "kubernetes" in resume.gaps
    assert "kubernetes" not in [s.lower() for s in resume.skills]


def test_every_bullet_cites_a_fact(candidate, nlp_job):
    resume = tailor_deterministic(candidate, nlp_job)
    assert resume.all_bullets()
    for bullet in resume.all_bullets():
        assert bullet.fact_ids, bullet.text


def test_skills_are_canonical_not_aliases(candidate, nlp_job):
    """Listing both "opencv" and "cv2" reads as padding."""
    resume = tailor_deterministic(candidate, nlp_job)
    assert len(resume.skills) == len(set(resume.skills))
    assert set(resume.skills) <= {f.label for f in candidate.skills()}


def test_wrapped_pdf_lines_are_rejoined(candidate, nlp_job):
    """A PDF wraps one bullet across lines; three fragments is not three bullets."""
    candidate.facts.append(
        fact(
            FactKind.PROJECT,
            "Wrapped Project | 2024",
            body="Built a Python service that ranked documents and improved recall for\n"
            "the search team by restructuring the index.",
            first_used_at=date(2024, 1, 1),
        )
    )
    resume = tailor_deterministic(candidate, nlp_job)
    entry = next(e for e in resume.projects if "Wrapped" in e.title)
    assert len(entry.bullets) == 1
    assert "restructuring the index" in entry.bullets[0].text


def test_summary_never_repeats_the_job_title(candidate):
    """ "AI Engineer (NLP)" in a summary reads as an unevidenced NLP claim."""
    job = NormalizedJob(job_id="j", company="A", title="Kubernetes Platform Engineer")
    resume = tailor_deterministic(candidate, job)
    assert "kubernetes" not in resume.summary.lower()


# ─── The validator, adversarially ───────────────────────────────────────────


def test_validator_catches_an_invented_skill(candidate, nlp_job):
    resume = tailor_deterministic(candidate, nlp_job)
    resume.skills.append("kubernetes")
    report = validate(resume, candidate)
    assert not report.ok
    assert any(v.kind == "unsupported_skill" for v in report.violations)


def test_validator_catches_an_invented_technology_in_prose(candidate, nlp_job):
    resume = tailor_deterministic(candidate, nlp_job)
    resume.experience[0].bullets.append(
        Bullet(text="Deployed models on Kubernetes at scale.", fact_ids=["x"], verbatim=False)
    )
    report = validate(resume, candidate)
    assert any(v.kind == "fabricated_technology" for v in report.violations)


def test_validator_catches_an_overstated_duration(candidate, nlp_job):
    """The most consequential fabrication, and the one an LLM produces most readily."""
    resume = tailor_deterministic(candidate, nlp_job)
    resume.summary = "Engineer with 5 years of experience in machine learning."
    report = validate(resume, candidate)
    assert any(v.kind == "overstated_duration" for v in report.violations)


def test_validator_catches_a_duration_with_no_dated_experience(nlp_job):
    bare = Candidate(name="No Dates", facts=[fact(FactKind.SKILL, "python")])
    resume = TailoredResume(candidate_name="No Dates", skills=["python"])
    resume.summary = "Engineer with 3 years of Python experience."
    report = validate(resume, bare)
    assert any(v.kind == "unverifiable_duration" for v in report.violations)


def test_validator_catches_unsupported_seniority_language(candidate, nlp_job):
    resume = tailor_deterministic(candidate, nlp_job)
    resume.experience[0].bullets.append(
        Bullet(text="Led a team of engineers building models.", fact_ids=["x"], verbatim=False)
    )
    report = validate(resume, candidate)
    assert any(v.kind == "overstatement" for v in report.violations)


def test_validator_catches_an_unsupported_certification_claim(nlp_job):
    bare = Candidate(name="Nobody", facts=[fact(FactKind.SKILL, "python")])
    resume = TailoredResume(
        candidate_name="Nobody",
        skills=["python"],
        summary="A certified Python developer.",
    )
    report = validate(resume, bare)
    assert any(v.kind == "unsupported_certification" for v in report.violations)


def test_validator_catches_a_dangling_fact_id(candidate, nlp_job):
    resume = tailor_deterministic(candidate, nlp_job)
    resume.summary_fact_ids.append("not-a-real-fact")
    report = validate(resume, candidate)
    assert any(v.kind == "dangling_evidence" for v in report.violations)


def test_validator_catches_a_renamed_employer(candidate, nlp_job):
    """A tailored resume that renames an employer is a background-check failure."""
    resume = tailor_deterministic(candidate, nlp_job)
    resume.experience[0].title = "Senior Engineer, Google"
    report = validate(resume, candidate)
    assert any(v.kind == "title_mismatch" for v in report.violations)


def test_validator_catches_a_shifted_date(candidate, nlp_job):
    resume = tailor_deterministic(candidate, nlp_job)
    resume.experience[0].dates = "Jun 2019 - Dec 2019"
    report = validate(resume, candidate)
    assert any(v.kind == "date_mismatch" for v in report.violations)


def test_validator_catches_an_unattributed_bullet(candidate, nlp_job):
    resume = tailor_deterministic(candidate, nlp_job)
    resume.experience[0].bullets.append(
        Bullet(text="Improved the thing substantially for everyone.", verbatim=False)
    )
    report = validate(resume, candidate)
    assert any(v.kind == "unattributed_claim" for v in report.violations)


def test_verbatim_bullets_are_not_re_derived(candidate, nlp_job):
    """The candidate's own words need no evidence check — they are the evidence."""
    resume = tailor_deterministic(candidate, nlp_job)
    for bullet in resume.all_bullets():
        assert bullet.verbatim
    assert validate(resume, candidate).ok


# ─── ATS scoring ────────────────────────────────────────────────────────────


def test_ats_warns_about_a_missing_skills_section(candidate, nlp_job):
    resume = tailor_deterministic(candidate, nlp_job)
    resume.skills = []
    score, warnings = check_ats(resume)
    assert score < 1.0
    assert any("skills" in w for w in warnings)


def test_ats_is_a_warning_not_a_violation(candidate, nlp_job):
    """An ugly resume is a worse resume, not a dishonest one."""
    resume = tailor_deterministic(candidate, nlp_job)
    resume.skills = []
    resume.summary = ""
    report = validate(resume, candidate)
    assert report.ok
    assert report.warnings
    assert report.ats_score < 1.0


def test_keyword_coverage_reports_gaps_without_inserting_them(candidate, nlp_job):
    resume = tailor_deterministic(candidate, nlp_job)
    coverage, matched, missing = keyword_coverage(resume, ["python", "kubernetes"])
    assert "python" in matched
    assert "kubernetes" in missing
    assert coverage == 0.5
    assert "kubernetes" not in to_text(resume).lower()


# ─── Rendering ──────────────────────────────────────────────────────────────


def test_plain_text_round_trips_the_content(candidate, nlp_job):
    resume = tailor_deterministic(candidate, nlp_job)
    text = to_text(resume)
    assert candidate.name in text
    assert "SKILLS" in text
    for bullet in resume.all_bullets()[:3]:
        assert bullet.text in text


def test_html_escapes_content(candidate, nlp_job):
    resume = tailor_deterministic(candidate, nlp_job)
    resume.experience[0].title = "Engineer <script>alert(1)</script>"
    assert "<script>" not in to_html(resume)
    assert "&lt;script&gt;" in to_html(resume)


async def test_render_pdf_refuses_an_invalid_document(candidate, nlp_job, tmp_path: Path):
    """The gate between generation and a file that could be attached anywhere."""
    from careeragent.resume.render import render_pdf

    resume = tailor_deterministic(candidate, nlp_job)
    resume.skills.append("kubernetes")
    report = validate(resume, candidate)
    assert not report.ok
    with pytest.raises(NotValidated):
        await render_pdf(resume, report, tmp_path)


def test_document_round_trips_through_dict(candidate, nlp_job):
    """Renders are stored as JSON; a lossy round trip would corrupt the evidence link."""
    resume = tailor_deterministic(candidate, nlp_job)
    restored = TailoredResume.from_dict(resume.to_dict())
    assert restored.to_dict() == resume.to_dict()
    assert restored.all_fact_ids() == resume.all_fact_ids()


# ─── LLM refinement fallback ────────────────────────────────────────────────


class _Router:
    """A router that returns whatever it is given, so the fallback path is testable
    without a network call."""

    def __init__(self, payload, available=True):
        self.payload = payload
        self._available = available

    def available(self):
        return self._available

    def complete_json(self, *args, **kwargs):
        return self.payload


def test_refinement_is_discarded_when_it_fabricates(candidate, nlp_job):
    """The anti-fabrication mechanism, end to end: a model that overstates duration
    loses, and the user still gets a document rather than an error."""
    router = _Router(
        {
            "summary": "Engineer with 6 years of experience in NLP and Kubernetes.",
            "lines": [{"i": 0, "text": "Deployed models on Kubernetes."}],
        }
    )
    result = tailor(candidate, nlp_job, use_llm=True, router=router)
    assert result.fell_back
    assert result.report.ok
    assert result.resume.generated_by == "deterministic"
    assert any("rejected" in note for note in result.resume.notes)
    assert "kubernetes" not in to_text(result.resume).lower()


def test_faithful_refinement_is_kept(candidate, nlp_job):
    router = _Router(
        {
            "summary": "Machine learning engineer working with Python and PyTorch.",
            "lines": [{"i": 0, "text": "Fine-tuned Whisper for Indic speech recognition."}],
        }
    )
    result = tailor(candidate, nlp_job, use_llm=True, router=router)
    assert not result.fell_back
    assert result.resume.generated_by == "llm"
    assert result.report.ok


def test_no_llm_still_produces_a_document(candidate, nlp_job):
    result = tailor(candidate, nlp_job, use_llm=True, router=_Router(None, available=False))
    assert result.fell_back
    assert result.report.ok
    assert result.resume.skills
