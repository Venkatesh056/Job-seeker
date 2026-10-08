"""Normalization, deduplication and stage-1 scoring.

These are the pure functions from docs/01-architecture.md §4 — the reliability
backbone. They run with no network, no database and no LLM, so they can be
exhaustive and fast.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import pytest

from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind, SearchConfig
from careeragent.models.job import (
    EmploymentType,
    RawJob,
    Seniority,
    WorkMode,
    detect_employment_type,
    detect_seniority,
    detect_work_mode,
    extract_years_experience,
    normalize_title,
)
from careeragent.pipeline.dedup import deduplicate, normalize_url
from careeragent.pipeline.normalize import normalize, normalize_all, parse_posted_date, parse_salary
from careeragent.pipeline.score import ScoreContext, passes_llm_gate, score_job


def make_raw(**kwargs) -> RawJob:
    base = {
        "source": "greenhouse",
        "source_job_id": "1",
        "title": "ML Engineer",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "company_name": "Acme",
        "location": "Bangalore, India",
        "description": "Build models with PyTorch.",
    }
    return RawJob(**{**base, **kwargs})


def make_candidate(*skills: str) -> Candidate:
    return Candidate(
        name="Test",
        facts=[
            Fact(
                kind=FactKind.SKILL,
                label=s,
                evidence_source=EvidenceSource.MASTER_RESUME,
                evidence_locator="resume.pdf#page=1",
            )
            for s in skills
        ],
    )


# ─── Title normalization ────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Senior ML Engineer", "machine learning engineer"),
        ("Sr. Machine Learning Engineer (Remote)", "machine learning engineer"),
        ("ML Engineer II", "machine learning engineer"),
        ("AI Engineer", "artificial intelligence engineer"),
        ("NLP Engineer - Speech", "natural language processing engineer speech"),
        ("Data Analyst [Contract]", "data analyst"),
    ],
)
def test_normalize_title(title: str, expected: str) -> None:
    assert normalize_title(title) == expected


def test_normalize_title_converges_synonyms() -> None:
    """The whole point: variants of one role must produce one string, or dedup
    across boards silently fails."""
    assert normalize_title("Senior ML Engineer") == normalize_title("Sr Machine Learning Engineer")


# ─── Field detection ────────────────────────────────────────────────────────


def test_detect_work_mode() -> None:
    assert detect_work_mode("Remote") is WorkMode.REMOTE
    assert detect_work_mode("Hybrid - Bangalore") is WorkMode.HYBRID
    assert detect_work_mode("On-site, Chennai") is WorkMode.ONSITE
    assert detect_work_mode("") is WorkMode.UNKNOWN


def test_detect_work_mode_handles_negation() -> None:
    """'This role is not remote' must not read as remote."""
    assert detect_work_mode("This role is not remote") is WorkMode.ONSITE


def test_detect_employment_type() -> None:
    assert detect_employment_type("", "ML Intern") is EmploymentType.INTERNSHIP
    assert detect_employment_type("Full-time") is EmploymentType.FULL_TIME
    assert detect_employment_type("Contract") is EmploymentType.CONTRACT
    assert detect_employment_type("") is EmploymentType.UNKNOWN


def test_detect_seniority() -> None:
    assert detect_seniority("Senior ML Engineer") is Seniority.SENIOR
    assert detect_seniority("ML Intern") is Seniority.INTERN
    assert detect_seniority("Staff Engineer") is Seniority.LEAD
    assert detect_seniority("Engineer", "We welcome new grad applicants") is Seniority.ENTRY


def test_extract_years_only_from_experience_sentences() -> None:
    assert extract_years_experience("We require 3-5 years of experience.") == (3.0, 5.0)
    # A company blurb mentioning years is not a requirement.
    assert extract_years_experience("We have 12 years of runway.") == (None, None)
    assert extract_years_experience("") == (None, None)


def test_extract_years_ignores_parsed_calendar_years() -> None:
    assert extract_years_experience("Founded 2024 years of experience irrelevant") == (None, None)


# ─── Dates and salary ───────────────────────────────────────────────────────


def test_parse_posted_date_from_iso() -> None:
    assert parse_posted_date(make_raw(published_at="2026-09-01T10:00:00Z")) == date(2026, 9, 1)


def test_parse_posted_date_rejects_future_and_epoch_zero() -> None:
    assert parse_posted_date(make_raw(published_at="1970-01-01T00:00:00Z")) is None
    assert parse_posted_date(make_raw(published_at="2099-01-01T00:00:00Z")) is None


def test_parse_posted_date_from_relative_label() -> None:
    today = datetime.now(UTC).date()
    assert parse_posted_date(make_raw(posted_label="Posted Today")) == today
    assert parse_posted_date(make_raw(posted_label="Posted Yesterday")) == today - timedelta(days=1)
    assert parse_posted_date(make_raw(posted_label="Posted 3 Days Ago")) == today - timedelta(
        days=3
    )


def test_parse_salary_range() -> None:
    salary = parse_salary("₹10,00,000 - ₹20,00,000")
    assert salary is not None and salary.currency == "INR"


def test_parse_salary_keeps_raw_when_unparseable() -> None:
    salary = parse_salary("Competitive")
    assert salary is not None and salary.raw == "Competitive"


# ─── Normalization ──────────────────────────────────────────────────────────


def test_normalize_produces_fingerprint_and_listing() -> None:
    job = normalize(make_raw())
    assert job.fingerprint and job.job_id == job.fingerprint
    assert len(job.listings) == 1
    assert job.normalized_title == "machine learning engineer"


def test_normalize_all_skips_jobs_without_title_or_url() -> None:
    jobs = normalize_all([make_raw(), make_raw(title=""), make_raw(url="")])
    assert len(jobs) == 1


def test_remote_flag_promotes_unknown_work_mode() -> None:
    job = normalize(make_raw(location="", description="", remote=True))
    assert job.work_mode is WorkMode.REMOTE


# ─── Deduplication ──────────────────────────────────────────────────────────


def test_normalize_url_strips_tracking() -> None:
    assert normalize_url("https://www.acme.com/jobs/1?utm_source=x&id=2") == (
        "https://acme.com/jobs/1?id=2"
    )
    assert normalize_url("https://acme.com/jobs/1/") == "https://acme.com/jobs/1"


def test_deduplicate_collapses_same_job_across_sources() -> None:
    """The headline case from docs/01 §5: LinkedIn + Indeed + ATS = one job."""
    jobs = normalize_all(
        [
            make_raw(
                source="greenhouse",
                source_job_id="1",
                url="https://boards.greenhouse.io/acme/jobs/1",
            ),
            make_raw(source="linkedin", source_job_id="L1", url="https://linkedin.com/jobs/1"),
            make_raw(source="indeed", source_job_id="I1", url="https://indeed.com/viewjob?jk=1"),
        ]
    )
    result = deduplicate(jobs)
    assert len(result.jobs) == 1
    assert result.duplicates_removed == 2
    assert len(result.jobs[0].listings) == 3


def test_deduplicate_matches_title_variants() -> None:
    jobs = normalize_all(
        [
            make_raw(title="Senior ML Engineer", source_job_id="1", url="https://a.com/1"),
            make_raw(
                title="Sr. Machine Learning Engineer", source_job_id="2", url="https://b.com/2"
            ),
        ]
    )
    assert len(deduplicate(jobs).jobs) == 1


def test_deduplicate_keeps_genuinely_different_roles() -> None:
    jobs = normalize_all(
        [
            make_raw(title="ML Engineer", source_job_id="1", url="https://a.com/1"),
            make_raw(title="Data Analyst", source_job_id="2", url="https://a.com/2"),
        ]
    )
    assert len(deduplicate(jobs).jobs) == 2


def test_deduplicate_keeps_same_title_at_different_companies() -> None:
    jobs = normalize_all(
        [
            make_raw(company_name="Acme", source_job_id="1", url="https://a.com/1"),
            make_raw(company_name="Globex", source_job_id="2", url="https://b.com/2"),
        ]
    )
    assert len(deduplicate(jobs).jobs) == 2


def test_merge_keeps_longest_description_and_earliest_date() -> None:
    jobs = normalize_all(
        [
            make_raw(
                source_job_id="1",
                url="https://a.com/1",
                description="Short",
                published_at="2026-09-05T00:00:00Z",
            ),
            make_raw(
                source_job_id="2",
                url="https://b.com/2",
                description="A much longer text.",
                published_at="2026-09-01T00:00:00Z",
            ),
        ]
    )
    merged = deduplicate(jobs).jobs[0]
    assert merged.description == "A much longer text."
    assert merged.posted_date == date(2026, 9, 1)


# ─── Scoring ────────────────────────────────────────────────────────────────


def test_score_rewards_skill_and_role_match() -> None:
    job = normalize(make_raw(description="You will use PyTorch and Python daily."))
    ctx = ScoreContext(
        candidate=make_candidate("pytorch", "python"),
        config=SearchConfig(roles=["ML Engineer"], locations=["Bangalore"]),
    )
    score = score_job(job, ctx)
    assert score.hard_fail is None
    assert score.total > 0.8
    assert set(score.matched_skills) == {"python", "pytorch"}
    assert score.reasons


def test_aliases_are_matched_but_counted_once() -> None:
    """An alias must widen what we can *find*, never inflate what we *count*.

    `skill_labels()` returns labels and aliases together, which is right for searching a
    description that says "TF" when the Fact Bank says "tensorflow". It was also being used
    to count distinct matched skills and to size the saturation denominator, so a Fact Bank
    carrying aliases reached full marks on half as many real skills — and the matched list
    shown to the user read "python, sql, tf, py", four entries for two skills.

    Found when a tool-using agent quoted that list back as its evidence for "100% skill
    match" on a job whose real overlap was three skills out of four.
    """

    def skill(label: str, *aliases: str) -> Fact:
        return Fact(
            kind=FactKind.SKILL,
            label=label,
            aliases=list(aliases),
            evidence_source=EvidenceSource.MASTER_RESUME,
            evidence_locator="resume.pdf#page=1",
        )

    # Enough distinct skills that the saturation denominator is SKILL_SATURATION rather
    # than the candidate's own count — which is the case where the alias inflation showed.
    candidate = Candidate(
        name="Test",
        facts=[
            skill("tensorflow", "tf"),
            skill("python", "py"),
            skill("kubernetes"),
            skill("terraform"),
            skill("spark"),
        ],
    )
    job = normalize(make_raw(description="Daily work in Python. Models are trained with TF. " * 4))
    score = score_job(job, ScoreContext(candidate=candidate, config=SearchConfig(roles=[])))

    # Both were found — the alias did its job.
    assert set(score.matched_skills) == {"python", "tensorflow"}
    # Counted once each: two real skills against a saturation of four is a half. Before
    # the fix, "python, py, tensorflow, tf" was four matches and scored full marks.
    assert score.skills == 0.5


def test_an_alias_the_candidate_has_is_not_reported_as_a_gap() -> None:
    """The mirror of the above: `missing` is checked against every spelling."""
    candidate = Candidate(
        name="Test",
        facts=[
            Fact(
                kind=FactKind.SKILL,
                label="tensorflow",
                aliases=["tf"],
                evidence_source=EvidenceSource.MASTER_RESUME,
                evidence_locator="resume.pdf#page=1",
            )
        ],
    )
    job = normalize(make_raw(description="Experience with tensorflow is required. " * 6))
    score = score_job(job, ScoreContext(candidate=candidate, config=SearchConfig(roles=[])))
    assert "tensorflow" not in score.missing_skills


def test_score_penalizes_wrong_location() -> None:
    job = normalize(make_raw(location="Berlin, Germany"))
    ctx = ScoreContext(
        candidate=make_candidate("pytorch"),
        config=SearchConfig(roles=["ML Engineer"], locations=["Bangalore"]),
    )
    assert score_job(job, ctx).location == 0.0


def test_blocked_company_hard_fails_regardless_of_fit() -> None:
    """A blocked company at 0.9 must not outrank a good match at 0.8."""
    job = normalize(make_raw(description="PyTorch Python perfect match"))
    ctx = ScoreContext(
        candidate=make_candidate("pytorch", "python"),
        config=SearchConfig(roles=["ML Engineer"], companies_blocked=["Acme"]),
    )
    score = score_job(job, ctx)
    assert score.hard_fail is not None
    assert score.total == 0.0
    assert not passes_llm_gate(score)


def test_excluded_keyword_hard_fails() -> None:
    job = normalize(make_raw(description="This is an unpaid position."))
    ctx = ScoreContext(
        candidate=make_candidate("pytorch"),
        config=SearchConfig(roles=["ML Engineer"], keywords_exclude=["unpaid"]),
    )
    assert score_job(job, ctx).hard_fail is not None


def test_stale_job_hard_fails() -> None:
    old = (datetime.now(UTC).date() - timedelta(days=90)).isoformat()
    job = normalize(make_raw(published_at=old))
    ctx = ScoreContext(
        candidate=make_candidate("pytorch"),
        config=SearchConfig(roles=["ML Engineer"], max_job_age_days=30),
    )
    assert "90 days ago" in (score_job(job, ctx).hard_fail or "")


def test_senior_role_scores_zero_for_entry_candidate() -> None:
    job = normalize(make_raw(title="Senior ML Engineer"))
    ctx = ScoreContext(
        candidate=make_candidate("pytorch"),
        config=SearchConfig(roles=["ML Engineer"], experience_band="entry"),
    )
    assert score_job(job, ctx).seniority == 0.0


def test_llm_gate_filters_the_funnel() -> None:
    """The gate is the cost control: weak matches must not reach an LLM."""
    good = normalize(make_raw(description="PyTorch Python NLP"))
    weak = normalize(make_raw(title="Warehouse Supervisor", description="Lift boxes."))
    ctx = ScoreContext(
        candidate=make_candidate("pytorch", "python", "nlp"),
        config=SearchConfig(roles=["ML Engineer"], locations=["Bangalore"]),
    )
    assert passes_llm_gate(score_job(good, ctx))
    assert not passes_llm_gate(score_job(weak, ctx))


def test_score_with_no_candidate_skills_does_not_crash() -> None:
    job = normalize(make_raw())
    ctx = ScoreContext(candidate=Candidate(name="Empty"), config=SearchConfig())
    score = score_job(job, ctx)
    assert score.skills == 0.0
    assert 0.0 <= score.total <= 1.0


def test_role_match_rejects_unrelated_engineering_titles() -> None:
    """Regression: token_set_ratio scored 'Security Engineer, Product' as a close
    match for 'ML Engineer' on the shared word 'engineer' alone. Generic title
    nouns must not carry a match by themselves."""
    ctx = ScoreContext(
        candidate=make_candidate("python", "aws"),
        config=SearchConfig(roles=["ML Engineer", "AI Engineer"]),
    )
    unrelated = normalize(make_raw(title="Security Engineer, Product"))
    assert score_job(unrelated, ctx).role == 0.0

    genuine = normalize(make_raw(title="Machine Learning Engineer, Platform"))
    assert score_job(genuine, ctx).role == 1.0


def test_role_match_still_accepts_abbreviated_titles() -> None:
    ctx = ScoreContext(
        candidate=make_candidate("python"),
        config=SearchConfig(roles=["ML Engineer"]),
    )
    assert score_job(normalize(make_raw(title="Sr. ML Engineer")), ctx).role == 1.0


def test_role_mismatch_cannot_outrank_a_real_match_on_skills_alone() -> None:
    """The product rule from the brief: never surface a job just because it
    contains your keywords. A skills-rich but role-irrelevant posting must rank
    below a genuine match and must not clear the LLM gate."""
    ctx = ScoreContext(
        candidate=make_candidate("python", "sql", "aws"),
        config=SearchConfig(roles=["ML Engineer"], locations=["Bangalore"]),
    )
    body = "You will use Python, SQL and AWS every day."
    off_role = normalize(make_raw(title="Solutions Consultant, Enterprise", description=body))
    on_role = normalize(make_raw(title="Machine Learning Engineer", description=body))

    off = score_job(off_role, ctx)
    on = score_job(on_role, ctx)

    assert off.total < on.total
    assert not passes_llm_gate(off)
    assert passes_llm_gate(on)


def test_skill_score_does_not_punish_a_broad_candidate() -> None:
    """Regression: coverage was measured against the candidate's whole skill
    list, so listing more skills lowered every score. A job mentioning four of
    your skills is a strong match whether you listed six or twenty."""
    job = normalize(make_raw(description="We use Python, PyTorch, SQL and Docker."))
    config = SearchConfig(roles=["ML Engineer"], locations=["Bangalore"])

    narrow = score_job(
        job, ScoreContext(make_candidate("python", "pytorch", "sql", "docker"), config)
    )
    broad = score_job(
        job,
        ScoreContext(
            make_candidate(
                "python", "pytorch", "sql", "docker", "nlp", "aws", "kubernetes", "spark"
            ),
            config,
        ),
    )
    assert narrow.skills == 1.0
    assert broad.skills == 1.0


def test_exact_role_match_outranks_a_loose_one() -> None:
    ctx = ScoreContext(
        candidate=make_candidate("python", "pytorch", "sql", "nlp", "docker", "aws"),
        config=SearchConfig(roles=["AI Engineer"], locations=["Remote"]),
    )
    exact = normalize(make_raw(title="Applied AI Engineer", description="", location="Remote"))
    loose = normalize(
        make_raw(
            title="Product Operations, AI Revenue Systems",
            description="Some SQL involved.",
            location="Remote",
        )
    )
    assert score_job(exact, ctx).total > score_job(loose, ctx).total


def test_senior_roles_are_rejected_for_an_internship_search() -> None:
    """Found on a real run: 'Senior Data Scientist' and 'Director Data Science'
    topped the list for a 3rd-year student searching for an internship. A senior
    posting matches the target role perfectly and wins on every other component,
    so scoring it low is not enough — it has to be out."""
    ctx = ScoreContext(
        candidate=make_candidate("python", "sql", "power bi"),
        config=SearchConfig(roles=["Data Scientist"], experience_band="internship"),
    )
    for title in ("Senior Data Scientist", "Director Data Science", "Staff Data Scientist"):
        score = score_job(normalize(make_raw(title=title)), ctx)
        assert score.hard_fail is not None, title
        assert not passes_llm_gate(score)


def test_an_actual_internship_outranks_an_equivalent_permanent_role() -> None:
    ctx = ScoreContext(
        candidate=make_candidate("python", "sql"),
        config=SearchConfig(roles=["Data Scientist"], experience_band="internship"),
    )
    body = "Work with Python and SQL on real datasets."
    internship = normalize(make_raw(title="Data Science Intern", description=body))
    permanent = normalize(make_raw(title="Data Scientist", description=body))
    assert score_job(internship, ctx).total > score_job(permanent, ctx).total


def test_senior_roles_are_fine_when_that_is_what_you_want() -> None:
    ctx = ScoreContext(
        candidate=make_candidate("python"),
        config=SearchConfig(roles=["Data Scientist"], experience_band="senior"),
    )
    assert score_job(normalize(make_raw(title="Senior Data Scientist")), ctx).hard_fail is None


@pytest.mark.parametrize(
    "title",
    [
        "Technical Architect - ML",
        "Associate Technical Architect - ML",
        "Principal Data Scientist",
        "Head of Data Science",
        "VP Engineering",
    ],
)
def test_architect_and_leadership_titles_are_senior(title: str) -> None:
    """Regression: 'Architect' was missing from seniority detection, so
    'Technical Architect - ML' topped an internship search."""
    assert detect_seniority(title) is Seniority.LEAD


def test_architect_roles_are_gated_out_of_an_internship_search() -> None:
    ctx = ScoreContext(
        candidate=make_candidate("python", "sql"),
        config=SearchConfig(roles=["ML Engineer"], experience_band="internship"),
    )
    score = score_job(normalize(make_raw(title="Technical Architect - ML")), ctx)
    assert score.hard_fail is not None
