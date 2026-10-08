"""Company intelligence.

Answers a different question from job matching: *is this a company worth working
for, given who you are?* That is why `company_fit` is a separate axis in the
ranking (docs/01-architecture.md §11) rather than folded into job fit — a perfect
role at a company with no technical relevance to your career is not the same
opportunity as the same role somewhere the work compounds.

Reads only what companies publish for reading: the home page and the careers
page, robots.txt honoured, a handful of requests per company. No enrichment
vendors, no scraping of profiles, no compiling of personal data.

Every field degrades to unknown, and unknown is *excluded* from the fit score
rather than scored zero — an unresearched company must not rank below a
researched bad one (docs/01-architecture.md §11, the same rule the opportunity
scorer applies).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx

from .agents.trust import harden
from .config import get_settings
from .logging import get_logger
from .models.candidate import Candidate
from .models.evidence import (
    Observation,
    Scored,
    combine,
    measured,
    not_applicable,
    unknown,
)
from .prompts import get as get_prompt
from .resume.taxonomy import ALIAS_TO_CANONICAL, AMBIGUOUS
from .sources.ats.discover import _robots_allows, domain_of

log = get_logger("company")

PAGES = ("/", "/about", "/about-us", "/careers", "/jobs", "/company")
MAX_PAGES = 3
MAX_HTML = 400_000

# Below this much readable text, the pages were almost certainly a JavaScript shell
# rather than content. Measured against real sites: a server-rendered marketing page
# yields thousands of characters, and a client-rendered one yields a few dozen.
#
# The distinction matters because "we read the site and it does no AI work" and "we
# could not read the site" must not produce the same score. Treating the second as the
# first is the same silent-wrong failure as a wrong ATS slug returning an empty list.
MIN_INFORMATIVE_CHARS = 800

# Size bands inferred from language companies use about themselves. Coarse on
# purpose: a precise headcount is not knowable from a home page, and a made-up
# one is worse than a band.
SIZE_MARKERS: tuple[tuple[str, str], ...] = (
    ("enterprise", r"fortune \d00|\b\d{2,3},000\+? employees|global leader in"),
    ("large", r"\b(\d,\d00|[2-9]\d00)\+? (employees|people)\b|offices in \d+ countries"),
    ("mid", r"\b[1-9]\d0\+? (employees|people)\b|series [cd]\b"),
    (
        "startup",
        r"\b(seed|series a|series b|early[- ]stage|founded in 202\d)\b|\byc\s?[wsu]?\d{2}\b",
    ),
)

INDUSTRY_MARKERS: tuple[tuple[str, str], ...] = (
    ("ai/ml", r"\b(artificial intelligence|machine learning|deep learning|foundation model|llm)\b"),
    ("fintech", r"\b(fintech|payments?|lending|banking|insurtech|trading)\b"),
    ("healthtech", r"\b(healthcare|clinical|medical|diagnostics?|patient|biotech|pharma)\b"),
    ("ecommerce", r"\b(e-?commerce|marketplace|retail|d2c|shopping)\b"),
    ("saas", r"\b(saas|b2b software|platform for teams|workflow automation)\b"),
    ("devtools", r"\b(developer (tools|platform|experience)|api[- ]first|sdk|open source)\b"),
    ("data", r"\b(data (platform|warehouse|infrastructure|pipeline)|analytics platform|bi tool)\b"),
    ("security", r"\b(cyber ?security|threat detection|zero trust|vulnerability)\b"),
    ("edtech", r"\b(edtech|learning platform|students?|курс|upskilling)\b"),
    ("logistics", r"\b(logistics|supply chain|freight|last[- ]mile|fleet)\b"),
    ("gaming", r"\b(gaming|game studio|esports)\b"),
    ("consulting", r"\b(consult(ing|ancy)|systems integrator|staffing|talent solutions)\b"),
)

# Signals that a company does real engineering rather than reselling it. Each is a
# weak indicator; together they separate a product company from a body shop,
# which matters a great deal to an early-career engineer and is rarely stated.
ENGINEERING_SIGNALS: tuple[tuple[str, str], ...] = (
    (
        "engineering_blog",
        r"(engineering|tech)\.?\s*blog|blog\.[a-z0-9-]+\.(com|io|dev)|/engineering/",
    ),
    ("open_source", r"github\.com/[a-z0-9-]+(?![a-z0-9-])"),
    ("research", r"\b(research (team|lab|papers?)|arxiv|publications?)\b"),
    ("docs", r"\b(api (docs|reference|documentation)|developer docs)\b"),
    ("hiring_engineers", r"\b(we'?re hiring|open roles?|join (our|the) team)\b"),
)

# Consultancy and staffing language. Not a disqualifier — it is recorded, and the
# user decides. Named plainly because an early-career candidate is the person most
# likely to be routed into one without realising.
BODY_SHOP = (
    r"\b(staff(ing|ed) (solutions|services)|manpower|resource augmentation|"
    r"bench strength|c2c|contract[- ]to[- ]hire|placement (services|assistance)|"
    r"pay (a )?fee|training and placement)\b"
)


@dataclass
class CompanyProfile:
    """What is known about a company. Every field may legitimately be unknown."""

    name: str
    domain: str | None = None
    website: str | None = None
    description: str = ""
    industry: str | None = None
    size_band: str | None = None
    hq_location: str | None = None
    tech_focus: list[str] = field(default_factory=list)
    signals: dict[str, Any] = field(default_factory=dict)
    careers_url: str | None = None
    # How much readable text the pages actually yielded. Zero or near-zero means a
    # JavaScript shell, and the fit score excludes what it cannot see.
    readable_chars: int = 0
    ai_relevance: int | None = None
    fit_score: float | None = None
    fit_reasons: list[str] = field(default_factory=list)
    researched_at: datetime | None = None
    error: str = ""

    @property
    def researched(self) -> bool:
        return self.researched_at is not None and not self.error

    def notes_for_prompt(self) -> str:
        """The only company detail a generator is allowed to use.

        Deliberately assembled from fetched text rather than model recall: this is
        what stops a cover letter praising a product the company never shipped.
        """
        if not self.researched:
            return ""
        bits = [self.description[:400]]
        if self.industry:
            bits.append(f"Industry: {self.industry}.")
        if self.tech_focus:
            bits.append(f"Technology mentioned on their site: {', '.join(self.tech_focus[:10])}.")
        if self.size_band:
            bits.append(f"Apparent size: {self.size_band}.")
        return " ".join(b for b in bits if b).strip()

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "domain": self.domain,
            "website": self.website,
            "description": self.description,
            "industry": self.industry,
            "size_band": self.size_band,
            "hq_location": self.hq_location,
            "tech_focus": list(self.tech_focus),
            "signals": dict(self.signals),
            "careers_url": self.careers_url,
            "readable_chars": self.readable_chars,
            "ai_relevance": self.ai_relevance,
            "fit_score": self.fit_score,
            "fit_reasons": list(self.fit_reasons),
            "researched_at": self.researched_at.isoformat() if self.researched_at else None,
            "error": self.error,
        }


# ─── Text extraction ────────────────────────────────────────────────────────


def _strip_html(html: str) -> str:
    text = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html[:MAX_HTML])
    text = re.sub(r"(?s)<!--.*?-->", " ", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = (
        text.replace("&amp;", "&")
        .replace("&nbsp;", " ")
        .replace("&#39;", "'")
        .replace("&quot;", '"')
        .replace("&lt;", "<")
        .replace("&gt;", ">")
    )
    return " ".join(text.split())


def meta_description(html: str) -> str:
    """The company's own one-line self-description, which beats any summary of it."""
    for pattern in (
        r'<meta[^>]+(?:name|property)=["\'](?:og:)?description["\'][^>]+content=["\']([^"\']{20,400})',
        r'<meta[^>]+content=["\']([^"\']{20,400})["\'][^>]+(?:name|property)=["\'](?:og:)?description',
    ):
        if match := re.search(pattern, html[:MAX_HTML], re.IGNORECASE):
            return " ".join(match.group(1).split())
    return ""


def first_heading(html: str) -> str:
    if match := re.search(r"(?is)<h1[^>]*>(.*?)</h1>", html[:MAX_HTML]):
        return _strip_html(match.group(1))[:200]
    return ""


def tech_mentioned(text: str) -> list[str]:
    """Technologies the company names on its own site, from the known taxonomy.

    Taxonomy-bounded for the same reason resume extraction is: a free-text guess
    invents plausible-looking technology that then feeds a fit score.
    """
    lowered = f" {text.lower()} "
    found: list[str] = []
    for alias, canonical in ALIAS_TO_CANONICAL.items():
        if alias in AMBIGUOUS or len(alias) < 3:
            continue
        if re.search(rf"(?<![\w#+.]){re.escape(alias)}(?![\w#+])", lowered):
            found.append(canonical)
    return sorted(set(found))


def classify(text: str) -> tuple[str | None, str | None, dict[str, Any]]:
    """Industry, size band and engineering signals from page text."""
    lowered = text.lower()

    industry = next(
        (name for name, pattern in INDUSTRY_MARKERS if re.search(pattern, lowered)), None
    )
    size = next((band for band, pattern in SIZE_MARKERS if re.search(pattern, lowered)), None)

    signals: dict[str, Any] = {
        name: bool(re.search(pattern, lowered)) for name, pattern in ENGINEERING_SIGNALS
    }
    if match := re.search(BODY_SHOP, lowered):
        signals["staffing_language"] = match.group(0)
    return industry, size, signals


def ai_relevance(tech: list[str], text: str) -> int:
    """0-100: how much of this company's stated work is AI/ML.

    A count of AI/ML technologies mentioned, bounded. Crude and honest — the
    alternative is asking a model how "AI-focused" a company is, which returns a
    confident number derived from nothing.
    """
    ai_terms = {
        "pytorch",
        "tensorflow",
        "keras",
        "scikit-learn",
        "transformers",
        "hugging face",
        "llm",
        "rag",
        "nlp",
        "computer vision",
        "langchain",
        "langgraph",
        "ai agents",
        "fine-tuning",
        "bert",
        "whisper",
        "opencv",
        "xgboost",
        "mlflow",
        "onnx",
        "jax",
        "deep learning",
        "machine learning",
    }
    hits = len(ai_terms & set(tech))
    lowered = text.lower()
    prose = sum(
        1
        for phrase in (
            "machine learning",
            "artificial intelligence",
            "deep learning",
            "neural network",
            "ai model",
        )
        if phrase in lowered
    )
    return min(100, hits * 12 + prose * 8)


# ─── Fetching ───────────────────────────────────────────────────────────────


async def research(
    name: str,
    domain: str | None = None,
    *,
    client: httpx.AsyncClient | None = None,
    candidate: Candidate | None = None,
    use_apollo: bool = True,
    session: Any = None,
) -> CompanyProfile:
    """Read a company's public pages and build a profile.

    Returns a profile with `error` set rather than raising: a company whose site is
    down must not fail a discovery run, and "not researched" is a valid state the
    ranking already handles.

    `use_apollo` supplements the read with Apollo's own record of the company. It fills
    gaps and never overrides: what a company publishes about itself is the better source
    for what it claims to be, and a third-party database can be years stale. Its real
    value is the case the web read cannot serve at all — a JavaScript-only site yields
    no readable text, which today means a fit score of zero for a company that might be
    an excellent match.
    """
    settings = get_settings()
    profile = CompanyProfile(name=name, domain=domain)

    if not domain:
        profile.error = "no domain known"
        return profile

    host = domain_of(domain)
    origin = f"https://{host}"
    profile.website = origin
    profile.domain = host

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
            headers={"user-agent": settings.user_agent, "accept": "text/html,*/*"},
        )

    collected: list[str] = []
    try:
        fetched = 0
        for path in PAGES:
            if fetched >= MAX_PAGES:
                break
            if settings.respect_robots_txt and not await _robots_allows(
                client, origin, path, settings.http_timeout_seconds
            ):
                log.debug("company_robots_disallowed", host=host, path=path)
                continue
            try:
                response = await client.get(f"{origin}{path}")
            except httpx.HTTPError as exc:
                if path == "/":
                    profile.error = f"{type(exc).__name__}"
                continue
            if response.status_code >= 400 or "html" not in response.headers.get(
                "content-type", ""
            ):
                continue

            html = response.text
            fetched += 1
            collected.append(_strip_html(html))

            if path == "/":
                profile.description = meta_description(html) or first_heading(html)
                profile.error = ""
            if path in {"/careers", "/jobs"} and not profile.careers_url:
                profile.careers_url = f"{origin}{path}"
    finally:
        if owns_client:
            await client.aclose()

    apollo_company = None
    if use_apollo:
        apollo_company = await _apollo_supplement(profile, session=session)

    if not collected:
        profile.error = profile.error or "no readable pages"
        # Apollo alone is enough to score a company whose site cannot be read. Said
        # plainly in the signals so the user knows the profile is second-hand.
        if apollo_company is not None and apollo_company.ok:
            profile.researched_at = datetime.now(UTC)
            profile.error = ""
            profile.signals["read_quality"] = "apollo_only"
            if candidate is not None:
                profile.fit_score, profile.fit_reasons = score_fit(profile, candidate)
                profile.fit_reasons.insert(
                    0, "their own site could not be read; this is from Apollo's record"
                )
        return profile

    text = " ".join(collected)[:120_000]
    profile.tech_focus = tech_mentioned(text)
    classified_industry, classified_size, signals = classify(text)
    profile.signals = {**signals, **profile.signals}
    profile.industry = profile.industry or classified_industry
    profile.size_band = profile.size_band or classified_size
    profile.hq_location = profile.hq_location or _guess_location(text)

    # An uninformative read yields no AI relevance rather than a zero, and the
    # content-dependent signals are dropped instead of read as absences.
    profile.readable_chars = len(text)
    if len(text) < MIN_INFORMATIVE_CHARS:
        # Replace the *content-derived* signals, which are meaningless when there was no
        # content, while keeping anything that did not come from the page. Assigning a
        # fresh dict here used to erase Apollo's record along with them, which silently
        # dropped the strongest fit component for exactly the companies whose sites are
        # JavaScript shells — Swiggy and Meesho among them.
        preserved = {
            key: value
            for key, value in (profile.signals or {}).items()
            if key in {"apollo", "apollo_error"}
        }
        profile.signals = {
            "read_quality": "uninformative",
            "readable_chars": len(text),
            **preserved,
        }
        profile.ai_relevance = None
        log.info("company_read_uninformative", company=name, domain=host, chars=len(text))
    else:
        profile.ai_relevance = ai_relevance(profile.tech_focus, text)
    if not profile.description:
        profile.description = text[:300]
    profile.researched_at = datetime.now(UTC)
    profile.error = ""

    if candidate is not None:
        profile.fit_score, profile.fit_reasons = score_fit(profile, candidate)

    log.info(
        "company_researched",
        company=name,
        domain=host,
        industry=profile.industry,
        tech=len(profile.tech_focus),
        ai_relevance=profile.ai_relevance,
        fit=profile.fit_score,
    )
    return profile


# ─── Apollo's record of the company ─────────────────────────────────────────

# Apollo's department names for the functions a data/ML candidate is aiming at. A company
# with 519 people in data science is hiring for this work whatever its marketing says.
TARGET_DEPARTMENTS = (
    "data_science",
    "engineering",
    "information_technology",
    "product_management",
)


async def _apollo_supplement(profile: CompanyProfile, session: Any = None) -> Any:
    """Fill gaps in a profile from Apollo, recording where each fact came from.

    Gaps only. A field the company's own site supplied is left alone: the site is the
    better authority on what a company says it is, and Apollo's record can be years old.
    Returns the Apollo record, or None when there is nothing to add.
    """
    from .sources import apollo

    if not apollo.configured() or not profile.domain:
        return None

    company = await apollo.enrich(profile.domain, session=session)
    if not company.ok:
        if company.error:
            profile.signals["apollo_error"] = company.error
        return None

    filled: list[str] = []

    def fill(field_name: str, value: Any) -> None:
        if value and not getattr(profile, field_name, None):
            setattr(profile, field_name, value)
            filled.append(field_name)

    fill("industry", company.industry)
    fill("size_band", company.size_band)
    fill("description", company.description)
    fill("hq_location", company.city or company.country)

    # Technology is a union rather than a gap-fill: the two sources genuinely disagree
    # about what a company uses, and both are evidence.
    if company.technologies:
        known = {t.lower() for t in profile.tech_focus}
        from .resume.taxonomy import ALIAS_TO_CANONICAL

        for name in company.technologies:
            canonical = ALIAS_TO_CANONICAL.get(name.lower().strip())
            if canonical and canonical not in known:
                profile.tech_focus.append(canonical)
                known.add(canonical)
        filled.append("tech_focus")

    profile.signals["apollo"] = {
        "employees": company.employees,
        "founded_year": company.founded_year,
        "headcount_by_department": company.headcount_by_department,
        "headcount_growth_12m": company.headcount_growth_12m,
        "latest_funding_stage": company.latest_funding_stage,
        "relevant_headcount": company.relevant_headcount(TARGET_DEPARTMENTS),
        "linkedin_url": company.linkedin_url,
        # Named so a reader can tell a second-hand fact from a first-hand one.
        "filled": filled,
    }
    log.info("company_apollo_supplement", company=profile.name, filled=filled)
    return company


_LOCATION = re.compile(
    r"\b(Bangalore|Bengaluru|Chennai|Coimbatore|Hyderabad|Pune|Mumbai|Delhi|Noida|Gurgaon|"
    r"Gurugram|Kolkata|Ahmedabad|Kochi|Trivandrum|Jaipur|Indore|London|Berlin|Amsterdam|"
    r"Dublin|Paris|Singapore|Sydney|Toronto|New York|San Francisco|Seattle|Austin|Boston|Dubai)\b"
)


# One city, two spellings. Apollo says "Bengaluru", the candidate's preferences say
# "Bangalore", and comparing the strings directly scored a Bangalore company as being in
# the wrong place. Same for the Bombay/Mumbai and Gurgaon/Gurugram pairs.
CITY_ALIASES = {
    "bengaluru": "bangalore",
    "bombay": "mumbai",
    "gurugram": "gurgaon",
    "madras": "chennai",
    "calcutta": "kolkata",
    "trivandrum": "thiruvananthapuram",
    "new delhi": "delhi",
    "ncr": "delhi",
    "sf": "san francisco",
    "nyc": "new york",
    "bay area": "san francisco",
}


def _canonical_city(name: str) -> str:
    """One spelling per city, for comparison only. Never shown to the user."""
    text = (name or "").strip().lower()
    if not text:
        return ""
    # A preference may be written as "Bangalore, India"; the city is the part that matters.
    head = text.split(",")[0].strip()
    return CITY_ALIASES.get(head, head)


def _guess_location(text: str) -> str | None:
    """The most-mentioned known city. A guess, and labelled as one by being the
    only location field — no confident precision where there is none."""
    counts: dict[str, int] = {}
    for match in _LOCATION.finditer(text):
        city = match.group(1)
        counts[city] = counts.get(city, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda pair: pair[1])[0]


# ─── Company fit ────────────────────────────────────────────────────────────

# Components and their share of company fit. Only components with evidence
# participate; the rest give up their weight.
# Below this many known technologies, an absence of overlap says more about the page we
# read than about the company. Above it, a miss is real evidence.
MIN_TECH_FOR_ABSENCE = 8

FIT_WEIGHTS = {
    "tech_overlap": 0.35,
    "ai_relevance": 0.18,
    "engineering_culture": 0.17,
    # How many people already do the work the candidate wants to do. The strongest single
    # signal available and the only one that is a count rather than an inference: a company
    # with 519 people in data science hires data people, whatever its home page says.
    "relevant_team": 0.20,
    "location": 0.05,
    "industry": 0.05,
}


def observe_fit(
    profile: CompanyProfile, candidate: Candidate, preferred_industries: list[str] | None = None
) -> Scored:
    """How well this company suits this candidate, with the provenance of the answer.

    Every component is an `Observation` rather than a bare number, so a question we could
    not answer stays *unanswered* instead of becoming a zero. That distinction is not
    pedantry: scoring an unread website as 0.0 put Swiggy at the bottom of the ranking
    until Apollo filled the gap and moved it to 0.87, and the same mistake in the
    technology component made the heaviest weight score 0.13 across 63 real companies.

    Returns a score carrying its own confidence and evidence coverage, so a caller can
    tell a 0.9 built on one signal from a 0.9 built on six.
    """
    observations: list[Observation] = []
    reasons: list[str] = []

    # -- Technology overlap ------------------------------------------------
    skills = {s.lower() for s in candidate.skill_labels()}
    known_tech = set(profile.tech_focus)
    if not known_tech:
        observations.append(unknown("tech_overlap", "no technology is recorded for them"))
    else:
        overlap = skills & known_tech
        # The denominator is what they could plausibly have overlapped on, not a flat
        # five: a company that names three technologies and you have all three is a
        # complete match on the evidence available.
        reachable = min(len(known_tech), 5)
        if len(known_tech) < MIN_TECH_FOR_ABSENCE and not overlap:
            # Two or three buzzwords that miss says something about the page we read,
            # not about the company.
            observations.append(
                unknown(
                    "tech_overlap",
                    f"only {len(known_tech)} technolog"
                    f"{'y is' if len(known_tech) == 1 else 'ies are'} on record, "
                    "too few to judge an overlap",
                )
            )
            reasons.append(
                f"their site names only {len(known_tech)} technolog"
                f"{'y' if len(known_tech) == 1 else 'ies'}, too few to judge the overlap"
            )
        else:
            observations.append(measured("tech_overlap", len(overlap) / max(1, reachable)))
            if overlap:
                reasons.append(
                    f"uses {len(overlap)} of the {len(known_tech)} technologies named "
                    f"there that you have: " + ", ".join(sorted(overlap)[:5])
                )
            else:
                reasons.append(
                    f"none of the {len(known_tech)} technologies on record there overlap "
                    "with your skills"
                )

    # -- AI relevance ------------------------------------------------------
    if profile.ai_relevance is None:
        observations.append(
            unknown("ai_relevance", "not enough readable text to assess their focus")
        )
    else:
        observations.append(measured("ai_relevance", profile.ai_relevance / 100.0))
        if profile.ai_relevance >= 40:
            reasons.append(f"substantial AI/ML focus ({profile.ai_relevance}/100)")

    # -- Engineering culture -----------------------------------------------
    signals = profile.signals or {}
    if not signals or signals.get("read_quality") in {"uninformative", "apollo_only"}:
        observations.append(
            unknown(
                "engineering_culture",
                "their site rendered nothing readable, so its public engineering "
                "signals could not be seen",
            )
        )
    else:
        positives = sum(
            1 for key in ("engineering_blog", "open_source", "research", "docs") if signals.get(key)
        )
        culture = positives / 4.0
        if signals.get("staffing_language"):
            # A body shop can still be a fine first job; it is a different job, and the
            # user should see the label rather than a silently lower score.
            culture = max(0.0, culture - 0.5)
            reasons.append(
                f"staffing/consultancy language on their site: {signals['staffing_language']!r}"
            )
        observations.append(measured("engineering_culture", culture))
        if positives >= 2:
            reasons.append(f"{positives} public engineering signals (blog, open source, docs)")

    # -- Relevant team size ------------------------------------------------
    # Scored on a curve rather than linearly: the gap between 0 and 20 people doing this
    # work matters far more than the gap between 500 and 2,000.
    apollo_signals = signals.get("apollo") or {}
    relevant = apollo_signals.get("relevant_headcount")
    if not isinstance(relevant, int):
        observations.append(
            unknown("relevant_team", "no headcount by function is available for them")
        )
    else:
        for floor, value in ((500, 1.0), (100, 0.85), (25, 0.65), (5, 0.45), (1, 0.25)):
            if relevant >= floor:
                observations.append(measured("relevant_team", value))
                break
        else:
            observations.append(measured("relevant_team", 0.1))

        if relevant >= 25:
            reasons.append(f"{relevant} people already work in engineering and data roles there")
        elif relevant == 0:
            reasons.append("no engineering or data headcount on record")

        growth = apollo_signals.get("headcount_growth_12m")
        if isinstance(growth, int | float) and growth > 0.05:
            reasons.append(f"headcount grew {growth * 100:.0f}% over the last year")

    # -- Location ----------------------------------------------------------
    if not profile.hq_location:
        observations.append(unknown("location", "we do not know where they are based"))
    else:
        preferred = {_canonical_city(p) for p in candidate.preferred_locations if p} | {
            _canonical_city(candidate.location)
        }
        here = _canonical_city(profile.hq_location)
        matched = bool(here) and any(here in p or p in here for p in preferred if p)
        observations.append(measured("location", 1.0 if matched else 0.35))
        if matched:
            reasons.append(f"based in {profile.hq_location}, which you prefer")

    # -- Industry ----------------------------------------------------------
    if not preferred_industries:
        # Not a gap in our knowledge -- the candidate stated no preference, so there is
        # nothing to be right or wrong about.
        observations.append(
            not_applicable("industry", "you have not named any preferred industries")
        )
    elif not profile.industry:
        observations.append(unknown("industry", "their industry could not be determined"))
    else:
        wanted = {i.lower() for i in preferred_industries}
        observations.append(
            measured("industry", 1.0 if profile.industry.lower() in wanted else 0.4)
        )

    result = combine(observations, FIT_WEIGHTS, reasons=reasons)

    # Appended to the result, not to the local list: `combine` copies what it is given,
    # so a later append to `reasons` would never reach the caller.
    if result.confidence == 0.0:
        result.reasons.append(
            "nothing about them could be assessed — this is 'not assessed', not 'a poor fit'"
        )
    elif result.provisional:
        result.reasons.append(
            f"based on {int(result.evidence_coverage * 100)}% of the usual evidence "
            f"(missing {', '.join(result.unknown_fields)}) — treat it as provisional"
        )

    # Kept on the profile so the dashboard and the ranking can both see how much is behind
    # the number without recomputing it.
    profile.signals["fit_coverage"] = result.evidence_coverage
    profile.signals["fit_confidence"] = result.confidence
    profile.signals["fit_unknown"] = result.unknown_fields
    return result


def score_fit(
    profile: CompanyProfile, candidate: Candidate, preferred_industries: list[str] | None = None
) -> tuple[float, list[str]]:
    """The score and its reasons, for callers that want only those.

    `observe_fit` is the full answer; this is the two fields most callers need. Both run
    the same code, so they cannot drift apart.
    """
    result = observe_fit(profile, candidate, preferred_industries)
    return (round(result.score, 4), result.reasons)


# ─── Optional LLM summary ───────────────────────────────────────────────────

SUMMARY = get_prompt("company_summary")


def summarize(profile: CompanyProfile, text: str, router: Any = None) -> dict[str, Any] | None:
    """A readable summary of already-fetched text. Adds no facts of its own."""
    if router is None:
        from .llm.router import get_router

        router = get_router()
    if not router.available():
        return None

    from .llm.router import Tier, tokens_for

    data = router.complete_json(
        SUMMARY.render(name=profile.name, text=" ".join(text.split())[:4000]),
        tier=Tier.CHEAP,
        system=harden(SUMMARY.system),
        temperature=0.0,
        # A few sentences and three short lists.
        max_tokens=tokens_for(400),
        agent="company_research",
    )
    return data if isinstance(data, dict) else None
