"""Natural-language command parsing.

Turns "Find AI/ML internships in Bangalore posted this week" into a structured
`SearchConfig` for review.

Deliberately deterministic for now. An LLM version slots in behind the same
signature once a provider key is configured, but the important property holds
either way: this only ever *parses*. Acting on the result is a separate,
explicit call, so a misread instruction cannot submit applications
(docs/01-architecture.md §6).
"""

from __future__ import annotations

import re

from .models.candidate import SearchConfig

ROLE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bml\b|\bmachine learning\b", "ML Engineer"),
    (r"\bai engineer\b|\bartificial intelligence\b", "AI Engineer"),
    (r"\bgen\s?ai\b|\bgenerative ai\b|\bllm\b", "GenAI Engineer"),
    (r"\bnlp\b|\bnatural language\b", "NLP Engineer"),
    (r"\bdata scien", "Data Scientist"),
    (r"\bdata analy", "Data Analyst"),
    (r"\bdata engineer", "Data Engineer"),
    (r"\bcomputer vision\b|\bcv engineer\b", "Computer Vision Engineer"),
    (r"\bsoftware engineer\b|\bswe\b|\bsde\b|\bbackend\b|\bfull.?stack\b", "Software Engineer"),
    (r"\bresearch\b", "Research Engineer"),
    (r"\bmlops\b|\bplatform engineer\b", "MLOps Engineer"),
)

# Indian metros plus the common remote/international phrasings. A curated list
# beats a gazetteer here: it cannot mistake a company name for a city.
LOCATIONS: tuple[str, ...] = (
    "Bangalore",
    "Bengaluru",
    "Chennai",
    "Coimbatore",
    "Hyderabad",
    "Pune",
    "Mumbai",
    "Delhi",
    "Noida",
    "Gurgaon",
    "Gurugram",
    "Kolkata",
    "Ahmedabad",
    "Kochi",
    "Trivandrum",
    "Jaipur",
    "Indore",
    "India",
    "London",
    "Berlin",
    "Amsterdam",
    "Dublin",
    "Singapore",
    "Dubai",
    "New York",
    "San Francisco",
    "Seattle",
    "Austin",
    "Toronto",
)

EXPERIENCE_PATTERNS: tuple[tuple[str, str], ...] = (
    (r"\bintern(ship)?s?\b", "internship"),
    (r"\bfresher|new grad|graduate\b", "entry"),
    (r"\bentry.?level\b|\b0.?1 year", "entry"),
    (r"\bjunior\b|\b1.?2 year", "entry"),
    (r"\bmid.?level\b|\b3.?5 year", "mid"),
    (r"\bsenior\b|\bstaff\b|\bprincipal\b", "senior"),
)

AGE_PATTERNS: tuple[tuple[str, int], ...] = (
    (r"\b(today|last 24 hours|past day|since yesterday)\b", 1),
    (r"\b(this week|last 7 days|past week)\b", 7),
    (r"\blast (\d+) days?\b", 0),  # captured below
    (r"\b(this month|last 30 days|past month)\b", 30),
    (r"\b(last 3 days|past 3 days)\b", 3),
)

WORK_MODES: tuple[tuple[str, str], ...] = (
    (r"\bremote\b|\bwork from home\b|\bwfh\b", "remote"),
    (r"\bhybrid\b", "hybrid"),
    (r"\bon.?site\b|\bin.?office\b", "onsite"),
)

EMPLOYMENT_TYPES: tuple[tuple[str, str], ...] = (
    (r"\bintern(ship)?s?\b", "internship"),
    (r"\bfull.?time\b", "full_time"),
    (r"\bpart.?time\b", "part_time"),
    (r"\bcontract\b|\bfreelance\b", "contract"),
)


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out = []
    for value in values:
        key = value.lower()
        if key not in seen:
            seen.add(key)
            out.append(value)
    return out


def parse_search_command(text: str) -> tuple[SearchConfig, str]:
    """Parse an instruction into a reviewable config plus a plain-English summary.

    Anything not recognised is simply absent from the config rather than
    guessed. An over-broad search wastes a run; a silently wrong one wastes an
    application.
    """
    lowered = text.lower()
    config = SearchConfig(name="from command")

    config.roles = _dedupe([role for pattern, role in ROLE_PATTERNS if re.search(pattern, lowered)])
    config.locations = _dedupe(
        [loc for loc in LOCATIONS if re.search(rf"\b{re.escape(loc.lower())}\b", lowered)]
    )
    config.work_modes = _dedupe(
        [mode for pattern, mode in WORK_MODES if re.search(pattern, lowered)]
    )
    if "remote" in config.work_modes and "Remote" not in config.locations:
        config.locations.append("Remote")

    config.employment_types = _dedupe(
        [kind for pattern, kind in EMPLOYMENT_TYPES if re.search(pattern, lowered)]
    )

    for pattern, band in EXPERIENCE_PATTERNS:
        if re.search(pattern, lowered):
            config.experience_band = band
            break

    if m := re.search(r"\blast (\d+) days?\b", lowered):
        config.max_job_age_days = int(m.group(1))
    else:
        for pattern, days in AGE_PATTERNS:
            if days and re.search(pattern, lowered):
                config.max_job_age_days = days
                break

    if m := re.search(r"\b(?:top|best|first)\s+(\d+)\b", lowered):
        config.limit = int(m.group(1))

    # "not X", "excluding X", "no X"
    for m in re.finditer(
        r"\b(?:exclude|excluding|not|no|without)\s+([a-z0-9+#.\- ]{2,25})", lowered
    ):
        term = m.group(1).strip()
        if term and len(term.split()) <= 3:
            config.keywords_exclude.append(term)

    return config, _summarize(config, text)


def _summarize(config: SearchConfig, original: str) -> str:
    parts: list[str] = []
    parts.append(
        f"Search for {', '.join(config.roles)} roles" if config.roles else "Search for any role"
    )
    if config.locations:
        parts.append(f"in {', '.join(config.locations)}")
    if config.experience_band:
        parts.append(f"at {config.experience_band} level")
    if config.employment_types:
        parts.append(f"({', '.join(config.employment_types)})")
    if config.max_job_age_days:
        parts.append(f"posted within {config.max_job_age_days} days")
    if config.keywords_exclude:
        parts.append(f"excluding {', '.join(config.keywords_exclude)}")

    summary = " ".join(parts) + "."
    if not config.roles and not config.locations:
        summary += (
            " Nothing specific was recognised in that instruction — "
            "edit the config before running it."
        )
    _ = original
    return summary
