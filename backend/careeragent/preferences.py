"""What you are actually looking for, kept somewhere the whole system can read it.

The scoring machinery has always understood experience band, work mode, employment type,
location and job age. `_hard_fail` even rejects a senior posting outright when you are
targeting an internship, on the grounds that a Senior Data Scientist matches a target role
perfectly and would otherwise outrank a real internship on every other component.

None of it ever fired, because nothing stored the answers. Every caller built
`SearchConfig(roles=[])` inline — an empty preference — so a first-year MSc student looking
for internships in Bangalore was shown Technical Architect roles in USA-Remote at 79%. The
scorer was working exactly as designed against a config that said nothing.

This is the missing half: one place that loads and saves the active preference, and which
every scoring path reads. `load()` never returns None — a missing preference is an empty one,
which is precisely the old behaviour, so nothing breaks before you have answered anything.

**Preferences are answers, not inferences.** Nothing here guesses your experience band from
your resume. A Fact Bank can say what you have done; it cannot say what you want next, and a
system that decided you wanted entry-level roles because your resume is short would be
wrong the moment you disagreed with it.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .logging import get_logger
from .models.candidate import SearchConfig
from .storage.models import SearchConfigRow

log = get_logger("preferences")

# ─── The vocabulary the scorer understands ──────────────────────────────────
#
# Listed here rather than only in the scorer so a UI can offer exactly what will work.
# A dropdown offering "graduate" when the scorer only recognises "fresher" produces a
# preference that silently does nothing, which is worse than no dropdown.

EXPERIENCE_BANDS: list[dict[str, str]] = [
    {"value": "internship", "label": "Internship", "note": "Intern postings only"},
    {"value": "fresher", "label": "Fresher / graduate", "note": "Entry-level and internships"},
    {"value": "entry", "label": "Entry level", "note": "Entry-level and internships"},
    {"value": "0-1", "label": "0–1 years", "note": "Entry-level and internships"},
    {"value": "1-2", "label": "1–2 years", "note": "Entry-level and mid roles"},
    {"value": "mid", "label": "Mid level", "note": "Mid-level roles"},
    {"value": "senior", "label": "Senior", "note": "Senior and lead roles"},
]

WORK_MODES: list[dict[str, str]] = [
    {"value": "remote", "label": "Remote"},
    {"value": "hybrid", "label": "Hybrid"},
    {"value": "onsite", "label": "On-site"},
]

EMPLOYMENT_TYPES: list[dict[str, str]] = [
    {"value": "internship", "label": "Internship"},
    {"value": "full_time", "label": "Full time"},
    {"value": "part_time", "label": "Part time"},
    {"value": "contract", "label": "Contract"},
]

# The bands where a senior or lead posting is rejected outright rather than scored low.
# Mirrors `pipeline.score._JUNIOR_BANDS`; a test asserts they have not drifted apart.
JUNIOR_BANDS = frozenset({"internship", "intern", "fresher", "entry", "entry level", "0-1"})

DEFAULT_NAME = "default"


def _valid(values: list[str] | None, allowed: list[dict[str, str]]) -> list[str]:
    """Keep only the values the scorer will act on.

    Silently dropping an unrecognised value is right here: a preference the scorer does not
    understand does nothing, and storing it would make the UI show a setting that has no
    effect.
    """
    permitted = {entry["value"] for entry in allowed}
    return [v for v in (values or []) if str(v).strip().lower() in permitted]


async def load(session: AsyncSession, candidate_id: str) -> SearchConfig:
    """The active preference. Never None — an unanswered preference is an empty one."""
    row = (
        (
            await session.execute(
                select(SearchConfigRow).where(
                    SearchConfigRow.candidate_id == candidate_id,
                    SearchConfigRow.active.is_(True),
                )
            )
        )
        .scalars()
        .first()
    )

    if row is None or not row.config:
        return SearchConfig(name=DEFAULT_NAME)
    try:
        return SearchConfig(**row.config)
    except Exception as exc:
        # A stored preference that no longer parses — a field renamed, a bad hand edit —
        # must not stop the system scoring. It degrades to the behaviour it had before
        # preferences existed, loudly.
        log.warning("preferences_unreadable", error=f"{type(exc).__name__}: {exc}"[:140])
        return SearchConfig(name=DEFAULT_NAME)


async def save(
    session: AsyncSession,
    candidate_id: str,
    config: SearchConfig,
) -> SearchConfig:
    """Store the active preference, keeping only values the scorer acts on."""
    config.roles = [r.strip() for r in config.roles if r and r.strip()]
    config.locations = [loc.strip() for loc in config.locations if loc and loc.strip()]
    config.work_modes = _valid(config.work_modes, WORK_MODES)
    config.employment_types = _valid(config.employment_types, EMPLOYMENT_TYPES)

    band = (config.experience_band or "").strip().lower()
    config.experience_band = band if band in {e["value"] for e in EXPERIENCE_BANDS} else None

    if config.max_job_age_days is not None:
        config.max_job_age_days = max(1, min(int(config.max_job_age_days), 365))

    row = (
        (
            await session.execute(
                select(SearchConfigRow).where(
                    SearchConfigRow.candidate_id == candidate_id,
                    SearchConfigRow.active.is_(True),
                )
            )
        )
        .scalars()
        .first()
    )

    if row is None:
        row = SearchConfigRow(candidate_id=candidate_id, name=config.name or DEFAULT_NAME)
        session.add(row)
    row.config = config.model_dump(mode="json")
    row.name = config.name or DEFAULT_NAME
    row.active = True
    await session.flush()

    log.info(
        "preferences_saved",
        band=config.experience_band,
        work_modes=config.work_modes,
        employment=config.employment_types,
        locations=len(config.locations),
        roles=len(config.roles),
    )
    return config


def describe(config: SearchConfig) -> dict[str, Any]:
    """The preference plus what it will actually do, in words.

    The effects are spelled out because the consequential one is invisible otherwise: a
    junior band does not merely deprioritise senior roles, it removes them. Someone who set
    "internship" and then wondered where all the jobs went deserves to have been told.
    """
    band = (config.experience_band or "").lower()
    effects: list[str] = []

    if band in JUNIOR_BANDS:
        effects.append(
            "Senior and lead roles are rejected outright, not just ranked lower — they are "
            "unreachable, and surfacing them crowds out the roles you can actually get."
        )
    elif band:
        effects.append(f"Roles outside the {band} band score lower.")
    else:
        effects.append(
            "No experience band set, so a senior architect role can outrank an internship."
        )

    if config.employment_types:
        effects.append(
            "Anything that is not "
            + " or ".join(t.replace("_", " ") for t in config.employment_types)
            + " is rejected."
        )
    if config.work_modes:
        effects.append("Remote roles only count as a location match if remote is wanted.")
    if config.locations:
        effects.append(f"Roles outside {', '.join(config.locations)} score lower.")
    else:
        effects.append("No location preference, so a role anywhere scores the same.")
    if config.companies_blocked:
        effects.append(f"{len(config.companies_blocked)} company(ies) are blocked outright.")
    if config.max_job_age_days:
        effects.append(f"Postings older than {config.max_job_age_days} days are rejected.")

    return {
        "config": config.model_dump(mode="json"),
        "is_set": bool(band or config.locations or config.employment_types or config.roles),
        "effects": effects,
        "options": {
            "experience_bands": EXPERIENCE_BANDS,
            "work_modes": WORK_MODES,
            "employment_types": EMPLOYMENT_TYPES,
        },
    }
