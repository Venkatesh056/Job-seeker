"""What you are looking for, and what it does to the ranking — spec §6.

The scoring machinery always understood experience band, work mode, employment type and
location. Nothing stored the answers, so every caller built an empty `SearchConfig` inline
and a first-year MSc student looking for internships in Bangalore was shown Technical
Architect roles in USA-Remote at 79%.

Measured on the real database once preferences were wired in: 1,403 of 1,592 stored jobs
hard-failed, and the top of the ranking became a Bangalore Product Analyst role instead of a
USA-Remote architect one.

The tests that matter here are the ones asserting every scoring path *reads* the stored
preference. A preference that is saved and then ignored looks exactly like one that works.
"""

from __future__ import annotations

import pytest

from careeragent import preferences
from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind, SearchConfig
from careeragent.models.job import RawJob
from careeragent.pipeline.normalize import company_from_slug, normalize_all
from careeragent.pipeline.score import ScoreContext, score_job


def _skill(label: str) -> Fact:
    return Fact(
        kind=FactKind.SKILL,
        label=label,
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#page=1",
    )


def _job(title: str, **kwargs) -> object:
    defaults = {
        "source": "greenhouse",
        "source_job_id": "1",
        "url": "https://boards.greenhouse.io/acme/jobs/1",
        "company_name": "Acme",
        "location": "Bangalore, India",
        "description": "Python and SQL reporting work. " * 8,
    }
    defaults.update(kwargs)
    return normalize_all([RawJob(title=title, **defaults)])[0]


@pytest.fixture
def candidate() -> Candidate:
    return Candidate(name="T", facts=[_skill("python"), _skill("sql")])


# ─── The vocabulary a form may offer ────────────────────────────────────────


def test_the_offered_bands_are_the_ones_the_scorer_understands():
    """A dropdown offering "graduate" when the scorer recognises "fresher" produces a
    setting that silently does nothing, which is worse than not offering it."""
    from careeragent.pipeline.score import _seniority_match

    for entry in preferences.EXPERIENCE_BANDS:
        config = SearchConfig(experience_band=entry["value"])
        score, _reason = _seniority_match(_job("Data Analyst"), config)
        assert score != 0.6, f"the scorer ignores band {entry['value']!r}"


def test_the_junior_bands_have_not_drifted_from_the_scorer():
    """Two copies of the same list is how one of them silently stops matching."""
    from careeragent.pipeline.score import _JUNIOR_BANDS

    assert preferences.JUNIOR_BANDS == _JUNIOR_BANDS


def test_an_unrecognised_value_is_dropped_rather_than_stored():
    assert preferences._valid(["remote", "telepathic"], preferences.WORK_MODES) == ["remote"]
    assert preferences._valid(None, preferences.WORK_MODES) == []


# ─── What a preference actually does ────────────────────────────────────────


def test_a_junior_band_removes_senior_roles_rather_than_ranking_them_lower(candidate):
    """The consequential rule. A Senior Data Scientist matches a target role perfectly and
    would outrank a real internship on every other component."""
    senior = _job("Senior Data Scientist")
    config = SearchConfig(roles=["Data Scientist"], experience_band="internship")

    scored = score_job(senior, ScoreContext(candidate=candidate, config=config))
    assert scored.total == 0.0
    assert scored.hard_fail
    assert "internship" in scored.hard_fail


def test_without_a_band_the_same_senior_role_survives(candidate):
    """The before picture: this is what was happening for every job in the database."""
    senior = _job("Senior Data Scientist")
    scored = score_job(senior, ScoreContext(candidate=candidate, config=SearchConfig()))
    assert scored.hard_fail is None
    assert scored.total > 0


def test_an_unwanted_employment_type_is_rejected(candidate):
    config = SearchConfig(employment_types=["internship"])
    contract = _job("Data Analyst", employment_type="contract")
    scored = score_job(contract, ScoreContext(candidate=candidate, config=config))
    assert scored.hard_fail


def test_a_location_outside_the_preference_scores_lower(candidate):
    config = SearchConfig(locations=["Bangalore"])
    here = score_job(_job("Data Analyst"), ScoreContext(candidate=candidate, config=config))
    away = score_job(
        _job("Data Analyst", location="Boston, USA"),
        ScoreContext(candidate=candidate, config=config),
    )
    assert here.location > away.location
    assert here.total > away.total


def test_describe_spells_out_the_consequential_rule():
    """Someone who set "internship" and then wondered where all the jobs went deserves to
    have been told."""
    described = preferences.describe(SearchConfig(experience_band="internship"))
    joined = " ".join(described["effects"]).lower()
    assert "rejected outright" in joined
    assert described["is_set"] is True


def test_describe_says_so_when_nothing_is_set():
    described = preferences.describe(SearchConfig())
    assert described["is_set"] is False
    assert "can outrank an internship" in " ".join(described["effects"])


# ─── Role matching through the family taxonomy ──────────────────────────────


def test_a_neighbouring_job_family_is_not_a_total_mismatch(candidate):
    """Measured: "Product Analyst II" against a target of "Data Analyst" scored role 0.0,
    which multiplies the whole score by the mismatch floor. Perfect skills, perfect
    location and perfect work mode came out at 0.32 because the title used a different
    noun. The taxonomy already links data_analytics and business_analysis."""
    from careeragent.pipeline.score import _role_match

    config = SearchConfig(roles=["Data Analyst"])
    score, reason = _role_match(_job("Product Analyst II"), config)

    assert score > 0.0, "a neighbouring family scored as a total mismatch"
    assert "adjacent" in reason or "same job family" in reason


def test_the_same_family_scores_higher_than_a_neighbouring_one():
    from careeragent.pipeline.score import _role_match

    config = SearchConfig(roles=["Business Analyst"])
    same, _ = _role_match(_job("Product Analyst II"), config)
    adjacent, _ = _role_match(_job("Data Analyst"), config)
    assert same > adjacent > 0


def test_a_direct_title_match_still_beats_the_family_fallback():
    """The family route is evidence, not a shortcut past the employer using the words the
    candidate asked for."""
    from careeragent.pipeline.score import _role_match

    config = SearchConfig(roles=["Data Analyst"])
    direct, _ = _role_match(_job("Data Analyst"), config)
    family, _ = _role_match(_job("Product Analyst II"), config)
    assert direct == 1.0
    assert direct > family


def test_an_unrelated_role_is_still_a_mismatch():
    """The fallback must not make everything match everything."""
    from careeragent.pipeline.score import _role_match

    config = SearchConfig(roles=["Data Analyst"])
    score, _ = _role_match(_job("Registered Nurse"), config)
    assert score == 0.0


# ─── Company names ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("slug", "expected"),
    [
        ("quantiphi.wd1.myworkdayjobs.com::Careers_at_Quantiphi", "Quantiphi"),
        ("sprinklr.wd1.myworkdayjobs.com::careers", "Sprinklr"),
        ("browserstack.wd3.myworkdayjobs.com::External", "Browserstack"),
        ("signzy.keka.com", "Signzy"),
        ("glance", "Glance"),
        ("hacker-rank", "Hacker Rank"),
        ("", ""),
    ],
)
def test_a_board_slug_yields_an_employer_name(slug, expected):
    """The fallback used to be the slug itself, which put
    "quantiphi.wd1.myworkdayjobs.com::Careers_at_Quantiphi" in the rankings beside
    "Quantiphi" as though they were two different employers."""
    assert company_from_slug(slug) == expected


def test_an_explicit_company_name_always_wins():
    """The slug is a fallback, never an override."""
    job = _job("Data Analyst", company_name="Real Name Ltd")
    assert job.company == "Real Name Ltd"


# ─── Storage, and every path reading it ─────────────────────────────────────


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setenv("CAREERAGENT_DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'p.db'}")
    monkeypatch.setenv("CAREERAGENT_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("CAREERAGENT_CACHE_DIR", str(tmp_path / "cache"))

    from careeragent import config as config_module
    from careeragent import storage

    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]
    yield tmp_path
    config_module.get_settings.cache_clear()
    storage.db._engine = None  # type: ignore[attr-defined]
    storage.db._session_factory = None  # type: ignore[attr-defined]


async def _seed() -> str:
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo

    await init_db(drop=True)
    model = Candidate(name="T", email="t@example.com", facts=[_skill("python")])
    async with session_scope() as session:
        row = await repo.save_candidate_profile(session, model)
        await repo.replace_facts(session, row.id, model.facts)
        return row.id


async def test_an_unanswered_preference_is_empty_not_an_error(isolated):
    """Nothing breaks before the user has answered anything — that is the old behaviour."""
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    async with session_scope() as session:
        config = await preferences.load(session, candidate_id)
    assert config.experience_band is None
    assert config.roles == []


async def test_a_preference_survives_a_round_trip(isolated):
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    async with session_scope() as session:
        await preferences.save(
            session,
            candidate_id,
            SearchConfig(
                roles=["Data Analyst"],
                locations=["Bangalore"],
                experience_band="entry",
                work_modes=["remote", "hybrid"],
                employment_types=["internship", "full_time"],
            ),
        )
    async with session_scope() as session:
        config = await preferences.load(session, candidate_id)

    assert config.experience_band == "entry"
    assert config.locations == ["Bangalore"]
    assert set(config.work_modes) == {"remote", "hybrid"}


async def test_saving_drops_values_the_scorer_cannot_act_on(isolated):
    """What comes back is what will actually be used."""
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    async with session_scope() as session:
        saved = await preferences.save(
            session,
            candidate_id,
            SearchConfig(
                experience_band="wizard",
                work_modes=["remote", "underwater"],
                employment_types=["full_time", "indentured"],
            ),
        )
    assert saved.experience_band is None
    assert saved.work_modes == ["remote"]
    assert saved.employment_types == ["full_time"]


async def test_an_unreadable_stored_preference_degrades_rather_than_crashing(isolated):
    """A field renamed or a bad hand edit must not stop the system scoring."""
    from sqlalchemy import select

    from careeragent.storage import session_scope
    from careeragent.storage.models import SearchConfigRow

    candidate_id = await _seed()
    async with session_scope() as session:
        session.add(
            SearchConfigRow(candidate_id=candidate_id, config={"max_job_age_days": "not a number"})
        )
    async with session_scope() as session:
        config = await preferences.load(session, candidate_id)
        assert config.experience_band is None
        assert (await session.execute(select(SearchConfigRow))).scalars().first() is not None


def test_no_scoring_path_builds_an_empty_preference_inline():
    """The bug this file exists for. Four call sites constructed `SearchConfig(roles=[])`,
    so nothing the user set was ever consulted."""
    import pathlib

    root = pathlib.Path(__file__).resolve().parents[1] / "backend" / "careeragent"
    offenders = [
        str(path.relative_to(root))
        for path in root.rglob("*.py")
        if path.name not in {"preferences.py"}
        and "SearchConfig(roles=[])" in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"these score against an empty preference: {offenders}"
