"""Resume tailoring.

Selection first, generation second — and generation is optional.

The deterministic pass alone produces a complete, usable, job-specific resume: it
picks which of the candidate's real experiences and projects are relevant to this
job, orders skills by what the job asked for, and reproduces the candidate's own
bullet text verbatim. It cannot fabricate anything, because it writes nothing.

The LLM pass only *rephrases* lines that already exist and drafts a summary out
of facts it is handed. Its output then goes through the same pure validator as
everything else, and a document with any violation is discarded in favour of the
deterministic one. That ordering is the anti-fabrication mechanism: the fallback
is always a truthful document, never a missing one.

Consequence worth stating plainly: with no LLM key configured, tailoring still
works. It is less fluent and just as honest.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..logging import get_logger
from ..models.candidate import Candidate, Fact, FactKind
from ..models.job import NormalizedJob
from ..prompts import get as get_prompt
from . import archetypes as arch
from .documents import Bullet, Entry, TailoredResume
from .taxonomy import ALIAS_TO_CANONICAL
from .validate import Report, validate

log = get_logger("resume.tailor")

MAX_EXPERIENCE = 4
MAX_PROJECTS = 4
MAX_EDUCATION = 3
MAX_BULLETS_PER_ENTRY = 5
MAX_CERTIFICATIONS = 5
MAX_ACHIEVEMENTS = 4
MIN_BULLET_CHARS = 12


def _canonical(term: str) -> str:
    lowered = term.strip().lower()
    return ALIAS_TO_CANONICAL.get(lowered, lowered)


def job_keywords(job: NormalizedJob) -> list[str]:
    """What this job actually asked for, canonicalized and deduplicated."""
    raw = [*job.requirements, *job.preferred_skills]
    return list(dict.fromkeys(_canonical(k) for k in raw if k and k.strip()))


# ─── Relevance ──────────────────────────────────────────────────────────────


def _fact_relevance(fact: Fact, wanted: set[str], archetype: arch.Archetype) -> float:
    """How much this experience or project speaks to this job.

    Overlap with what the job asked for dominates; archetype priority breaks ties.
    Recency contributes a little, so that two equally relevant roles order the way
    a reader expects.
    """
    text = f"{fact.label} {fact.body}".lower()
    hits = sum(
        1
        for term in wanted
        if term and re.search(rf"(?<![\w#+.]){re.escape(term)}(?![\w#+])", text)
    )
    priority_hits = sum(1 for term in archetype.priority_skills if term in text)

    score = hits * 1.0 + priority_hits * 0.35
    if fact.last_used_at is None and fact.first_used_at is not None:
        score += 0.5  # a current role
    if fact.first_used_at is not None:
        score += min(fact.first_used_at.year - 2015, 6) * 0.05
    return round(score, 4)


def _split_bullets(body: str) -> list[str]:
    """Recover whole bullets from a PDF's wrapped lines.

    The parser preserves the document's line breaks, which is right for
    provenance and wrong for prose: a PDF wraps one bullet across three lines, and
    rendering those as three bullets produces the fragment "...enhance customer"
    followed by "engagement. Integrated an AI chatbot...". A line is treated as a
    continuation when the previous line did not end a sentence and this one does
    not start one.
    """
    raw_lines = [" ".join(line.split()) for line in (body or "").splitlines()]
    merged: list[str] = []
    for text in raw_lines:
        if not text:
            continue
        if merged and _is_continuation(merged[-1], text):
            merged[-1] = f"{merged[-1]} {text}"
        else:
            merged.append(text)
    return [text for text in merged if len(text) >= MIN_BULLET_CHARS]


def _is_continuation(previous: str, current: str) -> bool:
    if previous.endswith((".", "!", "?", ":", ";")):
        return False
    if not current[:1].isalpha():
        return False
    # A new bullet almost always opens with a capitalised verb; a wrapped line
    # almost never does.
    return current[:1].islower() or previous.endswith(("and", "or", "with", "to", "the", "a"))


def _pick_bullets(fact: Fact, wanted: set[str], limit: int) -> list[Bullet]:
    """The candidate's own lines, ordered by relevance to this job.

    `verbatim=True` because these are reproduced exactly. That flag is what tells
    the validator it need not re-derive evidence for wording the candidate wrote.
    """
    lines = _split_bullets(fact.body)

    def relevance(line: str) -> tuple[int, int]:
        lowered = line.lower()
        hits = sum(1 for term in wanted if term and term in lowered)
        return (-hits, len(line))

    chosen = sorted(lines, key=relevance)[:limit]
    # Restore the resume's own order among the chosen lines: a reordered bullet
    # list reads as edited, and the original sequence is usually chronological.
    ordered = [line for line in lines if line in set(chosen)]
    return [Bullet(text=line, fact_ids=[str(fact.id)], verbatim=True) for line in ordered]


def _entry_from(fact: Fact, wanted: set[str], bullets: int = MAX_BULLETS_PER_ENTRY) -> Entry:
    label = " ".join(fact.label.split())
    dates = ""
    if match := re.search(
        r"((?:\w+\.?\s*)?\d{4}\s*(?:[-–—]|to)\s*(?:present|current|now|(?:\w+\.?\s*)?\d{4}))",
        label,
        re.IGNORECASE,
    ):
        dates = match.group(1).strip()
        label = label.replace(match.group(1), "").strip(" ,-–—|")

    title, _, subtitle = label.partition(" — ")
    if not subtitle:
        title, _, subtitle = label.partition(" - ")
    if not subtitle and "," in label:
        title, _, subtitle = label.partition(",")

    return Entry(
        title=title.strip() or label,
        subtitle=subtitle.strip(),
        dates=dates,
        bullets=_pick_bullets(fact, wanted, bullets),
        fact_ids=[str(fact.id)],
    )


def _deterministic_summary(
    candidate: Candidate, job: NormalizedJob, archetype: arch.Archetype, skills: list[str]
) -> tuple[str, list[str]]:
    """A summary assembled only from things already true.

    Names the education, the top matching skills, and — only when the Fact Bank
    has dated roles — the duration. No adjectives about ability, because nothing
    in the Fact Bank evidences one.
    """
    parts: list[str] = []
    fact_ids: list[str] = []

    education = next((f for f in candidate.facts if f.kind is FactKind.EDUCATION), None)
    if education:
        parts.append(_degree_phrase(education.label))
        fact_ids.append(str(education.id))

    years = candidate.total_years_experience()
    if years is not None and years >= 0.5:
        dated = [
            str(f.id) for f in candidate.facts if f.kind is FactKind.EXPERIENCE and f.first_used_at
        ]
        parts.append(f"{years:g} years of professional experience")
        fact_ids.extend(dated)

    top = list(skills[:6])
    if top:
        parts.append("working with " + ", ".join(top))
        for skill in top:
            if fact := candidate.supports(skill):
                fact_ids.append(str(fact.id))

    if not parts:
        return ("", [])

    # Deliberately does NOT name the target role. The job title routinely
    # contains a technology ("AI Engineer (NLP)"), and a summary that repeats it
    # reads to the validator — correctly — as an unevidenced claim about the
    # candidate. The application already says which job it is for.
    return (f"{'. '.join(parts)}.", list(dict.fromkeys(fact_ids)))


def _degree_phrase(label: str) -> str:
    """The degree, without the date range and grade the heading carries.

    An education heading parsed from a resume is "2023 - 2028 MSc. Data Science,
    CIT CGPA: 7.52/10". Dropped into a summary verbatim that reads as scraped
    text, which is what it is.
    """
    text = " ".join(label.split())
    text = re.sub(
        r"^\s*\(?\s*(?:\w+\.?\s*)?\d{4}\s*(?:[-–—]|to)\s*(?:present|current|now|(?:\w+\.?\s*)?\d{4})\s*\)?\s*",
        "",
        text,
        flags=re.IGNORECASE,
    )
    text = re.sub(r"\s*[-–—|,]?\s*(CGPA|GPA|Percentage)\b.*$", "", text, flags=re.IGNORECASE)
    return text.strip(" ,-–—|") or " ".join(label.split())


# ─── Deterministic tailoring ────────────────────────────────────────────────


def tailor_deterministic(
    candidate: Candidate,
    job: NormalizedJob,
    archetype: arch.Archetype | None = None,
) -> TailoredResume:
    """Select, order and reproduce. Writes no new prose about the candidate."""
    archetype = archetype or arch.for_title(job.title)
    wanted_list = job_keywords(job)
    wanted = set(wanted_list)

    # Canonical skill labels only. `skill_labels()` also returns aliases, and a
    # skills section listing both "opencv" and "cv2" reads as padding.
    available = sorted({f.label for f in candidate.skills()})
    skills = arch.rank_skills(available, archetype, wanted_list)

    experience = sorted(
        (f for f in candidate.facts if f.kind is FactKind.EXPERIENCE),
        key=lambda f: _fact_relevance(f, wanted, archetype),
        reverse=True,
    )[:MAX_EXPERIENCE]
    projects = sorted(
        (f for f in candidate.facts if f.kind is FactKind.PROJECT),
        key=lambda f: _fact_relevance(f, wanted, archetype),
        reverse=True,
    )[:MAX_PROJECTS]
    education = [f for f in candidate.facts if f.kind is FactKind.EDUCATION][:MAX_EDUCATION]

    summary, summary_ids = _deterministic_summary(candidate, job, archetype, skills)

    # Skills the job asked for that the candidate cannot evidence. Carried on the
    # document rather than silently added to the skills list.
    gaps = [term for term in wanted_list if not candidate.supports(term) and term not in skills]

    resume = TailoredResume(
        candidate_name=candidate.name,
        archetype=archetype.id,
        job_id=job.job_id,
        job_title=job.title,
        company=job.company,
        contact={
            "email": str(candidate.email or ""),
            "phone": candidate.phone or "",
            "location": candidate.location or "",
            **{k: v for k, v in candidate.links.items() if v},
        },
        summary=summary,
        summary_fact_ids=summary_ids,
        skills=skills,
        experience=[_entry_from(f, wanted) for f in experience],
        projects=[_entry_from(f, wanted) for f in projects],
        education=[_entry_from(f, wanted, bullets=2) for f in education],
        certifications=[
            Bullet(text=" ".join(f.label.split()), fact_ids=[str(f.id)], verbatim=True)
            for f in candidate.facts
            if f.kind is FactKind.CERTIFICATION
        ][:MAX_CERTIFICATIONS],
        achievements=[
            Bullet(text=" ".join(f.label.split()), fact_ids=[str(f.id)], verbatim=True)
            for f in candidate.facts
            if f.kind is FactKind.ACHIEVEMENT
        ][:MAX_ACHIEVEMENTS],
        gaps=gaps,
        generated_by="deterministic",
    )
    return resume


# ─── LLM refinement ─────────────────────────────────────────────────────────

REWRITE = get_prompt("tailor_resume")

MAX_REWRITE_LINES = 24

# One round. A second costs the same again to fix a document the model has already shown it
# cannot fix, and an agent arguing with a validator is not worth paying for.
MAX_REPAIR_ROUNDS = 1

# Room for the reasoning these models do before emitting, plus the summary, plus per line.
# A flat 2400 truncated fifteen bullets mid-array on every real job — the reply parsed as
# nothing, the run fell back to the deterministic document, and the log blamed the model.
# Per rewritten line, on top of the router's reasoning floor. `max_tokens` is a ceiling
# rather than a charge, so the generosity is free when the model finishes early and the
# alternative — a reply cut mid-array, parsing as nothing — costs the whole call.
REWRITE_TOKENS_PER_LINE = 150


def _rewrite_budget(lines: int) -> int:
    """Completion cap for a rewrite of `lines` bullets."""
    from ..llm.router import tokens_for

    return tokens_for(REWRITE_TOKENS_PER_LINE * max(lines, 1))


@dataclass
class TailorResult:
    resume: TailoredResume
    report: Report
    fell_back: bool = False
    fallback_reason: str = ""
    # True when the validator rejected the model's first attempt and accepted its revision.
    # Worth recording separately: it is the difference between a document the model got
    # right and one it was talked into getting right, and the second is the interesting
    # case when reviewing how well the rules are holding.
    repaired: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "resume": self.resume.to_dict(),
            "validation": self.report.to_dict(),
            "fell_back": self.fell_back,
            "fallback_reason": self.fallback_reason,
            "repaired": self.repaired,
        }


def _refine(
    base: TailoredResume, candidate: Candidate, job: NormalizedJob, router: Any
) -> TailoredResume | None:
    """Ask a strong model to rewrite existing lines. Returns None on any failure.

    Only lines the model returned *for an index it was given* are accepted, and
    only if they still cite the original fact. Everything else keeps its original
    text — a partial rewrite is fine; an unmatched one is not.
    """
    from ..llm.router import Tier

    bullets = base.all_bullets()[:MAX_REWRITE_LINES]
    if not bullets:
        return None

    fact_lines = []
    for fact in candidate.facts:
        if fact.kind in {FactKind.EDUCATION, FactKind.EXPERIENCE, FactKind.CERTIFICATION}:
            fact_lines.append(f"- {fact.kind.value}: {' '.join(fact.label.split())}")
    years = candidate.total_years_experience()
    fact_lines.append(
        f"- total professional experience: {years:g} years"
        if years is not None
        else "- total professional experience: NOT STATED (do not mention duration)"
    )
    fact_lines.append(f"- skills: {', '.join(base.skills[:20])}")

    from ..agents.trust import harden

    data = router.complete_json(
        REWRITE.render(
            title=job.title,
            company=job.company,
            keywords=", ".join(job_keywords(job)[:20]) or "not stated",
            facts="\n".join(fact_lines[:30]),
            lines="\n".join(f"[{i}] {b.text}" for i, b in enumerate(bullets)),
        ),
        tier=Tier.STRONG,
        system=harden(REWRITE.system),
        temperature=0.2,
        max_tokens=_rewrite_budget(len(bullets)),
        agent="resume_tailoring",
    )
    if not isinstance(data, dict):
        return None

    from copy import deepcopy

    refined = deepcopy(base)
    refined_bullets = refined.all_bullets()[:MAX_REWRITE_LINES]

    changed = 0
    for entry in data.get("lines") or []:
        if not isinstance(entry, dict):
            continue
        index = entry.get("i")
        text = " ".join(str(entry.get("text") or "").split())
        if not isinstance(index, int) or not 0 <= index < len(refined_bullets) or not text:
            continue
        target = refined_bullets[index]
        if text == target.text:
            continue
        target.text = text
        # No longer the candidate's own words, so the validator must check it.
        target.verbatim = False
        changed += 1

    summary = " ".join(str(data.get("summary") or "").split())
    if summary:
        refined.summary = summary[:600]
        refined.summary_fact_ids = base.summary_fact_ids or [
            str(f.id) for f in candidate.facts if f.kind is FactKind.EDUCATION
        ]

    if not changed and not summary:
        return None

    refined.generated_by = "llm"
    refined.notes.append(f"{changed} line(s) rewritten by the model")
    return refined


def _repair(
    rejected: TailoredResume,
    report: Report,
    base: TailoredResume,
    candidate: Candidate,
    job: NormalizedJob,
    router: Any,
) -> TailoredResume | None:
    """Show the model what the validator refused and ask for a fix. Returns None on failure.

    The violations are handed over verbatim. That is the whole mechanism: "mentions
    kubernetes with no evidence" and "claims 7 years; facts support 0.2" are specific enough
    to act on, and vague feedback would produce a vaguer rewrite.

    The instruction is to *remove* the unsupported claim, never to justify it. A model asked
    to fix an evidence violation will otherwise try to find supporting wording, which is the
    failure mode this whole subsystem exists to prevent.
    """
    from copy import deepcopy

    from ..agents.trust import harden
    from ..llm.router import Tier

    bullets = rejected.all_bullets()[:MAX_REWRITE_LINES]
    if not bullets:
        return None

    complaints = "\n".join(
        f"- {v.kind}: {v.detail}" + (f" [in: {v.location}]" if v.location else "")
        for v in report.violations[:10]
    )

    prompt = (
        f"Your rewrite for {job.title} at {job.company} was rejected by the evidence "
        "validator. These are the exact problems:\n\n"
        f"{complaints}\n\n"
        "Fix them by REMOVING or WEAKENING the unsupported claim. Never try to justify one, "
        "and never substitute a different unsupported claim for it. If a line cannot be "
        "fixed, return the original wording for it.\n\n"
        "SUMMARY YOU WROTE:\n"
        f"{rejected.summary or '(none)'}\n\n"
        "ORIGINAL SUMMARY (safe to fall back to):\n"
        f"{base.summary or '(none)'}\n\n"
        "LINES YOU WROTE:\n"
        + "\n".join(f"[{i}] {b.text}" for i, b in enumerate(bullets))
        + "\n\nReturn JSON:\n"
        '{"summary": string, "lines": [{"i": 0, "text": "corrected line"}]}'
    )

    data = router.complete_json(
        prompt,
        tier=Tier.STRONG,
        system=harden(REWRITE.system),
        temperature=0.0,
        max_tokens=_rewrite_budget(len(bullets)),
        agent="resume_repair",
    )
    if not isinstance(data, dict):
        return None

    fixed = deepcopy(rejected)
    fixed_bullets = fixed.all_bullets()[:MAX_REWRITE_LINES]
    changed = 0
    for entry in data.get("lines") or []:
        if not isinstance(entry, dict):
            continue
        index = entry.get("i")
        text = " ".join(str(entry.get("text") or "").split())
        if not isinstance(index, int) or not 0 <= index < len(fixed_bullets) or not text:
            continue
        if text != fixed_bullets[index].text:
            fixed_bullets[index].text = text
            fixed_bullets[index].verbatim = False
            changed += 1

    summary = " ".join(str(data.get("summary") or "").split())
    if summary and summary != fixed.summary:
        fixed.summary = summary[:600]
        changed += 1

    if not changed:
        return None
    fixed.notes.append(f"{changed} correction(s) after the evidence validator rejected the rewrite")
    return fixed


def tailor(
    candidate: Candidate,
    job: NormalizedJob,
    *,
    archetype: str | arch.Archetype | None = None,
    use_llm: bool = True,
    router: Any = None,
) -> TailorResult:
    """Build a tailored resume for one job, validated before it is returned.

    The returned document is always one the validator passed, or — if even the
    deterministic document has violations, which means the Fact Bank itself is
    inconsistent — the deterministic one with its report attached so the user can
    see what is wrong. It never returns an unvalidated LLM document.
    """
    resolved = (
        archetype
        if isinstance(archetype, arch.Archetype)
        else (arch.get(archetype) if archetype else arch.for_title(job.title))
    )
    if resolved is arch.CUSTOM and not archetype:
        resolved = arch.for_title(job.title)

    keywords = job_keywords(job)
    base = tailor_deterministic(candidate, job, resolved)
    base_report = validate(base, candidate, keywords)

    if not use_llm:
        return TailorResult(base, base_report)

    if router is None:
        from ..llm.router import get_router

        router = get_router()
    if not router.available():
        return TailorResult(base, base_report, fell_back=True, fallback_reason="no LLM configured")

    refined = _refine(base, candidate, job, router)
    if refined is None:
        return TailorResult(
            base, base_report, fell_back=True, fallback_reason="model returned nothing usable"
        )

    report = validate(refined, candidate, keywords)
    if report.ok:
        log.info(
            "resume_tailored",
            job_id=job.job_id,
            archetype=resolved.id,
            ats=report.ats_score,
            keyword_match=report.keyword_match,
        )
        return TailorResult(refined, report)

    # Rejected. Before discarding the whole rewrite — which loses every improvement in it
    # over one bad clause — show the agent exactly what failed and let it answer once. The
    # validator still decides; this only changes what it is deciding about.
    reason = "; ".join(str(v) for v in report.violations[:3])
    log.info(
        "resume_refinement_rejected",
        job_id=job.job_id,
        violations=len(report.violations),
        action="asking the model to correct it",
    )

    for _ in range(MAX_REPAIR_ROUNDS):
        fixed = _repair(refined, report, base, candidate, job, router)
        if fixed is None:
            break
        fixed_report = validate(fixed, candidate, keywords)
        if fixed_report.ok:
            log.info(
                "resume_repair_accepted",
                job_id=job.job_id,
                archetype=resolved.id,
                ats=fixed_report.ats_score,
            )
            return TailorResult(fixed, fixed_report, repaired=True)
        # Still wrong. The second report is the honest one to report on, because it is what
        # the model produced when told precisely what was wrong.
        refined, report = fixed, fixed_report
        reason = "; ".join(str(v) for v in report.violations[:3])

    # The whole point of the fallback: an unfaithful rewrite is discarded, and the user gets
    # the honest document rather than an error.
    log.warning("resume_refinement_discarded", job_id=job.job_id, violations=len(report.violations))
    base.notes.append(f"model rewrite rejected by the evidence validator: {reason}")
    return TailorResult(base, base_report, fell_back=True, fallback_reason=reason)
