"""Stage-1 deterministic scoring.

Free, pure, explainable — and the gate that decides which jobs are worth spending
LLM tokens on. Per docs/00-research.md §7, cost control belongs in the funnel: a
run over 5,000 jobs should reach the LLM with about 100 of them.

Every score carries its reasons. A ranking a user cannot interrogate is one they
will not trust, and "why was this rejected?" is the first question they ask.
"""

from __future__ import annotations

from dataclasses import dataclass

from rapidfuzz import fuzz

from ..models.candidate import Candidate, SearchConfig
from ..models.job import (
    EmploymentType,
    NormalizedJob,
    Seniority,
    WorkMode,
    _norm,
    normalize_title,
)
from ..models.scoring import DeterministicScore
from ..resume.taxonomy import mentions

# Component weights within stage 1. These are not the final opportunity weights
# (models/scoring.py: ScoreWeights) — these only decide who reaches stage 2.
#
# Role relevance is deliberately NOT in here. It applies as a multiplier below,
# because it is a relevance gate rather than one feature among many: added
# instead, a job matching none of your target roles still reaches the top of the
# list on skill keywords alone. That is precisely the "never apply just because
# it contains a keyword" failure the product is meant to avoid.
WEIGHTS = {
    "skills": 0.533,
    "location": 0.160,
    "seniority": 0.133,
    "work_mode": 0.067,
    "employment_type": 0.067,
    "keywords": 0.040,
}

# Number of distinct matched skills that counts as full marks.
SKILL_SATURATION = 4

# Below this much description text there is nothing to match skills against, so
# the skills component is treated as unassessable rather than as a zero.
MIN_DESCRIPTION_FOR_SKILLS = 120

# A total role mismatch retains this fraction of the score — enough to stay
# visible as an adjacent opportunity, not enough to outrank a real match or to
# clear the LLM gate on its own.
ROLE_MISMATCH_FLOOR = 0.35

# Bands where a senior posting is out of reach rather than merely a stretch.
_JUNIOR_BANDS = frozenset({"internship", "intern", "fresher", "entry", "entry level", "0-1"})

# An explicitly-labelled internship is worth more than an equally-scoring
# permanent role when that is what the candidate is actually looking for.
INTERNSHIP_BONUS = 0.08

# Jobs below this never reach an LLM. Set from JobHunterX's observed gate and
# the funnel maths in docs/01-architecture.md §12.
LLM_GATE = 0.35


@dataclass(frozen=True)
class ScoreContext:
    candidate: Candidate
    config: SearchConfig


def _skill_overlap(candidate: Candidate, job: NormalizedJob) -> tuple[float, list[str], list[str]]:
    """How much of the job's stated requirement surface the candidate covers.

    Matching runs against the description rather than a parsed requirement list,
    because stage 1 runs before extraction — that is the point of a cheap gate.
    """
    # Two different sets, and conflating them was a real scoring bug.
    #
    # `skill_labels()` returns every label AND every alias, which is exactly right for
    # *finding* a skill in a description — a posting says "TF", the Fact Bank says
    # "tensorflow". It is wrong for *counting* one. Matching "python" and "py" used to
    # count as two distinct skills against a saturation of four, so a Fact Bank carrying
    # aliases reached full marks on half as many real skills as one without them, and the
    # matched list shown to the user read "python, sql, tf, py" — four entries for two
    # skills.
    #
    # So: search over every form, then fold each hit back to the canonical label.
    searchable = candidate.skill_labels()
    if not searchable:
        return 0.0, [], []

    canonical_of: dict[str, str] = {}
    for fact in candidate.skills():
        label = fact.label.lower()
        canonical_of[label] = label
        for alias in fact.aliases:
            canonical_of[alias.lower()] = label

    # Word boundaries, not substrings. `s in haystack` scored a description mentioning
    # none of a candidate's skills at full marks: "excellent" contains "excel", "digital"
    # contains "git", "organisational" contains "go", and "r" sits inside most English
    # words. Every job in the database had been scored that way.
    haystack = _norm(f"{job.title} {job.description[:6000]}")
    matched = sorted({canonical_of.get(s, s) for s in searchable if s and mentions(haystack, s)})
    # The candidate's distinct real skills, not their distinct spellings.
    skills = {canonical_of.get(s, s) for s in searchable}

    # Requirement-shaped lines mentioning a skill we lack are the useful gaps.
    missing: list[str] = []
    for line in job.description[:6000].splitlines():
        low = line.lower()
        if len(low) > 200 or not any(
            k in low for k in ("experience with", "proficien", "knowledge of", "familiar")
        ):
            continue
        for token in (
            "pytorch",
            "tensorflow",
            "kubernetes",
            "aws",
            "gcp",
            "azure",
            "spark",
            "scala",
            "java",
            "golang",
            "rust",
            "kafka",
            "airflow",
            "terraform",
        ):
            # Checked against every spelling, so an alias the candidate does have is
            # never reported as a gap.
            if mentions(low, token) and token not in searchable:
                missing.append(token)
    missing = sorted(set(missing))[:8]

    # Measured as absolute evidence of overlap, NOT as a fraction of everything
    # the candidate knows. Dividing by the full skill list punishes a broad
    # candidate: list twenty skills and no single posting can ever mention
    # enough of them, so a perfectly-matched job scores low on skills.
    # Hitting SKILL_SATURATION distinct skills is full marks; a candidate who
    # lists fewer than that only needs all of them.
    denominator = max(min(len(skills), SKILL_SATURATION), 1)
    return min(len(matched) / denominator, 1.0), matched, missing


# Nouns that appear in nearly every job title. Matching on these alone is how
# "Security Engineer" ends up reading as "close to ML Engineer", so they count
# for much less than the tokens that actually name a discipline.
_GENERIC_ROLE_TOKENS = frozenset(
    {
        "engineer",
        "developer",
        "scientist",
        "analyst",
        "specialist",
        "manager",
        "associate",
        "intern",
        "consultant",
        "lead",
        "architect",
        "programmer",
        "officer",
        "administrator",
        "coordinator",
        "director",
        "staff",
    }
)


def _role_match(job: NormalizedJob, config: SearchConfig) -> tuple[float, str]:
    """How well the job title matches one of the target roles.

    Weighted token coverage rather than a plain fuzzy ratio: token_set_ratio
    scores generously on a single shared word, which turns every '… Engineer'
    posting into a near-match for 'ML Engineer'.
    """
    if not config.roles:
        return 0.6, "no target roles configured"

    title_tokens = set(job.normalized_title.split())
    if not title_tokens:
        return 0.0, "job has no usable title"

    best, best_role = 0.0, ""
    for role in config.roles:
        role_normalized = normalize_title(role)
        role_tokens = set(role_normalized.split())
        if not role_tokens:
            continue

        weights = {t: (0.3 if t in _GENERIC_ROLE_TOKENS else 1.0) for t in role_tokens}
        covered = sum(w for token, w in weights.items() if token in title_tokens)
        coverage = covered / sum(weights.values())

        # Keep a fuzzy path for spelling variants the tokenizer misses, but only
        # when it is close to identical.
        fuzzy = fuzz.token_sort_ratio(role_normalized, job.normalized_title) / 100.0
        ratio = max(coverage, fuzzy if fuzzy >= 0.9 else 0.0)

        if ratio > best:
            best, best_role = ratio, role

    if best >= 0.9:
        return 1.0, f"title matches target role '{best_role}'"
    if best >= 0.7:
        return 0.75, f"title close to '{best_role}'"
    if best >= 0.5:
        return 0.4, f"title loosely related to '{best_role}'"

    # Before giving up, ask the family taxonomy. The roles someone types are a sample of
    # what they want, not an exhaustive list of every title an employer might use, and
    # token matching cannot see that "Product Analyst" and "Data Analyst" are neighbouring
    # jobs. Scoring that as a total mismatch multiplies the whole score by the floor: a
    # posting with perfect skills, location and work mode came out at 0.32 because its
    # title used a different noun.
    #
    # The taxonomy already encodes this — 50 families, each with its own `related` list —
    # so this consults what is there rather than inventing a second opinion about which
    # jobs are adjacent.
    family_score, family_reason = _family_match(job, config)
    if family_score:
        return family_score, family_reason

    return 0.0, "title does not match any target role"


def _family_match(job: NormalizedJob, config: SearchConfig) -> tuple[float, str]:
    """Relate a job to the target roles through the job-family taxonomy.

    Deliberately below a direct title match: a role in the same family is good evidence,
    a neighbouring family is weaker evidence, and neither is as strong as the employer
    using the words the candidate asked for.
    """
    from ..careers.families import for_title, get

    job_family = for_title(job.title)
    if job_family is None:
        return 0.0, ""

    for role in config.roles:
        target = for_title(role)
        if target is None:
            continue
        if target.id == job_family.id:
            return 0.75, f"same job family as '{role}' ({job_family.label})"

    # A neighbouring family — the taxonomy's own `related` links, not a guess.
    for role in config.roles:
        target = for_title(role)
        if target is None:
            continue
        if job_family.id in target.related or target.id in job_family.related:
            return 0.5, f"{job_family.label} is adjacent to '{role}'"
        # One hop further out is too far: everything eventually relates to everything.
        _ = get

    return 0.0, ""


def _location_match(job: NormalizedJob, config: SearchConfig) -> tuple[float, str]:
    if job.work_mode is WorkMode.REMOTE and (
        not config.work_modes or "remote" in {w.lower() for w in config.work_modes}
    ):
        return 1.0, "remote role"
    if not config.locations:
        return 0.6, "no location preference set"
    job_location = _norm(job.location)
    if not job_location:
        return 0.4, "job location unknown"
    for preferred in config.locations:
        pref = _norm(preferred)
        if pref and (pref in job_location or job_location in pref):
            return 1.0, f"located in {preferred}"
    return 0.0, f"location '{job.location}' not in preferences"


def _seniority_match(job: NormalizedJob, config: SearchConfig) -> tuple[float, str]:
    band = (config.experience_band or "").lower()
    if not band:
        return 0.6, ""
    wanted = {
        "internship": {Seniority.INTERN},
        "intern": {Seniority.INTERN},
        "fresher": {Seniority.ENTRY, Seniority.INTERN},
        "entry": {Seniority.ENTRY, Seniority.INTERN},
        "entry level": {Seniority.ENTRY, Seniority.INTERN},
        "0-1": {Seniority.ENTRY, Seniority.INTERN},
        "1-2": {Seniority.ENTRY, Seniority.MID},
        "mid": {Seniority.MID},
        "senior": {Seniority.SENIOR, Seniority.LEAD},
    }.get(band, set())

    if not wanted:
        return 0.6, ""
    if job.seniority is Seniority.UNKNOWN:
        return 0.5, "seniority not stated"
    if job.seniority in wanted:
        return 1.0, f"seniority matches ({job.seniority.value})"
    # A senior posting for an entry-level candidate is a waste of an application.
    if job.seniority in {Seniority.SENIOR, Seniority.LEAD} and wanted & {
        Seniority.INTERN,
        Seniority.ENTRY,
    }:
        return 0.0, f"role is {job.seniority.value}, candidate is {band}"
    return 0.3, f"seniority mismatch ({job.seniority.value} vs {band})"


def _hard_fail(job: NormalizedJob, config: SearchConfig) -> str | None:
    """Blocking rules. These reject outright rather than scoring low, because a
    blocked company at 0.9 would still outrank a good match at 0.8."""
    company = _norm(job.company)
    for blocked in config.companies_blocked:
        if _norm(blocked) and _norm(blocked) in company:
            return f"company '{job.company}' is blocked"

    blob = _norm(f"{job.title} {job.description[:4000]}")
    for term in config.keywords_exclude:
        if _norm(term) and _norm(term) in blob:
            return f"contains excluded keyword '{term}'"

    if (
        config.max_job_age_days is not None
        and job.age_days is not None
        and job.age_days > config.max_job_age_days
    ):
        return f"posted {job.age_days} days ago (limit {config.max_job_age_days})"

    if config.employment_types and job.employment_type is not EmploymentType.UNKNOWN:
        wanted = {t.lower().replace(" ", "_") for t in config.employment_types}
        if job.employment_type.value not in wanted:
            return f"employment type {job.employment_type.value} not wanted"

    # A senior or lead posting is not a "low priority" match for someone seeking
    # an internship or a first role — it is unreachable, and surfacing it crowds
    # out the jobs they can actually get. Scoring it low is not enough: a Senior
    # Data Scientist matches the target role perfectly and still outranks a real
    # internship on every other component.
    band = (config.experience_band or "").lower()
    if band in _JUNIOR_BANDS and job.seniority in {Seniority.SENIOR, Seniority.LEAD}:
        return f"{job.seniority.value}-level role, and you are targeting {band}"

    return None


def score_job(job: NormalizedJob, ctx: ScoreContext) -> DeterministicScore:
    """Score one job against the candidate and search config."""
    score = DeterministicScore()

    if reason := _hard_fail(job, ctx.config):
        score.hard_fail = reason
        score.reasons.append(reason)
        score.total = 0.0
        return score

    skills, matched, missing = _skill_overlap(ctx.candidate, job)
    role, role_reason = _role_match(job, ctx.config)
    location, location_reason = _location_match(job, ctx.config)
    seniority, seniority_reason = _seniority_match(job, ctx.config)

    work_mode = 1.0
    if ctx.config.work_modes and job.work_mode is not WorkMode.UNKNOWN:
        work_mode = (
            1.0 if job.work_mode.value in {w.lower() for w in ctx.config.work_modes} else 0.2
        )

    employment = 1.0 if job.employment_type is not EmploymentType.UNKNOWN else 0.6

    blob = _norm(f"{job.title} {job.description[:4000]}")
    wanted_terms = [t for t in ctx.config.keywords_include if _norm(t)]
    keywords = (
        sum(1 for t in wanted_terms if _norm(t) in blob) / len(wanted_terms)
        if wanted_terms
        else 0.5
    )

    preferred_bonus = 0.0
    for preferred in ctx.config.companies_preferred:
        if _norm(preferred) and _norm(preferred) in _norm(job.company):
            preferred_bonus = 0.05
            score.reasons.append(f"preferred company: {job.company}")
            break

    band = (ctx.config.experience_band or "").lower()
    if band in _JUNIOR_BANDS and job.employment_type is EmploymentType.INTERNSHIP:
        preferred_bonus += INTERNSHIP_BONUS
        score.reasons.append("explicitly an internship")

    score.skills = skills
    score.role = role
    score.location = location
    score.seniority = seniority
    score.work_mode = work_mode
    score.employment_type = employment
    score.keywords = keywords
    score.matched_skills = matched
    score.missing_skills = missing

    # Components we cannot assess are dropped and their weight redistributed,
    # rather than scored zero. Several ATS listing endpoints (Workday,
    # SmartRecruiters, BambooHR) return no description at all; scoring their
    # skill match as 0 would push every job from those boards to the bottom on
    # a missing field rather than a poor fit. "No evidence" is not "bad match".
    assessable: dict[str, tuple[float, float]] = {
        "location": (location, WEIGHTS["location"]),
        "seniority": (seniority, WEIGHTS["seniority"]),
        "work_mode": (work_mode, WEIGHTS["work_mode"]),
        "employment_type": (employment, WEIGHTS["employment_type"]),
    }
    if len(job.description) >= MIN_DESCRIPTION_FOR_SKILLS and ctx.candidate.skill_labels():
        assessable["skills"] = (skills, WEIGHTS["skills"])
    if wanted_terms:
        assessable["keywords"] = (keywords, WEIGHTS["keywords"])

    total_weight = sum(w for _, w in assessable.values()) or 1.0
    base = sum(value * weight for value, weight in assessable.values()) / total_weight

    relevance = ROLE_MISMATCH_FLOOR + (1.0 - ROLE_MISMATCH_FLOOR) * role
    score.total = round(min(base * relevance + preferred_bonus, 1.0), 4)

    if matched:
        score.reasons.append(f"matches {len(matched)} of your skills: {', '.join(matched[:6])}")
    if role_reason:
        score.reasons.append(role_reason)
    if location_reason:
        score.reasons.append(location_reason)
    if seniority_reason:
        score.reasons.append(seniority_reason)
    if missing:
        score.reasons.append(f"asks for skills you have not evidenced: {', '.join(missing[:4])}")

    return score


def score_all(
    jobs: list[NormalizedJob], ctx: ScoreContext
) -> list[tuple[NormalizedJob, DeterministicScore]]:
    """Score and rank. Returns (job, score) pairs sorted best first."""
    scored = [(job, score_job(job, ctx)) for job in jobs]
    scored.sort(key=lambda pair: pair[1].total, reverse=True)
    return scored


def passes_llm_gate(score: DeterministicScore, threshold: float = LLM_GATE) -> bool:
    """Only jobs that clear this gate are worth an LLM call."""
    return score.hard_fail is None and score.total >= threshold
