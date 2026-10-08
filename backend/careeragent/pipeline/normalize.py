"""Normalization: RawJob → NormalizedJob.

Pure and total. Sources differ wildly in date formats, location granularity and
employment-type vocabulary; this is the one place that variance is resolved, so
it is also the one place worth testing exhaustively.

A job is never dropped for being malformed — a missing date or unparseable
location downgrades a field to UNKNOWN rather than discarding a real opening.
"""

from __future__ import annotations

import re
from datetime import UTC, date, datetime

from dateutil import parser as date_parser

from ..logging import get_logger
from ..models.job import (
    JobListing,
    NormalizedJob,
    RawJob,
    Salary,
    detect_employment_type,
    detect_seniority,
    detect_work_mode,
    extract_years_experience,
    normalize_title,
)

log = get_logger("pipeline.normalize")

# "Posted Yesterday" / "Posted 3 Days Ago" — Workday's listing gives these
# instead of a date. Better to derive an approximate date than to throw away
# freshness entirely, but the result is marked approximate by being derived.
_RELATIVE = re.compile(
    r"(?:posted\s+)?(?:(today)|(yesterday)|(\d+)\+?\s*(day|week|month)s?\s*ago)", re.IGNORECASE
)

_SALARY = re.compile(
    r"(?P<cur>[$₹€£]|USD|INR|EUR|GBP)?\s*"
    r"(?P<min>\d[\d,.]*)\s*(?:k|K)?\s*(?:-|–|to)\s*(?P<cur2>[$₹€£])?\s*(?P<max>\d[\d,.]*)\s*(?:k|K)?"
)

_CURRENCY_SYMBOLS = {"$": "USD", "₹": "INR", "€": "EUR", "£": "GBP"}


def parse_posted_date(raw: RawJob) -> date | None:
    """Resolve a posting date from whatever the source gave us."""
    for candidate in (raw.published_at, raw.updated_at):
        if not candidate:
            continue
        try:
            parsed = date_parser.parse(candidate)
        except (ValueError, OverflowError, TypeError):
            continue
        result = parsed.date()
        # A source occasionally emits an epoch-0 or far-future date; neither is
        # a real posting date and both poison freshness scoring.
        if date(2000, 1, 1) <= result <= datetime.now(UTC).date():
            return result

    if raw.posted_label and (m := _RELATIVE.search(raw.posted_label)):
        today = datetime.now(UTC).date()
        if m.group(1):
            return today
        if m.group(2):
            return date.fromordinal(today.toordinal() - 1)
        if m.group(3):
            n, unit = int(m.group(3)), m.group(4).lower()
            days = {"day": 1, "week": 7, "month": 30}[unit] * n
            return date.fromordinal(max(today.toordinal() - days, 1))
    return None


def parse_salary(text: str) -> Salary | None:
    if not text:
        return None
    if not (m := _SALARY.search(text)):
        return Salary(raw=text)

    def to_float(value: str) -> float | None:
        try:
            return float(value.replace(",", ""))
        except ValueError:
            return None

    low, high = to_float(m.group("min")), to_float(m.group("max"))
    # A "60k-90k" style range: the k is outside the captured digits.
    if low is not None and high is not None and "k" in text.lower() and high < 1000:
        low, high = low * 1000, high * 1000

    symbol = m.group("cur") or m.group("cur2") or ""
    currency = _CURRENCY_SYMBOLS.get(symbol, symbol.upper() or None)
    return Salary(min=low, max=high, currency=currency, raw=text)


# ATS host suffixes that appear inside a board slug. Stripped so a Workday slug like
# `quantiphi.wd1.myworkdayjobs.com::Careers_at_Quantiphi` yields "Quantiphi" rather than
# becoming the company name in full.
_ATS_HOST_SUFFIX = re.compile(
    r"\.(?:wd\d+\.myworkdayjobs\.com|myworkdayjobs\.com|breezy\.hr|bamboohr\.com"
    r"|teamtailor\.com|recruitee\.com|keka\.com|freshteam\.com|personio\.de"
    r"|applytojob\.com|workable\.com)$",
    re.IGNORECASE,
)


def company_from_slug(slug: str) -> str:
    """A readable company name from a board slug.

    The fallback used to be the slug itself, which is fine for Greenhouse ("glance") and
    wrong for Workday, where the slug carries a whole host and site name. That produced
    "quantiphi.wd1.myworkdayjobs.com::Careers_at_Quantiphi" as a company, sitting in the
    rankings beside "Quantiphi" as though they were two different employers.

    Note this feeds `compute_fingerprint`, so changing what it returns changes job ids —
    which is why it strips rather than rewrites, and why the rules here are conservative.
    """
    head = (slug or "").split("::", 1)[0].strip()
    if not head:
        return ""
    head = _ATS_HOST_SUFFIX.sub("", head)
    # Whatever is left of a host is the tenant: `quantiphi.wd1` → `quantiphi`.
    head = head.split(".", 1)[0]
    head = head.replace("-", " ").replace("_", " ").strip()
    # Title-cased only when the slug is clearly a plain lowercase handle; a name that
    # already carries capitals ("BrowserStack") is left as the source wrote it.
    return head.title() if head.islower() else head


def normalize(
    raw: RawJob, company_name: str | None = None, company_domain: str | None = None
) -> NormalizedJob:
    company = company_name or raw.company_name or company_from_slug(raw.board_slug or "")

    work_mode = detect_work_mode(
        raw.location, raw.employment_type, raw.title, raw.description[:2000]
    )
    if raw.remote is True and work_mode.value == "unknown":
        from ..models.job import WorkMode

        work_mode = WorkMode.REMOTE

    min_years, max_years = extract_years_experience(raw.description)

    job = NormalizedJob(
        company=company,
        company_domain=company_domain,
        title=raw.title,
        normalized_title=normalize_title(raw.title),
        location=raw.location,
        work_mode=work_mode,
        employment_type=detect_employment_type(
            raw.employment_type, raw.title, raw.description[:1500]
        ),
        seniority=detect_seniority(raw.title, raw.description),
        salary=parse_salary(raw.salary),
        description=raw.description,
        min_years_experience=min_years,
        max_years_experience=max_years,
        posted_date=parse_posted_date(raw),
        listings=[
            JobListing(
                source=raw.source,
                source_job_id=raw.source_job_id,
                url=raw.url,
                application_url=raw.url,
                ats=raw.source,
                scraped_at=raw.scraped_at,
            )
        ],
        ats=raw.source,
        first_seen_at=raw.scraped_at,
        last_seen_at=raw.scraped_at,
    )
    job.fingerprint = job.compute_fingerprint()
    job.job_id = job.fingerprint
    return job


def normalize_all(
    raws: list[RawJob], company_name: str | None = None, company_domain: str | None = None
) -> list[NormalizedJob]:
    out: list[NormalizedJob] = []
    for raw in raws:
        if not raw.title or not raw.url:
            log.debug("skipped_incomplete_job", source=raw.source, id=raw.source_job_id)
            continue
        out.append(normalize(raw, company_name, company_domain))
    return out
