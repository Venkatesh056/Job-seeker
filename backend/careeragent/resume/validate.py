"""Resume validation: evidence integrity and ATS compatibility.

Pure. No LLM, no database, no network — because this is the function that decides
whether a generated document is allowed to reach a recruiter, and a check that
can fail for an unrelated reason is not a safety mechanism.

Two independent questions, deliberately not blended into one score:

  * **Evidence integrity** — does every claim trace to a fact the candidate
    actually has? A failure here is *blocking*. This is the check that catches
    the model adding "Kubernetes" to a skills list because the job asked for it.
  * **ATS compatibility** — will a parser read this correctly? A failure here is
    a *warning*: an ugly resume is a worse resume, not a dishonest one.

The asymmetry is the point. The system will happily emit a resume that scores 71%
on ATS heuristics. It will not emit one containing a claim it cannot back.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..models.candidate import Candidate, Fact, FactKind
from .documents import TailoredResume
from .taxonomy import ALIAS_TO_CANONICAL, AMBIGUOUS

# Wording that claims seniority or duration the Fact Bank cannot confirm. These
# are the overstatements that survive a naive evidence check: every individual
# token is supported, but the sentence asserts something the facts do not.
# The lookbehind and the optional decimal matter more than they look: without them
# "0.5 years" parses as "5 years" and the deterministic summary — which is honest —
# fails its own validator. Found by the boundary case of a six-month internship.
OVERSTATEMENT = (
    (r"(?<![\d.])(\d+(?:\.\d+)?)\+?\s*years?\b", "claims a duration"),
    (r"\b(expert|expertise|mastery|guru|ninja|world-?class)\b", "claims expert level"),
    (r"\b(led|managed|mentored)\s+a?\s*team\b", "claims team leadership"),
    (r"\b(architected|owned)\s+the\b", "claims sole ownership"),
    (r"\bsenior\b", "claims seniority"),
    (r"\b(certified|accredited)\b", "claims a certification"),
)

# Sections an ATS parser expects to find. Missing ones cost readability, not
# integrity.
EXPECTED_SECTIONS = ("skills", "experience_or_projects", "education")

MAX_BULLET_CHARS = 240
MIN_BULLET_CHARS = 25
MAX_SUMMARY_CHARS = 600
MAX_SKILLS = 40
TARGET_WORDS = (350, 900)


@dataclass
class Violation:
    """A blocking problem: the document must not be used as-is."""

    kind: str
    detail: str
    location: str = ""

    def __str__(self) -> str:
        where = f" [{self.location}]" if self.location else ""
        return f"{self.kind}: {self.detail}{where}"


@dataclass
class Report:
    evidence_integrity: float = 1.0
    ats_score: float = 1.0
    keyword_match: float = 0.0
    violations: list[Violation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    matched_keywords: list[str] = field(default_factory=list)
    missing_keywords: list[str] = field(default_factory=list)
    claims_checked: int = 0

    @property
    def ok(self) -> bool:
        """Safe to render and upload. Warnings do not block; violations do."""
        return not self.violations

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "evidence_integrity": round(self.evidence_integrity, 4),
            "ats_score": round(self.ats_score, 4),
            "keyword_match": round(self.keyword_match, 4),
            "violations": [
                {"kind": v.kind, "detail": v.detail, "location": v.location}
                for v in self.violations
            ],
            "warnings": list(self.warnings),
            "matched_keywords": list(self.matched_keywords),
            "missing_keywords": list(self.missing_keywords),
            "claims_checked": self.claims_checked,
        }


# ─── Evidence ───────────────────────────────────────────────────────────────


def _canonical(term: str) -> str:
    lowered = term.strip().lower()
    return ALIAS_TO_CANONICAL.get(lowered, lowered)


def _fact_index(candidate: Candidate) -> dict[str, Fact]:
    return {str(f.id): f for f in candidate.facts}


def _supported_terms(candidate: Candidate) -> set[str]:
    """Every technology term the candidate can truthfully claim.

    Includes skill labels and aliases, plus terms appearing inside project and
    experience bodies — a tool named in a project description is evidenced by
    that description even when the parser did not promote it to its own skill fact.
    """
    terms: set[str] = set()
    for fact in candidate.facts:
        if fact.kind is FactKind.SKILL:
            terms.add(_canonical(fact.label))
            terms.update(_canonical(a) for a in fact.aliases)
        # Body text is the candidate's own writing; tokens in it are evidenced.
        for token in _technology_tokens(f"{fact.label} {fact.body}"):
            terms.add(token)
    return terms


def _technology_tokens(text: str) -> set[str]:
    """Technology names the taxonomy recognises inside a piece of prose.

    Only recognised names, never arbitrary capitalised words: "Improved" is not a
    technology, and a check that flagged it would be noise the user learns to
    ignore. Ambiguous single letters ("R", "C", "Go") are excluded — the taxonomy
    already marks them, and a false positive here blocks a valid resume.
    """
    lowered = f" {text.lower()} "
    found: set[str] = set()
    for alias, canonical in ALIAS_TO_CANONICAL.items():
        if alias in AMBIGUOUS or len(alias) < 3:
            continue
        # Word-boundary match, tolerating the punctuation resumes actually use.
        if re.search(rf"(?<![\w#+.]){re.escape(alias)}(?![\w#+])", lowered):
            found.add(canonical)
    return found


def check_evidence(resume: TailoredResume, candidate: Candidate) -> tuple[float, list[Violation]]:
    """Every claim against the Fact Bank. Returns (integrity, violations).

    Integrity is the fraction of checked claims that resolved. It is reported
    even when violations exist, so a near-clean document reads differently from
    a wholesale invention.
    """
    facts = _fact_index(candidate)
    supported = _supported_terms(candidate)
    violations: list[Violation] = []
    checked = passed = 0

    # ─── Skills must each be a fact ─────────────────────────────────────────
    for skill in resume.skills:
        checked += 1
        if candidate.supports(skill) or _canonical(skill) in supported:
            passed += 1
        else:
            violations.append(
                Violation("unsupported_skill", f"{skill!r} is not in the Fact Bank", "skills")
            )

    # ─── Every cited fact_id must exist ─────────────────────────────────────
    for fact_id in resume.all_fact_ids():
        checked += 1
        if fact_id in facts:
            passed += 1
        else:
            violations.append(
                Violation("dangling_evidence", f"fact {fact_id} does not exist", "fact_ids")
            )

    # ─── Prose must not introduce technologies ──────────────────────────────
    for bullet in resume.all_bullets():
        if bullet.verbatim:
            continue  # the candidate's own words need no re-derivation
        checked += 1
        introduced = _technology_tokens(bullet.text) - supported
        if introduced:
            violations.append(
                Violation(
                    "fabricated_technology",
                    f"mentions {', '.join(sorted(introduced))} with no evidence",
                    bullet.text[:60],
                )
            )
        elif not bullet.fact_ids:
            violations.append(Violation("unattributed_claim", "no fact cited", bullet.text[:60]))
        else:
            passed += 1

    if resume.summary:
        checked += 1
        introduced = _technology_tokens(resume.summary) - supported
        if introduced:
            violations.append(
                Violation(
                    "fabricated_technology",
                    f"summary mentions {', '.join(sorted(introduced))} with no evidence",
                    "summary",
                )
            )
        else:
            passed += 1

    # ─── Overstatement ──────────────────────────────────────────────────────
    violations.extend(_check_overstatement(resume, candidate))

    # ─── Dates and titles must match their facts ────────────────────────────
    violations.extend(_check_consistency(resume, facts))

    integrity = 1.0 if checked == 0 else round(passed / checked, 4)
    return integrity, violations


def _check_overstatement(resume: TailoredResume, candidate: Candidate) -> list[Violation]:
    """Catch sentences whose individual words are supported but whose claim is not.

    This is the residual risk called out in docs/02-roadmap.md §4.2, made
    mechanical for the cases that can be checked: a stated duration is verified
    against the dated facts, and certification or seniority language is verified
    against the presence of the corresponding fact kind.
    """
    out: list[Violation] = []
    total_years = candidate.total_years_experience()
    has_certification = any(f.kind is FactKind.CERTIFICATION for f in candidate.facts)

    texts = [("summary", resume.summary)] + [
        (b.text[:60], b.text) for b in resume.all_bullets() if not b.verbatim
    ]

    for location, text in texts:
        if not text:
            continue
        for pattern, label in OVERSTATEMENT:
            match = re.search(pattern, text, re.IGNORECASE)
            if not match:
                continue

            if label == "claims a duration":
                claimed = float(match.group(1))
                # A year count above what the dated facts support is the single
                # most consequential fabrication on a resume.
                if total_years is None:
                    out.append(
                        Violation(
                            "unverifiable_duration",
                            f"claims {claimed:g} years; no dated experience in the Fact Bank",
                            location,
                        )
                    )
                elif claimed > total_years + 0.5:
                    out.append(
                        Violation(
                            "overstated_duration",
                            f"claims {claimed:g} years; facts support {total_years:g}",
                            location,
                        )
                    )
                continue

            if label == "claims a certification" and not has_certification:
                out.append(
                    Violation("unsupported_certification", "no certification fact", location)
                )
                continue

            if label in {"claims expert level", "claims team leadership", "claims sole ownership"}:
                # Allowed only if the candidate's own resume text says it.
                if not _phrase_in_facts(match.group(0), candidate):
                    out.append(
                        Violation(
                            "overstatement",
                            f"{label}: {match.group(0)!r} is not in your resume",
                            location,
                        )
                    )
                continue

            if label == "claims seniority" and not _phrase_in_facts("senior", candidate):
                out.append(Violation("overstatement", "claims seniority", location))

    return out


def _phrase_in_facts(phrase: str, candidate: Candidate) -> bool:
    needle = " ".join(phrase.lower().split())
    return any(needle in f"{f.label} {f.body}".lower() for f in candidate.facts)


def _check_consistency(resume: TailoredResume, facts: dict[str, Fact]) -> list[Violation]:
    """Titles and dates on the render must match the facts they cite.

    A tailored resume that renames an employer or shifts a date is a background-
    check failure, and it is trivially detectable, so it is detected.
    """
    out: list[Violation] = []
    for entry in (*resume.experience, *resume.projects, *resume.education):
        cited = [facts[i] for i in entry.fact_ids if i in facts]
        if not cited:
            if entry.fact_ids:
                continue  # already reported as dangling_evidence
            out.append(
                Violation("unattributed_entry", f"{entry.title!r} cites no fact", entry.title[:60])
            )
            continue

        titles = {" ".join(f.label.lower().split()) for f in cited}
        rendered = " ".join(entry.title.lower().split())
        if rendered and not any(rendered in t or t in rendered for t in titles):
            out.append(
                Violation(
                    "title_mismatch",
                    f"{entry.title!r} does not match its fact ({sorted(titles)[0]!r})",
                    entry.title[:60],
                )
            )

        if entry.dates:
            for year in {int(y) for y in re.findall(r"\b(19[89]\d|20[0-4]\d)\b", entry.dates)}:
                if not _year_supported(year, cited):
                    out.append(
                        Violation(
                            "date_mismatch",
                            f"{entry.title!r} shows {year}, which its facts do not support",
                            entry.title[:60],
                        )
                    )
    return out


def _year_supported(year: int, facts: list[Fact]) -> bool:
    for fact in facts:
        start = fact.first_used_at
        end = fact.last_used_at or date.today()
        if start is None:
            # Undated fact: no date claim can be checked, so none may be made
            # from it, but neither can we call it wrong.
            return True
        if start.year <= year <= end.year:
            return True
    return False


# ─── ATS compatibility ──────────────────────────────────────────────────────


def check_ats(
    resume: TailoredResume, job_keywords: list[str] | None = None
) -> tuple[float, list[str]]:
    """Readability heuristics over the plain-text form. Returns (score, warnings).

    Scored on the text an ATS parser actually sees, not the styled PDF: a
    beautiful two-column layout that parses into interleaved nonsense scores
    badly here, which is correct.
    """
    warnings: list[str] = []
    text = resume.plain_text()
    words = len(text.split())
    deductions = 0.0

    if not resume.summary:
        warnings.append("no summary; most parsers use it as the opening context")
        deductions += 0.08
    elif len(resume.summary) > MAX_SUMMARY_CHARS:
        warnings.append(
            f"summary is {len(resume.summary)} characters; keep it under {MAX_SUMMARY_CHARS}"
        )
        deductions += 0.05

    if not resume.skills:
        warnings.append("no skills section; this is the section keyword matchers read first")
        deductions += 0.15
    elif len(resume.skills) > MAX_SKILLS:
        warnings.append(f"{len(resume.skills)} skills listed; over {MAX_SKILLS} reads as padding")
        deductions += 0.05

    if not (resume.experience or resume.projects):
        warnings.append("no experience or projects; there is nothing to assess")
        deductions += 0.25
    if not resume.education:
        warnings.append("no education section")
        deductions += 0.05

    if words < TARGET_WORDS[0]:
        warnings.append(f"{words} words; under {TARGET_WORDS[0]} reads as thin")
        deductions += 0.10
    elif words > TARGET_WORDS[1]:
        warnings.append(f"{words} words; over {TARGET_WORDS[1]} will be skimmed")
        deductions += 0.05

    long_bullets = [b for b in resume.all_bullets() if len(b.text) > MAX_BULLET_CHARS]
    if long_bullets:
        warnings.append(f"{len(long_bullets)} bullet(s) over {MAX_BULLET_CHARS} characters")
        deductions += min(0.10, 0.02 * len(long_bullets))

    thin_bullets = [b for b in resume.all_bullets() if 0 < len(b.text) < MIN_BULLET_CHARS]
    if thin_bullets:
        warnings.append(f"{len(thin_bullets)} bullet(s) under {MIN_BULLET_CHARS} characters")
        deductions += min(0.06, 0.02 * len(thin_bullets))

    seen: set[str] = set()
    duplicates = 0
    for bullet in resume.all_bullets():
        normalized = " ".join(bullet.text.lower().split())
        if normalized and normalized in seen:
            duplicates += 1
        seen.add(normalized)
    if duplicates:
        warnings.append(f"{duplicates} duplicated bullet(s)")
        deductions += min(0.10, 0.03 * duplicates)

    if not resume.contact.get("email"):
        warnings.append("no email address; some parsers discard a resume without one")
        deductions += 0.10

    return (round(max(0.0, 1.0 - deductions), 4), warnings)


def keyword_coverage(
    resume: TailoredResume, job_keywords: list[str]
) -> tuple[float, list[str], list[str]]:
    """How many of the job's keywords appear — truthfully — in the document.

    Missing keywords are reported, never inserted. That list is the honest
    answer to "why might this be screened out", and it feeds gap tracking.
    """
    wanted = list(dict.fromkeys(_canonical(k) for k in job_keywords if k.strip()))
    if not wanted:
        return (0.0, [], [])

    haystack = resume.plain_text().lower()
    present = {_canonical(s) for s in resume.skills}
    present |= _technology_tokens(haystack)

    matched = [k for k in wanted if k in present or k in haystack]
    missing = [k for k in wanted if k not in matched]
    return (round(len(matched) / len(wanted), 4), matched, missing)


def validate(
    resume: TailoredResume,
    candidate: Candidate,
    job_keywords: list[str] | None = None,
) -> Report:
    """The gate. Nothing renders or uploads without `report.ok`."""
    integrity, violations = check_evidence(resume, candidate)
    ats, warnings = check_ats(resume, job_keywords)
    coverage, matched, missing = keyword_coverage(resume, job_keywords or [])

    return Report(
        evidence_integrity=integrity,
        ats_score=ats,
        keyword_match=coverage,
        violations=violations,
        warnings=warnings,
        matched_keywords=matched,
        missing_keywords=missing,
        claims_checked=len(resume.skills) + len(resume.all_bullets()) + len(resume.all_fact_ids()),
    )
