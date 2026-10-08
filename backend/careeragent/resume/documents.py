"""The tailored-resume document model.

Kept separate from both generation and validation so the validator never imports
the generator. The point of that is enforcement order: generation produces a
`TailoredResume`, the validator is a pure function over it, and nothing renders
or uploads a document the validator has not passed.

Every claim-bearing line carries `fact_ids`. A bullet with no fact behind it is
not "unattributed", it is unsupported — and the validator treats it that way.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class Bullet:
    """One line of a resume, with the evidence it rests on."""

    text: str
    fact_ids: list[str] = field(default_factory=list)
    # True when the text is reproduced from the master resume rather than
    # rewritten. Copied lines need no fabrication check: the candidate wrote them.
    verbatim: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "fact_ids": list(self.fact_ids), "verbatim": self.verbatim}


@dataclass
class Entry:
    """An experience, project, or education entry."""

    title: str
    subtitle: str = ""
    dates: str = ""
    bullets: list[Bullet] = field(default_factory=list)
    fact_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "subtitle": self.subtitle,
            "dates": self.dates,
            "bullets": [b.to_dict() for b in self.bullets],
            "fact_ids": list(self.fact_ids),
        }


@dataclass
class TailoredResume:
    """A resume built for one job out of one Fact Bank."""

    candidate_name: str
    archetype: str = "custom"
    job_id: str | None = None
    job_title: str = ""
    company: str = ""

    contact: dict[str, str] = field(default_factory=dict)
    summary: str = ""
    summary_fact_ids: list[str] = field(default_factory=list)
    skills: list[str] = field(default_factory=list)
    experience: list[Entry] = field(default_factory=list)
    projects: list[Entry] = field(default_factory=list)
    education: list[Entry] = field(default_factory=list)
    certifications: list[Bullet] = field(default_factory=list)
    achievements: list[Bullet] = field(default_factory=list)

    # Skills the job wanted that the candidate has no evidence for. Recorded on
    # the document itself so the gap travels with it instead of being quietly
    # dropped, and so the user sees what this application is weak on.
    gaps: list[str] = field(default_factory=list)
    generated_by: str = "deterministic"  # deterministic | llm
    model: str = ""
    notes: list[str] = field(default_factory=list)

    def all_fact_ids(self) -> list[str]:
        ids: list[str] = list(self.summary_fact_ids)
        for entry in (*self.experience, *self.projects, *self.education):
            ids.extend(entry.fact_ids)
            for bullet in entry.bullets:
                ids.extend(bullet.fact_ids)
        for bullet in (*self.certifications, *self.achievements):
            ids.extend(bullet.fact_ids)
        return list(dict.fromkeys(ids))

    def all_bullets(self) -> list[Bullet]:
        out: list[Bullet] = []
        for entry in (*self.experience, *self.projects, *self.education):
            out.extend(entry.bullets)
        out.extend(self.certifications)
        out.extend(self.achievements)
        return out

    def plain_text(self) -> str:
        """The ATS-readable form. This is what a parser sees, so it is also what
        the ATS validator scores — not the styled PDF."""
        lines: list[str] = [self.candidate_name]
        contact_line = " | ".join(v for v in self.contact.values() if v)
        if contact_line:
            lines.append(contact_line)
        if self.summary:
            lines += ["", "SUMMARY", self.summary]
        if self.skills:
            lines += ["", "SKILLS", ", ".join(self.skills)]
        for heading, entries in (
            ("EXPERIENCE", self.experience),
            ("PROJECTS", self.projects),
            ("EDUCATION", self.education),
        ):
            if not entries:
                continue
            lines += ["", heading]
            for entry in entries:
                header = entry.title
                if entry.subtitle:
                    header += f" — {entry.subtitle}"
                if entry.dates:
                    header += f"  ({entry.dates})"
                lines.append(header)
                lines += [f"- {b.text}" for b in entry.bullets]
        for heading, bullets in (
            ("CERTIFICATIONS", self.certifications),
            ("ACHIEVEMENTS", self.achievements),
        ):
            if not bullets:
                continue
            lines += ["", heading]
            lines += [f"- {b.text}" for b in bullets]
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_name": self.candidate_name,
            "archetype": self.archetype,
            "job_id": self.job_id,
            "job_title": self.job_title,
            "company": self.company,
            "contact": dict(self.contact),
            "summary": self.summary,
            "summary_fact_ids": list(self.summary_fact_ids),
            "skills": list(self.skills),
            "experience": [e.to_dict() for e in self.experience],
            "projects": [e.to_dict() for e in self.projects],
            "education": [e.to_dict() for e in self.education],
            "certifications": [b.to_dict() for b in self.certifications],
            "achievements": [b.to_dict() for b in self.achievements],
            "gaps": list(self.gaps),
            "generated_by": self.generated_by,
            "model": self.model,
            "notes": list(self.notes),
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TailoredResume:
        def bullets(items: Any) -> list[Bullet]:
            return [
                Bullet(
                    text=str(b.get("text") or ""),
                    fact_ids=list(b.get("fact_ids") or []),
                    verbatim=bool(b.get("verbatim")),
                )
                for b in (items or [])
                if isinstance(b, dict)
            ]

        def entries(items: Any) -> list[Entry]:
            return [
                Entry(
                    title=str(e.get("title") or ""),
                    subtitle=str(e.get("subtitle") or ""),
                    dates=str(e.get("dates") or ""),
                    bullets=bullets(e.get("bullets")),
                    fact_ids=list(e.get("fact_ids") or []),
                )
                for e in (items or [])
                if isinstance(e, dict)
            ]

        return cls(
            candidate_name=str(data.get("candidate_name") or ""),
            archetype=str(data.get("archetype") or "custom"),
            job_id=data.get("job_id"),
            job_title=str(data.get("job_title") or ""),
            company=str(data.get("company") or ""),
            contact=dict(data.get("contact") or {}),
            summary=str(data.get("summary") or ""),
            summary_fact_ids=list(data.get("summary_fact_ids") or []),
            skills=list(data.get("skills") or []),
            experience=entries(data.get("experience")),
            projects=entries(data.get("projects")),
            education=entries(data.get("education")),
            certifications=bullets(data.get("certifications")),
            achievements=bullets(data.get("achievements")),
            gaps=list(data.get("gaps") or []),
            generated_by=str(data.get("generated_by") or "deterministic"),
            model=str(data.get("model") or ""),
            notes=list(data.get("notes") or []),
        )
