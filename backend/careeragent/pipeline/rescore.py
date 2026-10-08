"""Recomputing stored scores in place — no network, no LLM spend.

A scoring bug does not fix itself when you fix the scorer. Every score already in the
database was computed by the old code and stays wrong until something recomputes it, and
"run discovery again" is the wrong answer: it re-fetches a thousand job descriptions that
have not changed, spends the API quota, and takes minutes.

This reads what is already stored and applies the current scorer to it.

**What is preserved matters as much as what is recomputed.** Stage 2 (embeddings) and
stage 3 (LLM reasoning) were never affected by a matcher change, and stage 3 cost real
money. Both are read back from the stored row and folded into the new blend unchanged.
Discarding them would turn a free correction into a paid one.

**Nothing is fetched, so nothing can be lost to a source being down.** The worst outcome
here is that a score is recomputed to the same value it already had.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ..logging import get_logger
from ..models.candidate import Candidate
from ..models.scoring import LLMScore, SemanticScore
from ..storage import repository as repo
from ..storage.models import Job, JobScore

log = get_logger("pipeline.rescore")

# A score that moved by less than this is noise from a rounding change, not a correction.
MEANINGFUL_DELTA = 0.02


@dataclass
class Change:
    job_id: str
    title: str
    company: str
    before: float
    after: float
    skills_before: list[str] = field(default_factory=list)
    skills_after: list[str] = field(default_factory=list)
    hard_fail: str = ""

    @property
    def delta(self) -> float:
        return round(self.after - self.before, 4)

    @property
    def reason(self) -> str:
        """Why this score moved, in the order the causes actually dominate.

        The hard failure has to come first. A job past the freshness limit is scored to zero
        *before* skills are looked at, so `matched_skills` comes back empty — and a report
        that read that diff would announce that a Data Analyst role "no longer matches
        Python", which is false and is the wrong thing to tell someone deciding where to
        apply. The first version of this said exactly that, for 548 rows.
        """
        if self.hard_fail:
            return self.hard_fail

        # Compared canonically, because the alias fix shows up in the raw lists as a
        # deletion. "py" being replaced by "python" is a rename, and reporting it as
        # "no longer matches py" beside a score that went *up* is the kind of line that
        # makes a reader distrust the whole report.
        before, after = self.canonical_before, self.canonical_after
        lost = sorted(before - after)
        gained = sorted(after - before)
        if lost:
            return "no longer matches " + ", ".join(lost[:4])
        if gained:
            return "now matches " + ", ".join(gained[:4])
        if set(self.skills_before) != set(self.skills_after):
            # Same skills, different spellings: aliases used to be counted as separate
            # matches, so a Fact Bank carrying them scored higher than one without.
            return "same skills, no longer counted twice through aliases"
        return "weighting changed"

    @staticmethod
    def _canonical(labels: list[str]) -> set[str]:
        from ..resume.taxonomy import ALIAS_TO_CANONICAL

        return {ALIAS_TO_CANONICAL.get(s.lower(), s.lower()) for s in labels if s}

    @property
    def canonical_before(self) -> set[str]:
        return self._canonical(self.skills_before)

    @property
    def canonical_after(self) -> set[str]:
        return self._canonical(self.skills_after)

    @property
    def kind(self) -> str:
        """What sort of change this is, for the per-cause counts."""
        if self.hard_fail:
            return "aged_out" if "posted" in self.hard_fail else "disqualified"
        if self.canonical_before != self.canonical_after:
            return "skills"
        if set(self.skills_before) != set(self.skills_after):
            return "aliases"
        return "other"

    def to_dict(self) -> dict[str, Any]:
        return {
            "job_id": self.job_id,
            "title": self.title,
            "company": self.company,
            "before": round(self.before, 4),
            "after": round(self.after, 4),
            "delta": self.delta,
            "kind": self.kind,
            "reason": self.reason,
            "hard_fail": self.hard_fail or None,
            "skills_removed": sorted(set(self.skills_before) - set(self.skills_after)),
            "skills_added": sorted(set(self.skills_after) - set(self.skills_before)),
        }


@dataclass
class Report:
    scored: int = 0
    changed: int = 0
    rose: int = 0
    fell: int = 0
    by_kind: dict[str, int] = field(default_factory=dict)
    biggest: list[Change] = field(default_factory=list)
    dry_run: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "scored": self.scored,
            "changed": self.changed,
            "rose": self.rose,
            "fell": self.fell,
            "unchanged": self.scored - self.changed,
            "by_kind": dict(self.by_kind),
            "dry_run": self.dry_run,
            "biggest_moves": [c.to_dict() for c in self.biggest],
        }

    def summary(self) -> str:
        """Counted by cause, because the causes mean different things.

        A posting ageing past the freshness limit and a skill match being corrected are not
        the same event: the first is the market moving on, the second is this system fixing
        something it got wrong. One combined number invites the reader to attribute all of
        it to whichever they happened to be thinking about.
        """
        if not self.scored:
            return "No stored scores to recompute."
        verb = "would change" if self.dry_run else "changed"
        parts = [
            f"{self.scored} score(s) recomputed, {self.changed} {verb} "
            f"({self.rose} up, {self.fell} down)."
        ]
        if aged := self.by_kind.get("aged_out"):
            parts.append(f"{aged} because the posting is now past the freshness limit.")
        if skills := self.by_kind.get("skills"):
            parts.append(f"{skills} because the skill match itself changed.")
        if aliases := self.by_kind.get("aliases"):
            parts.append(f"{aliases} because aliases are no longer counted as extra skills.")
        if other := self.by_kind.get("other"):
            parts.append(f"{other} from weighting alone.")
        return " ".join(parts)


def _current_company_fit(
    job: Job, fit_by_company: dict[str, float], stored: JobScore
) -> float | None:
    """This company's fit as it stands now, falling back to whatever was stored.

    Returns None rather than 0.0 when nothing is known: the opportunity scorer excludes an
    unknown company fit from the weighting, and passing a zero would score an unresearched
    company as a bad one.
    """
    from ..models.job import _norm

    # The same key `company_fit_map` is built with, so a name that normalises one way in
    # the map cannot silently miss here.
    key = _norm(job.company or "")
    if key and key in fit_by_company:
        return fit_by_company[key]
    return float(stored.company_fit) if stored.company_fit else None


async def rescore(
    session: AsyncSession,
    candidate: Candidate,
    candidate_id: str,
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> Report:
    """Recompute every stored score for this candidate against the current scorer.

    `dry_run` computes and reports without writing, which is the right default for finding
    out whether a scorer change did what was intended before it moves a thousand rows.
    """
    from .. import preferences
    from ..applications.service import _job_to_normalized
    from .opportunity import score_opportunity
    from .score import ScoreContext, score_job

    report = Report(dry_run=dry_run)
    # Rescoring against an empty preference would undo whatever the user has set, which is
    # the opposite of what a correction pass is for.
    context = ScoreContext(
        candidate=candidate, config=await preferences.load(session, candidate_id)
    )

    # Company fit is read fresh rather than from the stored score. This function exists to
    # recompute against the *current* state, and a company researched since the last run is
    # part of that state — reusing the stored value meant researching sixty companies moved
    # nothing, because every job kept the 0.0 it was first scored with.
    fit_by_company = await repo.company_fit_map(session)

    stmt = (
        select(JobScore, Job)
        .join(Job, Job.id == JobScore.job_id)
        .where(JobScore.candidate_id == candidate_id)
    )
    if limit:
        stmt = stmt.limit(limit)
    rows = (await session.execute(stmt)).all()

    changes: list[Change] = []
    for stored, job in rows:
        before = float(stored.final_score or 0.0)
        skills_before = list((stored.deterministic or {}).get("matched_skills") or [])

        normalized = _job_to_normalized(job)
        deterministic = score_job(normalized, context)

        # Stage 2 and stage 3 are read back rather than recomputed. Neither depends on skill
        # matching, and stage 3 was paid for.
        semantic = SemanticScore(**stored.semantic) if stored.semantic else None
        llm = LLMScore(**stored.llm) if stored.llm else None

        fresh = score_opportunity(
            normalized,
            deterministic,
            semantic=semantic,
            llm=llm,
            company_fit=_current_company_fit(job, fit_by_company, stored),
        )

        report.scored += 1
        after = float(fresh.final_score)
        if abs(after - before) >= MEANINGFUL_DELTA:
            report.changed += 1
            report.rose += after > before
            report.fell += after < before
            change = Change(
                job_id=job.id,
                title=job.title,
                company=job.company or "",
                before=before,
                after=after,
                skills_before=skills_before,
                skills_after=list(deterministic.matched_skills),
                hard_fail=deterministic.hard_fail or "",
            )
            report.by_kind[change.kind] = report.by_kind.get(change.kind, 0) + 1
            changes.append(change)

        if not dry_run:
            stored.deterministic = deterministic.model_dump(mode="json")
            stored.job_fit = fresh.job_fit
            # Written as well as used. Leaving this out meant the stored company fit stayed
            # at the 0.0 a job was first scored with while the final score reflected the
            # real value — the Jobs page showed "co=0.00" beside a score that company fit
            # had already moved, which is a number contradicting itself on screen.
            stored.company_fit = fresh.company_fit
            stored.final_score = after
            stored.priority = fresh.priority.value

    # Corrections first. Ageing out always moves a score further than a matching change
    # does, so sorting by raw delta would bury every row that says something about the
    # scorer under a wall of rows that only say the calendar moved.
    changes.sort(key=lambda c: (c.kind not in {"skills", "aliases"}, -abs(c.delta)))
    report.biggest = changes[:15]

    if not dry_run:
        await session.flush()
    log.info(
        "rescored",
        scored=report.scored,
        changed=report.changed,
        rose=report.rose,
        fell=report.fell,
        dry_run=dry_run,
    )
    return report
