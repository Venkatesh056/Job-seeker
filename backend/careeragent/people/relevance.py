"""Which of these people is worth contacting, for *this* candidate and *this* job?

The instinct a job-search tool usually encodes is a seniority ladder: CEO at the top,
founder next, everyone else beneath. That instinct is wrong here, and wrong in a way that
wastes the one thing a candidate cannot get more of — the small number of messages a
person can credibly send.

For an entry-level ML opening, the CEO of a 3,000-person company will not read the message
and could not act on it if they did. The engineering manager who owns the team is the
person with the problem the candidate solves, and the technical recruiter is the person
whose actual job is to receive exactly this message. At a twelve-person startup the same
CEO *is* the hiring manager and is the right contact. So seniority is not a score on its
own; it is one input, and its sign depends on the company's size and the role's level.

Relevance is therefore computed from seven things, each of which can be unknown:

    function       does this person work in the area the candidate is targeting?
    authority      can they act — hire, refer, or decide?
    proximity      how close to the actual opening, rather than to the org chart's top?
    seniority      calibrated against company size and the role's level, not absolute
    technical      would they recognise the candidate's work as relevant?
    reachability   is there a channel that exists and is safe to use?
    history        have we already contacted them, and how did that go?

An unknown input surrenders its weight rather than scoring zero — the same rule the rest
of this project now follows, for the same reason.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..models.evidence import Observation, Scored, combine, measured, not_applicable, unknown
from ..models.person import Person

# ─── What each kind of role is for ──────────────────────────────────────────

# Phrases that identify a *function*. Deliberately about the work, not the rank — "head of
# data" and "data engineer" are the same function at different heights.
FUNCTION_MARKERS: dict[str, tuple[str, ...]] = {
    "ai_ml": (
        "machine learning",
        "ml engineer",
        "ml platform",
        "artificial intelligence",
        " ai ",
        "ai engineer",
        "ai platform",
        "deep learning",
        "nlp",
        "computer vision",
        "applied scientist",
        "research scientist",
        "genai",
        "generative ai",
        "llm",
    ),
    "data": (
        "data scien",
        "data engineer",
        "data analyt",
        "data analyst",
        "data platform",
        "analyst",
        "analytics",
        "business intelligence",
        "data warehouse",
        "head of data",
        "decision scien",
    ),
    "engineering": (
        "engineering",
        "software",
        "backend",
        "frontend",
        "full stack",
        "fullstack",
        "platform",
        "infrastructure",
        "devops",
        "sre",
        "architect",
        "developer",
        "technology",
        "technical",
        "cto",
    ),
    "product": ("product manager", "product management", "head of product", "cpo"),
    "recruiting": (
        "recruit",
        "talent acquisition",
        "talent partner",
        "hiring",
        "human resource",
        "hr ",
        "people ops",
        "people operations",
        "staffing",
    ),
    "design": ("design", "ux", "user experience", "ui "),
    "sales": ("sales", "account executive", "business development", "revenue", "cro"),
    "marketing": ("marketing", "growth", "brand", "content", "seo", "cmo"),
    "finance": (
        "financ",
        "accounting",
        "controller",
        "cfo",
        "treasur",
        "audit",
    ),
    "legal": ("legal", "counsel", "compliance", "general counsel"),
    "operations": (
        "operations",
        "operating officer",
        "supply chain",
        "logistics",
        "coo",
    ),
    "executive": (
        "ceo",
        "founder",
        "chief executive",
        "managing director",
        "president",
        "board member",
        "chairman",
        "chairperson",
    ),
}

# Which functions matter for which career family, best first. This is what makes discovery
# career-aware: an ML candidate wants the AI lead and the technical recruiter, a civil
# engineering candidate wants the project manager and the engineering head.
CAREER_PRIORITIES: dict[str, tuple[str, ...]] = {
    "ai_ml": ("ai_ml", "data", "engineering", "recruiting", "product", "executive"),
    "data": ("data", "ai_ml", "engineering", "recruiting", "product", "executive"),
    "engineering": ("engineering", "product", "ai_ml", "data", "recruiting", "executive"),
    "product": ("product", "engineering", "design", "recruiting", "executive"),
    "design": ("design", "product", "engineering", "recruiting", "executive"),
    "sales": ("sales", "marketing", "recruiting", "executive"),
    "marketing": ("marketing", "sales", "product", "recruiting", "executive"),
    "finance": ("finance", "operations", "recruiting", "executive"),
    "operations": ("operations", "finance", "recruiting", "executive"),
    "legal": ("legal", "recruiting", "executive"),
}

# Seniority, as a rank rather than a score. Turned into a score later, against the role.
SENIORITY_MARKERS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (5, ("ceo", "founder", "co-founder", "chief executive", "president", "managing director")),
    (4, ("chief ", "cto", "cfo", "coo", "cpo", "cmo", "cro", "vp ", "vice president", "head of")),
    (3, ("director", "principal", "senior manager", "group manager")),
    (2, ("manager", "lead", "staff", "supervisor")),
    (1, ("senior", "sr ", "specialist", "partner")),
    (0, ()),
)

# Titles whose holder can actually start a hiring conversation.
HIRING_AUTHORITY = (
    "recruit",
    "talent acquisition",
    "hiring manager",
    "head of",
    "vp ",
    "vice president",
    "director",
    "manager",
    "lead",
    "founder",
    "ceo",
    "cto",
    "chief",
)


def _normalize(text: str) -> str:
    return f" {re.sub(r'[^a-z0-9+#. ]+', ' ', (text or '').lower())} "


# Matching a marker as a bare substring is how "cto" was found inside "dire(cto)r" and
# ranked an Engineering Director as a C-level executive. The same mistake as the skill
# matcher's, where "excellent" matched "excel" — so the same fix: whole words only.
_MARKER_CACHE: dict[str, re.Pattern[str]] = {}


# A short marker needs a boundary on *both* sides. Measured: "ai" matched the start of
# "Aiven", so `function_of("Chief Financial Officer, Aiven")` returned `ai_ml` and a CFO
# scored 85/100 for a machine-learning candidate. Longer markers are deliberately left open
# at the end, because several are prefixes — "recruit" must reach "recruiter", "financ"
# must reach "financial", "data scien" must reach "data scientist".
_SHORT_MARKER = 4


def _matches(marker: str, blob: str) -> bool:
    pattern = _MARKER_CACHE.get(marker)
    if pattern is None:
        clean = marker.strip()
        tail = r"(?![a-z0-9])" if len(clean) <= _SHORT_MARKER else ""
        pattern = re.compile(rf"(?<![a-z0-9]){re.escape(clean)}{tail}", re.I)
        _MARKER_CACHE[marker] = pattern
    return bool(pattern.search(blob))


def function_of(title: str) -> str:
    """Which function this title belongs to. "" when the title says nothing useful.

    Order matters: the specific functions are checked before the general ones, so
    "Head of AI Engineering" reads as `ai_ml` rather than `engineering`, and "VP of Data
    Science" as `data`.
    """
    blob = _normalize(title)
    for name in (
        "ai_ml",
        "data",
        "recruiting",
        "product",
        "design",
        "finance",
        "legal",
        "sales",
        "marketing",
        "operations",
        "engineering",
        "executive",
    ):
        if any(_matches(marker, blob) for marker in FUNCTION_MARKERS[name]):
            return name
    return ""


def seniority_of(title: str) -> int:
    """0–5. A rank, not a score — how it is scored depends on the role and the company."""
    blob = _normalize(title)
    for rank, markers in SENIORITY_MARKERS:
        if markers and any(_matches(marker, blob) for marker in markers):
            return rank
    return 0


def has_hiring_authority(title: str) -> bool:
    blob = _normalize(title)
    return any(_matches(marker, blob) for marker in HIRING_AUTHORITY)


def career_family(target_role: str) -> str:
    """Which career family a target role belongs to, for choosing whom to prioritise."""
    return function_of(target_role) or "engineering"


@dataclass
class PersonRelevance:
    """Why this person is, or is not, worth a message."""

    person: Person
    scored: Scored
    function: str = ""
    seniority: int = 0
    hiring_authority: bool = False
    recommended_channel: str = ""
    recommendation: str = ""
    reasons: list[str] = field(default_factory=list)

    @property
    def score(self) -> float:
        return self.scored.score

    @property
    def out_of_100(self) -> int:
        return round(self.scored.score * 100)

    @property
    def worth_contacting(self) -> bool:
        return self.scored.score >= 0.45 and self.person.reachable

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.person.to_dict(),
            "relevance": self.out_of_100,
            "confidence": self.scored.confidence,
            "evidence_coverage": self.scored.evidence_coverage,
            "unknown_fields": self.scored.unknown_fields,
            "function": self.function,
            "seniority": self.seniority,
            "hiring_authority": self.hiring_authority,
            "recommended_channel": self.recommended_channel,
            "recommendation": self.recommendation,
            "reasons": self.reasons,
            "worth_contacting": self.worth_contacting,
        }


WEIGHTS = {
    "function": 0.30,
    "authority": 0.20,
    "proximity": 0.18,
    "seniority": 0.12,
    "technical": 0.10,
    "reachability": 0.10,
}


def score_person(
    person: Person,
    *,
    target_role: str,
    job_title: str = "",
    company_size: int | None = None,
    role_is_junior: bool = True,
    previously_contacted: bool = False,
    replied: bool = False,
    opted_out: bool = False,
) -> PersonRelevance:
    """How useful is this person, for this candidate, at this company, for this opening?

    `company_size` and `role_is_junior` are what stop the seniority component being a
    simple ladder. At a twelve-person startup the founder *is* the hiring manager; at a
    three-thousand-person company they are unreachable and could not act on the message
    anyway.
    """
    observations: list[Observation] = []
    reasons: list[str] = []

    title = person.title or ""
    function = function_of(title)
    seniority = seniority_of(title)
    authority = has_hiring_authority(title)
    wanted = career_family(target_role)
    priorities = CAREER_PRIORITIES.get(wanted, CAREER_PRIORITIES["engineering"])

    # ── Does their function match the candidate's target? ──────────────────
    if not title:
        observations.append(unknown("function", "no job title is recorded for them"))
    elif not function:
        observations.append(
            unknown("function", f"'{title}' does not identify a recognisable function")
        )
    elif function in priorities:
        rank = priorities.index(function)
        value = max(0.15, 1.0 - rank * 0.16)
        observations.append(measured("function", value))
        if rank == 0:
            reasons.append(f"works directly in {function.replace('_', '/')}, your target area")
        elif function == "recruiting":
            reasons.append("recruiting — their job is to receive exactly this message")
        elif rank <= 2:
            reasons.append(f"works in {function.replace('_', '/')}, adjacent to your target")
    else:
        observations.append(measured("function", 0.05))
        reasons.append(f"works in {function}, which is unrelated to what you are looking for")

    # ── Can they actually do anything about *this* role? ──────────────────
    #
    # Authority is not a property of a title on its own. A finance manager has every bit
    # of the seniority of an engineering manager and none of the ability to hire an ML
    # engineer, so authority is scored *over the function the candidate is targeting*.
    # Without this gate a Finance Manager scored 45/100 for an ML opening.
    small_company = company_size is not None and company_size < 60
    relevant_function = bool(function) and (
        function == "recruiting"
        or function in priorities[:4]
        # At a company this small there is no separate hiring function: the founder is
        # the engineering lead, the recruiter and the decision-maker. Excluding them by
        # title would remove the only person there is to write to.
        or (function == "executive" and small_company)
    )
    if not title:
        observations.append(unknown("authority", "no title, so their authority is unknown"))
    elif not relevant_function:
        observations.append(measured("authority", 0.05))
    else:
        observations.append(measured("authority", 0.9 if authority else 0.25))
        if authority and function == "recruiting":
            reasons.append("hires for this company")
        elif authority:
            reasons.append("senior enough to hire, refer or decide for this team")

    # ── How close are they to the actual opening? ─────────────────────────
    if not job_title:
        observations.append(
            not_applicable("proximity", "no specific opening was named for this comparison")
        )
    elif not title:
        observations.append(unknown("proximity", "no title to compare with the opening"))
    else:
        job_function = function_of(job_title)
        if job_function and job_function == function:
            observations.append(measured("proximity", 1.0))
            reasons.append(f"same function as the {job_title} opening")
        elif function == "recruiting":
            # A recruiter is close to every opening by definition.
            observations.append(measured("proximity", 0.8))
        elif job_function and job_function in priorities and function in priorities:
            distance = abs(priorities.index(job_function) - priorities.index(function))
            observations.append(measured("proximity", max(0.1, 1.0 - distance * 0.25)))
        else:
            observations.append(measured("proximity", 0.15))

    # ── Seniority, calibrated rather than ranked ──────────────────────────
    #
    # And only within a relevant function. Being a senior person in an unrelated part of
    # the company is not a reason to write to them.
    if not title:
        observations.append(unknown("seniority", "no title to read a level from"))
    elif not relevant_function:
        observations.append(measured("seniority", 0.05))
    else:
        if seniority >= 5 and not small_company:
            # A CEO at a large company will not read this and could not act on it.
            value = 0.25 if role_is_junior else 0.5
            reasons.append(
                "very senior for this role — at a company this size they are unlikely "
                "to be the right first contact"
            )
        elif seniority >= 5:
            value = 0.9
            reasons.append("at a company this small, the founder is the hiring manager")
        elif seniority == 4:
            value = 0.7 if role_is_junior else 0.9
        elif seniority in (2, 3):
            # Managers and directors own teams and hire into them.
            value = 0.95
        elif seniority == 1:
            value = 0.55
        else:
            value = 0.3
        observations.append(measured("seniority", value))

    # ── Would they recognise the candidate's work? ────────────────────────
    technical_functions = {"ai_ml", "data", "engineering", "product"}
    if not function:
        observations.append(unknown("technical", "cannot tell whether the role is technical"))
    elif function in technical_functions:
        observations.append(measured("technical", 0.9))
    elif function == "recruiting":
        observations.append(measured("technical", 0.5))
    else:
        observations.append(measured("technical", 0.1))

    # ── Is there a way to reach them that we would use? ───────────────────
    if opted_out:
        observations.append(measured("reachability", 0.0))
        reasons.append("has asked not to be contacted")
    elif not relevant_function and title:
        # A verified address for somebody irrelevant is not a point in their favour.
        observations.append(
            not_applicable("reachability", "they are not in a function you are targeting")
        )
    elif person.contactable_now:
        observations.append(measured("reachability", 1.0))
    elif person.email:
        observations.append(measured("reachability", 0.5))
        reasons.append("an email exists but is not verified — it would need your approval")
    elif person.linkedin:
        observations.append(measured("reachability", 0.45))
        reasons.append("LinkedIn profile only, so any message would be sent by you")
    else:
        observations.append(measured("reachability", 0.05))
        reasons.append("no way to reach them has been found")

    scored = combine(observations, WEIGHTS, reasons=reasons)

    # ── History overrides the arithmetic ──────────────────────────────────
    if opted_out:
        scored.score = 0.0
        recommendation = "Do not contact — they have opted out"
        channel = "none"
    elif replied:
        recommendation = "Already replied — continue that conversation rather than starting one"
        channel = "existing thread"
    elif previously_contacted:
        scored.score = round(scored.score * 0.4, 4)
        recommendation = "Already contacted — wait before following up"
        channel = "follow-up"
    else:
        channel, recommendation = _recommend(person, scored.score, function)

    if scored.provisional and not opted_out:
        scored.reasons.append(
            f"based on {int(scored.evidence_coverage * 100)}% of the usual evidence — "
            "treat the number as provisional"
        )

    return PersonRelevance(
        person=person,
        scored=scored,
        function=function,
        seniority=seniority,
        hiring_authority=authority,
        recommended_channel=channel,
        recommendation=recommendation,
        reasons=scored.reasons,
    )


def _recommend(person: Person, score: float, function: str) -> tuple[str, str]:
    """Which channel, and what to do. Never recommends sending to a guess."""
    if score < 0.45:
        return "none", "Not relevant enough to be worth a message"

    if person.contactable_now:
        if person.linkedin:
            return (
                "email + linkedin",
                "Email them, and connect on LinkedIn with a shorter note",
            )
        return "email", "Email them — the address is published and verified"

    if person.email:
        return (
            "email (needs your approval)",
            "An email exists but is unverified; review it before anything is sent",
        )

    if person.linkedin:
        return (
            "linkedin (you send)",
            "LinkedIn only. The draft is prepared; you send it from your own account.",
        )

    return "none", "No channel has been found for them yet"


def rank(
    people: list[Person],
    *,
    target_role: str,
    job_title: str = "",
    company_size: int | None = None,
    role_is_junior: bool = True,
    contacted: set[str] | None = None,
    replied_to: set[str] | None = None,
    opted_out: set[str] | None = None,
    limit: int = 12,
) -> list[PersonRelevance]:
    """Everyone at a company, best first.

    Capped, because the useful answer is "here are the eight people who matter and why",
    not a list of fifty employees. A list nobody can act on is the same as no list.
    """
    contacted = contacted or set()
    replied_to = replied_to or set()
    opted_out = opted_out or set()

    scored = [
        score_person(
            person,
            target_role=target_role,
            job_title=job_title,
            company_size=company_size,
            role_is_junior=role_is_junior,
            previously_contacted=person.key() in contacted,
            replied=person.key() in replied_to,
            opted_out=person.key() in opted_out,
        )
        for person in people
    ]
    scored.sort(key=lambda r: (-r.score, -r.scored.confidence, r.person.name))
    return scored[:limit]
