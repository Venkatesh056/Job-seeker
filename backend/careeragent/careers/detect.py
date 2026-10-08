"""Detect a candidate's career field from their Fact Bank.

The question this answers is "what does this person actually do?", asked of a resume
the system has never seen the shape of before — a civil engineer, a pharmacist, a
backend developer. The answer decides which job families get searched, which is the
difference between a useful campaign and a page of irrelevant listings.

Deliberately not an LLM call. Three reasons, in order of how much they matter:

  1. It runs before anything else and must work with no key configured.
  2. A model asked "what field is this person in?" will answer confidently for a
     resume that genuinely spans two, and the honest output here is *both, ranked*.
  3. It is scored against the same skill vocabulary the rest of the system uses, so a
     detection can be explained line by line: "civil_engineering, because autocad,
     staad pro and surveying appear in your projects".

Evidence beats title. A resume headed "Software Engineer" whose every project is
AutoCAD and structural analysis is a civil engineer whose last job had a generic
title, and weighting the skills above the heading is what gets that right.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..models.candidate import Candidate, FactKind
from . import families as fam

# How much each kind of evidence counts. Skills dominate because they are the least
# ambiguous signal a resume carries; a title is one person's phrasing of a role.
WEIGHTS = {
    "skill": 3.0,
    "experience_title": 2.5,
    "project_body": 1.2,
    "education": 1.5,
    "certification": 1.0,
}

# Below this, the detection is too weak to act on and the caller should ask.
MIN_CONFIDENT_SCORE = 4.0


@dataclass
class FamilyMatch:
    family: fam.Family
    score: float
    reasons: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.family.id,
            "label": self.family.label,
            "group": self.family.group,
            "score": round(self.score, 2),
            "reasons": list(self.reasons),
            "queries": list(self.family.queries),
            "related": list(self.family.related),
        }


@dataclass
class Detection:
    matches: list[FamilyMatch] = field(default_factory=list)
    confident: bool = False

    @property
    def primary(self) -> fam.Family | None:
        return self.matches[0].family if self.matches else None

    @property
    def group(self) -> str:
        return self.matches[0].family.group if self.matches else "Technology"

    def suggested_ids(self, limit: int = 6) -> list[str]:
        return [m.family.id for m in self.matches[:limit]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "confident": self.confident,
            "primary": self.primary.id if self.primary else None,
            "group": self.group,
            "matches": [m.to_dict() for m in self.matches],
            "note": (
                ""
                if self.confident
                else (
                    "Not enough recognisable evidence to be sure of your field. Pick the "
                    "job families yourself — the search is only as good as this choice."
                )
            ),
        }


def _contains(haystack: str, needle: str) -> bool:
    """Word-boundary match that survives the punctuation resumes use."""
    return bool(re.search(rf"(?<![\w#+.]){re.escape(needle)}(?![\w#+])", haystack))


def detect(candidate: Candidate, limit: int = 8) -> Detection:
    """Rank the job families this candidate's evidence supports."""
    skills = {s.lower() for s in candidate.skill_labels()}
    titles = " ".join(f.label.lower() for f in candidate.facts if f.kind is FactKind.EXPERIENCE)
    projects = " ".join(
        f"{f.label} {f.body}".lower() for f in candidate.facts if f.kind is FactKind.PROJECT
    )
    education = " ".join(
        f"{f.label} {f.body}".lower() for f in candidate.facts if f.kind is FactKind.EDUCATION
    )
    certifications = " ".join(
        f.label.lower() for f in candidate.facts if f.kind is FactKind.CERTIFICATION
    )

    scored: list[FamilyMatch] = []
    for family in fam.FAMILIES:
        score = 0.0
        reasons: list[str] = []

        matched_skills = [s for s in family.skills if s in skills]
        if matched_skills:
            score += WEIGHTS["skill"] * min(len(matched_skills), 6) / 2
            reasons.append(f"skills: {', '.join(matched_skills[:5])}")

        matched_titles = [m for m in family.title_markers if m in titles]
        if matched_titles:
            score += WEIGHTS["experience_title"] * min(len(matched_titles), 3)
            reasons.append(f"your job titles mention {matched_titles[0]!r}")

        in_projects = [s for s in family.skills if s not in skills and _contains(projects, s)]
        if in_projects:
            score += WEIGHTS["project_body"] * min(len(in_projects), 4) / 2
            reasons.append(f"projects mention {', '.join(in_projects[:3])}")

        degree_markers = [m for m in family.title_markers if m in education]
        if degree_markers:
            score += WEIGHTS["education"] * min(len(degree_markers), 2)
            reasons.append(f"education mentions {degree_markers[0]!r}")

        cert_markers = [s for s in family.skills if _contains(certifications, s)]
        if cert_markers:
            score += WEIGHTS["certification"] * min(len(cert_markers), 2)
            reasons.append(f"certifications mention {cert_markers[0]!r}")

        if score > 0:
            scored.append(FamilyMatch(family, score, reasons))

    scored.sort(key=lambda m: m.score, reverse=True)
    top = scored[:limit]
    return Detection(matches=top, confident=bool(top) and top[0].score >= MIN_CONFIDENT_SCORE)


def suggest_families(candidate: Candidate, limit: int = 6) -> list[fam.Family]:
    """The families to offer as pre-ticked in the picker."""
    return [m.family for m in detect(candidate, limit).matches]


def archetype_for(family_ids: list[str]) -> str:
    """The resume archetype to tailor with, from the chosen families.

    Falls back to "custom" — which reorders by what the job asked for and emphasises
    nothing on its own — because inventing an emphasis for a field the archetype list
    does not cover is worse than applying none.
    """
    for family_id in family_ids:
        family = fam.get(family_id)
        if family and family.archetype != "custom":
            return family.archetype
    return "custom"
