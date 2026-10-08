"""Readers for each supported ATS's public job-board API.

Every endpoint here is one a company already exposes, without authentication, so
that its own careers page can draw a job list. None of this is HTML scraping: a
markup change cannot break it, and there is no bot protection to be at odds with.

Ported to Python from `shunsukefuruyama/ats-jobs` (MIT). See NOTICE for the
upstream copyright. Endpoint behaviour was re-verified live on 2026-09-12; the
results are recorded in docs/00-research.md §3.

Two traps worth keeping in mind when adding a provider:

  * `empty_means_exists` — several platforms answer 200 with an empty list for a
    board that never existed. An empty result from those proves nothing, and
    treating it as "no openings" silently under-reports.
  * Lever, Rippling and Breezy all return a bare JSON array, so the response
    *shape* cannot identify the platform. Each checks a platform-specific key
    instead. Mistaking one for another yields a pile of jobs with empty titles —
    failing silently and plausibly, the worst way for this to go wrong.
"""

from __future__ import annotations

import html
import re
from typing import Any, ClassVar
from urllib.parse import quote, urlparse

from ...models.job import RawJob

__all__ = ["PROVIDERS", "PROVIDERS_BY_ID", "ATSProvider", "provider_from_host", "slug_variants"]


# ─── Text helpers ───────────────────────────────────────────────────────────

_BR = re.compile(r"<br\s*/?>", re.IGNORECASE)
_BLOCK_END = re.compile(r"</(p|div|h[1-6])>", re.IGNORECASE)
_LI_END = re.compile(r"</li>", re.IGNORECASE)
_LI_START = re.compile(r"<li>", re.IGNORECASE)
_TAG = re.compile(r"<[^>]+>")
_BLANKS = re.compile(r"\n{3,}")


def strip_html(markup: Any) -> str:
    """Reduce HTML to readable text. Most platforms return descriptions as markup.

    Paragraphs and headings become blank-line separated; list items keep one
    break each, or the result is an unreadable wall that the extraction agent
    then has to fight through.
    """
    if not isinstance(markup, str):
        return "" if markup is None else str(markup)
    text = _BR.sub("\n", markup)
    text = _BLOCK_END.sub("\n\n", text)
    text = _LI_END.sub("\n", text)
    text = _LI_START.sub("- ", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    return _BLANKS.sub("\n\n", text).strip()


def _text(value: Any) -> str:
    if value is None:
        return ""
    return value if isinstance(value, str) else str(value)


def _source_id_from_href(href: Any) -> str:
    """The job id out of a careers-page link.

    Takes `Any` because BeautifulSoup returns a list for attributes it considers
    multi-valued, and a link that arrives as a list would otherwise raise on `.strip`.
    """
    text = _text(href).strip("/")
    return text.split("/")[1] if "/" in text else text


def parse_target(value: str | None) -> tuple[str, str, str]:
    """Split an input into (host, path, raw). Never raises on non-URL input."""
    raw = (value or "").strip()
    if not raw:
        return "", "", raw
    try:
        parsed = urlparse(raw if "://" in raw else f"https://{raw}")
        return (parsed.hostname or "").lower(), parsed.path or "", raw
    except ValueError:
        return "", "", raw


def bare_name(host: str) -> str:
    """Reduce a domain to a bare name (example.com → example). Last-resort slug."""
    return re.sub(r"[^a-z0-9-]", "", re.sub(r"^www\.", "", host).split(".")[0])


def slug_variants(base: str) -> list[str]:
    """Slug alternatives derivable from a domain, most likely first.

    datadoghq.com → ['datadoghq', 'datadog'] — the real board is `datadog`.

    Generating variants freely would mean pointless requests to other people's
    APIs, so this covers only the ways slugs have actually been observed to
    differ: a trailing hq/inc, a leading get, and hyphens.
    """
    out = [base]

    def add(value: str) -> None:
        cleaned = value.strip("-")
        if len(cleaned) >= 2 and cleaned not in out:
            out.append(cleaned)

    add(base.replace("-", ""))
    add(re.sub(r"(hq|inc|corp|labs|group|team)$", "", base, flags=re.IGNORECASE))
    add(re.sub(r"^(get|the|join|try)", "", base, flags=re.IGNORECASE))
    return out


# ─── Provider base ──────────────────────────────────────────────────────────


class ATSProvider:
    id: ClassVar[str] = ""
    label: ClassVar[str] = ""
    # Does an empty response prove the board exists? False when the platform
    # answers 200-empty for boards that were never theirs.
    empty_means_exists: ClassVar[bool] = False
    method: ClassVar[str] = "GET"
    page_size: ClassVar[int | None] = None
    # Only used to render a readable example in `careeragent providers`.
    example_slug: ClassVar[str] = "acme"

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, _, raw = parse_target(value)
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        raise NotImplementedError

    @classmethod
    def body(cls, slug: str, offset: int = 0) -> dict[str, Any] | None:
        return None

    @classmethod
    def next_offset(cls, payload: Any, fetched: int) -> int | None:
        return None

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        """Return jobs, or None meaning 'this is not that platform'."""
        raise NotImplementedError

    @classmethod
    def _job(cls, slug: str, **kwargs: Any) -> RawJob:
        kwargs.setdefault("source", cls.id)
        kwargs.setdefault("board_slug", slug)
        return RawJob(**kwargs)


# ─── Providers ──────────────────────────────────────────────────────────────


class Greenhouse(ATSProvider):
    id = "greenhouse"
    label = "Greenhouse"
    empty_means_exists = True  # 404s for boards that do not exist

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, path, raw = parse_target(value)
        if host.endswith("greenhouse.io"):
            if m := re.search(r"[?&]for=([a-z0-9_-]+)", raw, re.IGNORECASE):
                return m.group(1)
            seg = [s for s in path.split("/") if s]
            if seg:
                return seg[-2] if seg[-1] == "jobs" and len(seg) > 1 else seg[0]
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        query = "?content=true" if include_description else ""
        return f"https://boards-api.greenhouse.io/v1/boards/{quote(slug)}/jobs{query}"

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return None
        out = []
        for j in jobs:
            location = _text((j.get("location") or {}).get("name"))
            out.append(
                cls._job(
                    slug,
                    source_job_id=_text(j.get("id")),
                    title=_text(j.get("title")),
                    url=_text(j.get("absolute_url")),
                    location=location,
                    department=", ".join(
                        _text(d.get("name")) for d in (j.get("departments") or []) if d.get("name")
                    ),
                    team=", ".join(
                        _text(o.get("name")) for o in (j.get("offices") or []) if o.get("name")
                    ),
                    remote=True if re.search(r"remote", location, re.I) else None,
                    published_at=_text(j.get("first_published") or j.get("updated_at")) or None,
                    updated_at=_text(j.get("updated_at")) or None,
                    description=strip_html(j.get("content")) if j.get("content") else "",
                )
            )
        return out


class Lever(ATSProvider):
    id = "lever"
    label = "Lever"
    # Returns 200 and [] even for companies that were never theirs.
    empty_means_exists = False

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, path, raw = parse_target(value)
        if host.endswith("lever.co") and (seg := [s for s in path.split("/") if s]):
            return seg[0]
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        return f"https://api.lever.co/v0/postings/{quote(slug)}?mode=json"

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        if not isinstance(payload, list):
            return None
        # Rippling also answers with a bare array, so shape alone proves nothing.
        if payload and not ("text" in payload[0] and "hostedUrl" in payload[0]):
            return None
        out = []
        for j in payload:
            cats = j.get("categories") or {}
            location = _text(cats.get("location"))
            out.append(
                cls._job(
                    slug,
                    source_job_id=_text(j.get("id")),
                    title=_text(j.get("text")),
                    url=_text(j.get("hostedUrl") or j.get("applyUrl")),
                    location=location,
                    department=_text(cats.get("department")),
                    team=_text(cats.get("team")),
                    employment_type=_text(cats.get("commitment")),
                    remote=True
                    if re.search(r"remote", _text(j.get("workplaceType")) + location, re.I)
                    else None,
                    published_at=_epoch_ms_to_iso(j.get("createdAt")),
                    description=strip_html(j.get("descriptionPlain") or j.get("description") or ""),
                )
            )
        return out


class Ashby(ATSProvider):
    id = "ashby"
    label = "Ashby"
    empty_means_exists = True  # 404s for boards that do not exist

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, path, raw = parse_target(value)
        if host.endswith("ashbyhq.com") and (seg := [s for s in path.split("/") if s]):
            return seg[0]
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        query = "?includeCompensation=true" if include_description else ""
        return f"https://api.ashbyhq.com/posting-api/job-board/{quote(slug)}{query}"

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return None
        return [
            cls._job(
                slug,
                source_job_id=_text(j.get("id")),
                title=_text(j.get("title")).strip(),
                url=_text(j.get("jobUrl") or j.get("applyUrl")),
                location=_text(j.get("location")),
                department=_text(j.get("department")),
                team=_text(j.get("team")),
                employment_type=_text(j.get("employmentType")),
                remote=j.get("isRemote") if isinstance(j.get("isRemote"), bool) else None,
                published_at=_text(j.get("publishedAt")) or None,
                updated_at=_text(j.get("updatedAt")) or None,
                description=strip_html(j.get("descriptionPlain") or j.get("descriptionHtml") or ""),
            )
            for j in jobs
        ]


class SmartRecruiters(ATSProvider):
    id = "smartrecruiters"
    label = "SmartRecruiters"
    empty_means_exists = False  # 200 with content:[] even for unknown companies
    page_size = 100

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, path, raw = parse_target(value)
        if "smartrecruiters.com" in host and (seg := [s for s in path.split("/") if s]):
            return seg[0]
        # SmartRecruiters identifiers are usually CamelCase; pass through as-is.
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        return (
            f"https://api.smartrecruiters.com/v1/companies/{quote(slug)}"
            f"/postings?limit=100&offset={offset}"
        )

    @classmethod
    def next_offset(cls, payload: Any, fetched: int) -> int | None:
        # 100 per page is the cap. Stopping silently at 100 would hand back a
        # large employer's board with rows missing — wrong, and quiet about it.
        total = (payload or {}).get("totalFound", 0) if isinstance(payload, dict) else 0
        return fetched if fetched < total else None

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        jobs = payload.get("content") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return None
        out = []
        for j in jobs:
            loc = j.get("location") or {}
            out.append(
                cls._job(
                    slug,
                    source_job_id=_text(j.get("id")),
                    title=_text(j.get("name")),
                    url=_text(j.get("ref") or j.get("applyUrl"))
                    or f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
                    location=", ".join(
                        p for p in (_text(loc.get("city")), _text(loc.get("country"))) if p
                    ),
                    department=_text((j.get("department") or {}).get("label")),
                    team=_text((j.get("function") or {}).get("label")),
                    employment_type=_text((j.get("typeOfEmployment") or {}).get("label")),
                    remote=loc.get("remote") if isinstance(loc.get("remote"), bool) else None,
                    published_at=_text(j.get("releasedDate")) or None,
                )
            )
        return out


class Rippling(ATSProvider):
    id = "rippling"
    label = "Rippling"
    empty_means_exists = True

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, path, raw = parse_target(value)
        # ats.rippling.com/<slug>/jobs/<uuid>
        if "rippling.com" in host and (seg := [s for s in path.split("/") if s]):
            return seg[0]
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        return f"https://api.rippling.com/platform/api/ats/v1/board/{quote(slug)}/jobs"

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        if not isinstance(payload, list):
            return None
        # Lever also returns a bare array; check a Rippling-specific key.
        if payload and not ("uuid" in payload[0] and "name" in payload[0]):
            return None
        out = []
        for j in payload:
            location = _text((j.get("workLocation") or {}).get("label"))
            out.append(
                cls._job(
                    slug,
                    source_job_id=_text(j.get("uuid")),
                    title=_text(j.get("name")),
                    url=_text(j.get("url")),
                    location=location,
                    department=_text((j.get("department") or {}).get("label")),
                    employment_type=_text((j.get("employmentType") or {}).get("label")),
                    remote=True if re.search(r"remote", location, re.I) else None,
                )
            )
        return out


class Workday(ATSProvider):
    """Workday, built differently enough from the others to be handled differently.

      * the listing is a POST (everything else is a GET)
      * 20 results per page, fixed — raising the limit returns nothing at all
      * the listing is thin: no department, no employment type, and a relative
        date ("Posted Yesterday") rather than a real one
      * a board is identified by two things, a tenant host and a job-site name

    The slug therefore carries both, as `host::siteName` —
    `nvidia.wd5.myworkdayjobs.com::NVIDIAExternalCareerSite`.

    Workday boards are also by far the largest: NVIDIA publishes around 2,000
    open jobs where most boards on other platforms hold tens.
    """

    id = "workday"
    label = "Workday"
    example_slug = "acme.wd5.myworkdayjobs.com::Careers"
    method = "POST"
    page_size = 20
    empty_means_exists = False  # a wrong site name still answers 200 and empty

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, path, _ = parse_target(value)
        if not re.search(r"\.myworkdayjobs\.com$", host, re.IGNORECASE):
            return ""
        # A locale segment can sit in the path (/en-US/SiteName/job/...), so the
        # site name is the first segment that is not a locale.
        seg = [s for s in path.split("/") if s and not re.fullmatch(r"[a-z]{2}(-[A-Z]{2})?", s)]
        return f"{host}::{seg[0]}" if seg else ""

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        host, _, site = slug.partition("::")
        tenant = host.split(".")[0]
        return f"https://{host}/wday/cxs/{tenant}/{site}/jobs?offset={offset}"

    @classmethod
    def body(cls, slug: str, offset: int = 0) -> dict[str, Any]:
        return {"appliedFacets": {}, "limit": 20, "offset": offset, "searchText": ""}

    @classmethod
    def next_offset(cls, payload: Any, fetched: int) -> int | None:
        total = (payload or {}).get("total", 0) if isinstance(payload, dict) else 0
        return fetched if fetched < total else None

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        jobs = payload.get("jobPostings") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return None
        host, _, site = slug.partition("::")
        out = []
        for j in jobs:
            bullets = j.get("bulletFields") or []
            external = _text(j.get("externalPath"))
            location = _text(j.get("locationsText"))
            out.append(
                cls._job(
                    slug,
                    source_job_id=_text(bullets[0]) if bullets else external.split("_")[-1],
                    title=_text(j.get("title")),
                    url=f"https://{host}/{site}{external}",
                    location=location,
                    remote=True if re.search(r"remote", location, re.I) else None,
                    # The listing carries only a relative label. It is kept as
                    # posted_label rather than passed off as a date; published_at
                    # stays None until the detail endpoint supplies a real one.
                    posted_label=_text(j.get("postedOn")) or None,
                )
            )
        return out

    @classmethod
    def detail_endpoint(cls, slug: str, job: RawJob) -> str:
        """The only place the description, real date and employment type live."""
        host, _, site = slug.partition("::")
        tenant = host.split(".")[0]
        path = job.url.split(f"/{site}", 1)[1] if f"/{site}" in job.url else ""
        return f"https://{host}/wday/cxs/{tenant}/{site}{path}"

    @classmethod
    def merge_detail(cls, job: RawJob, payload: Any) -> RawJob:
        info = (payload or {}).get("jobPostingInfo") if isinstance(payload, dict) else None
        if not info:
            return job
        return job.model_copy(
            update={
                "description": strip_html(info.get("jobDescription") or ""),
                "published_at": _text(info.get("startDate")) or None,
                "employment_type": _text(info.get("timeType")),
                "url": _text(info.get("externalUrl")) or job.url,
            }
        )


class Workable(ATSProvider):
    id = "workable"
    label = "Workable"
    empty_means_exists = False  # 200-empty for accounts that do not exist

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, path, raw = parse_target(value)
        if "workable.com" in host:
            if seg := [s for s in path.split("/") if s]:
                return seg[0]
            sub: str = host.split(".")[0]
            if sub not in ("apply", "www"):
                return sub
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        return f"https://apply.workable.com/api/v1/widget/accounts/{quote(slug)}?details=true"

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        jobs = payload.get("jobs") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return None
        return [
            cls._job(
                slug,
                source_job_id=_text(j.get("shortcode") or j.get("id")),
                title=_text(j.get("title")),
                url=_text(j.get("url") or j.get("shortlink")),
                location=", ".join(
                    p
                    for p in (
                        _text(j.get("city")),
                        _text(j.get("state")),
                        _text(j.get("country")),
                    )
                    if p
                ),
                department=_text(j.get("department")),
                team=_text(j.get("function")),
                employment_type=_text(j.get("employment_type")),
                remote=j.get("telecommuting") if isinstance(j.get("telecommuting"), bool) else None,
                published_at=_text(j.get("published_on")) or None,
                description=strip_html(j.get("description") or ""),
            )
            for j in jobs
        ]


class Recruitee(ATSProvider):
    id = "recruitee"
    label = "Recruitee"
    empty_means_exists = True  # 404s for tenants that do not exist

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, _, raw = parse_target(value)
        if host.endswith("recruitee.com"):
            return host.split(".")[0]
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        # The trailing slash is required; without it the API 404s.
        return f"https://{quote(slug)}.recruitee.com/api/offers/"

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        jobs = payload.get("offers") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return None
        out = []
        for j in jobs:
            s = j.get("salary") or {}
            salary = ""
            if s.get("min") or s.get("max"):
                span = "–".join(str(v) for v in (s.get("min"), s.get("max")) if v)
                salary = f"{span} {_text(s.get('currency'))}/{_text(s.get('period'))}".rstrip()
            out.append(
                cls._job(
                    slug,
                    source_job_id=_text(j.get("id") or j.get("guid")),
                    title=_text(j.get("title")),
                    url=_text(j.get("careers_url") or j.get("careers_apply_url")),
                    location=_text(j.get("location"))
                    or ", ".join(p for p in (_text(j.get("city")), _text(j.get("country"))) if p),
                    department=_text(j.get("department")),
                    employment_type=_text(j.get("employment_type_code")).replace("_", " "),
                    remote=j.get("remote") if isinstance(j.get("remote"), bool) else None,
                    published_at=_text(j.get("published_at")) or None,
                    salary=salary,
                    description=strip_html(j.get("description") or ""),
                )
            )
        return out


class BambooHR(ATSProvider):
    id = "bamboohr"
    label = "BambooHR"
    empty_means_exists = False  # unknown subdomains 302 to the marketing site

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, _, raw = parse_target(value)
        if host.endswith("bamboohr.com"):
            return host.split(".")[0]
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        return f"https://{quote(slug)}.bamboohr.com/careers/list"

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        jobs = payload.get("result") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return None
        out = []
        for j in jobs:
            loc = j.get("location") or {}
            out.append(
                cls._job(
                    slug,
                    source_job_id=_text(j.get("id")),
                    title=_text(j.get("jobOpeningName")),
                    url=f"https://{slug}.bamboohr.com/careers/{_text(j.get('id'))}",
                    location=", ".join(
                        p for p in (_text(loc.get("city")), _text(loc.get("state"))) if p
                    ),
                    department=_text(j.get("departmentLabel")),
                    employment_type=_text(j.get("employmentStatusLabel")),
                    remote=j.get("isRemote") if isinstance(j.get("isRemote"), bool) else None,
                )
            )
        return out


class Breezy(ATSProvider):
    id = "breezy"
    label = "Breezy HR"
    empty_means_exists = True

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, _, raw = parse_target(value)
        if host.endswith("breezy.hr"):
            return host.split(".")[0]
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        return f"https://{quote(slug)}.breezy.hr/json"

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        if not isinstance(payload, list):
            return None
        # Lever and Rippling also return bare arrays; tell them apart by a
        # Breezy-specific key.
        if payload and "friendly_id" not in payload[0]:
            return None
        out = []
        for j in payload:
            loc = j.get("location") or {}
            out.append(
                cls._job(
                    slug,
                    source_job_id=_text(j.get("id")),
                    title=_text(j.get("name")),
                    url=_text(j.get("url")),
                    location=_text(loc.get("name")),
                    department=_text(j.get("department")),
                    employment_type=_text((j.get("type") or {}).get("name")),
                    remote=loc.get("is_remote") if isinstance(loc.get("is_remote"), bool) else None,
                    published_at=_text(j.get("published_date")) or None,
                    salary=_text(j.get("salary")),
                )
            )
        return out


class Teamtailor(ATSProvider):
    id = "teamtailor"
    label = "Teamtailor"
    empty_means_exists = True

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, _, raw = parse_target(value)
        # Many tenants run on their own domain, so a full host is accepted.
        if host.endswith("teamtailor.com"):
            return host.split(".")[0]
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        host = slug if "." in slug else f"{slug}.teamtailor.com"
        return f"https://{host}/jobs.json"

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        jobs = payload.get("items") if isinstance(payload, dict) else None
        if not isinstance(jobs, list):
            return None
        out = []
        for j in jobs:
            posting = j.get("_jobposting") or {}
            # Observed in the wild as a dict, a list of dicts, or absent. A
            # single tenant returning the list form used to abort the whole run.
            location = posting.get("jobLocation") or {}
            if isinstance(location, list):
                location = location[0] if location and isinstance(location[0], dict) else {}
            addr = (location.get("address") if isinstance(location, dict) else None) or {}
            if not isinstance(addr, dict):
                addr = {}
            out.append(
                cls._job(
                    slug,
                    source_job_id=_text(j.get("id")),
                    title=_text(j.get("title")),
                    url=_text(j.get("url")),
                    location=", ".join(
                        p
                        for p in (
                            _text(addr.get("addressLocality")),
                            _text(addr.get("addressRegion")),
                            _text(addr.get("addressCountry")),
                        )
                        if p
                    ),
                    department=_text(posting.get("industry")),
                    employment_type=_text(posting.get("employmentType")),
                    published_at=_text(j.get("date_published") or posting.get("datePosted"))
                    or None,
                    description=strip_html(j.get("content_html") or ""),
                )
            )
        return out


class Personio(ATSProvider):
    id = "personio"
    label = "Personio"
    empty_means_exists = False

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, _, raw = parse_target(value)
        if "jobs.personio." in host:
            return host.split(".")[0]
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        return f"https://{quote(slug)}.jobs.personio.de/xml"

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        """Personio alone answers in XML. Read just what is needed rather than
        pulling in a parser for one provider."""
        xml = payload if isinstance(payload, str) else ""
        if "<position>" not in xml:
            return None

        def pick(block: str, tag: str) -> str:
            m = re.search(rf"<{tag}>([\s\S]*?)</{tag}>", block, re.IGNORECASE)
            if not m:
                return ""
            return strip_html(re.sub(r"<!\[CDATA\[|\]\]>", "", m.group(1)))

        out = []
        for m in re.finditer(r"<position>([\s\S]*?)</position>", xml, re.IGNORECASE):
            block = m.group(1)
            job_id = pick(block, "id")
            out.append(
                cls._job(
                    slug,
                    source_job_id=job_id,
                    title=pick(block, "name"),
                    url=f"https://{slug}.jobs.personio.de/job/{job_id}",
                    location=pick(block, "office"),
                    department=pick(block, "department"),
                    team=pick(block, "subcompany"),
                    employment_type=pick(block, "employmentType"),
                    published_at=pick(block, "createdAt") or None,
                    description=pick(block, "jobDescriptions"),
                )
            )
        return out


class Keka(ATSProvider):
    """Keka — widely used by Indian startups and mid-size companies.

    Added because the Greenhouse/Lever/Ashby set, which covers US companies
    well, is close to blind in India: of 60 Indian companies probed, two thirds
    exposed no readable board and Keka was the most common reason.

    Like Workday, a board needs two identifiers — the tenant subdomain and a
    careers-portal UUID — so the slug carries both as `subdomain::uuid`. The
    UUID is not derivable; `discover.py` reads it from the tenant's own careers
    page, which is the only public source.
    """

    id = "keka"
    label = "Keka"
    example_slug = "acme::00000000-0000-0000-0000-000000000000"
    # A wrong UUID answers 200 with [], exactly like an existing board with no
    # openings. Verified live: empty proves nothing here.
    empty_means_exists = False

    # Keka's numeric employment type. 2 is overwhelmingly the common value and
    # maps to a permanent role; the rest are inferred conservatively.
    JOB_TYPES: ClassVar[dict[int, str]] = {
        1: "part time",
        2: "full time",
        3: "contract",
        4: "internship",
        5: "temporary",
    }

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, _path, raw = parse_target(value)
        if host.endswith("keka.com"):
            subdomain = host.split(".")[0]
            # A UUID already in the URL means the slug is fully formed.
            if m := re.search(
                r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", raw, re.I
            ):
                return f"{subdomain}::{m.group(0)}"
            return subdomain
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        subdomain, _, tenant = slug.partition("::")
        return (
            f"https://{quote(subdomain)}.keka.com/careers/api/embedjobs/default/"
            f"active/{quote(tenant)}"
        )

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        if not isinstance(payload, list):
            return None
        # Lever, Rippling and Breezy also return bare arrays; check a
        # Keka-specific key rather than trusting the shape.
        if payload and not ("jobLocations" in payload[0] and "title" in payload[0]):
            return None

        subdomain, _, _tenant = slug.partition("::")
        out = []
        for j in payload:
            locations = j.get("jobLocations") or []
            names = [
                ", ".join(p for p in (_text(loc.get("city")), _text(loc.get("countryName"))) if p)
                for loc in locations
                if isinstance(loc, dict)
            ]
            location = " | ".join(n for n in names if n)
            salary = j.get("salaryRange") or {}
            out.append(
                cls._job(
                    slug,
                    source_job_id=_text(j.get("id")),
                    title=_text(j.get("title")),
                    url=f"https://{subdomain}.keka.com/careers/jobdetails/{_text(j.get('id'))}",
                    location=location,
                    department=_text(j.get("departmentName")),
                    employment_type=cls.JOB_TYPES.get(j.get("jobType"), ""),
                    remote=True if re.search(r"remote", location, re.I) else None,
                    published_at=_text(j.get("publishedOn")) or None,
                    salary=_text(j.get("salaryRangeFormat")) or _text(salary.get("currency")),
                    description=strip_html(j.get("description") or j.get("excerpt") or ""),
                    raw={
                        "experience": _text(j.get("experience")),
                        "skills": j.get("skillNames") or [],
                    },
                )
            )
        return out


class Freshteam(ATSProvider):
    """Freshteam — the other common ATS among Indian startups.

    The only provider here that reads HTML rather than a JSON feed, because
    Freshteam has no public one: the careers page is server-rendered and its
    documented API needs a bearer token. So this is genuinely more fragile than
    the rest, and that is stated rather than hidden.

    What makes it tolerable: the job cards carry `data-portal-*` attributes that
    Freshteam's own template comment marks as load-bearing — "Do not remove
    data-portal-* attributes. Removing the same will result in breakages in
    filter behaviour." Attributes their own filtering depends on are the most
    stable thing on the page.

    Employment type is read from the visible label ("Full Time", "Contract")
    rather than the numeric `data-portal-job-type`, because the words are
    self-describing and the numbers are an undocumented internal code.
    """

    id = "freshteam"
    label = "Freshteam"
    # A nonexistent tenant serves a near-empty 200 page, so an empty result
    # proves nothing. Verified live against a made-up subdomain.
    empty_means_exists = False

    @classmethod
    def slug_from(cls, value: str) -> str:
        host, _path, raw = parse_target(value)
        if host.endswith("freshteam.com"):
            return host.split(".")[0]
        return bare_name(host) if host else raw

    @classmethod
    def endpoint(cls, slug: str, include_description: bool = True, offset: int = 0) -> str:
        return f"https://{quote(slug)}.freshteam.com/jobs"

    @classmethod
    def parse(cls, payload: Any, slug: str) -> list[RawJob] | None:
        html_text = payload if isinstance(payload, str) else ""
        # Identify the platform before trusting anything: the marker below is
        # Freshteam's own, so another site's HTML cannot be misread as a board.
        if "data-portal-job-type" not in html_text and "freshteam" not in html_text.lower():
            return None

        try:
            from bs4 import BeautifulSoup
        except ImportError:  # pragma: no cover - dependency is pinned
            return None

        soup = BeautifulSoup(html_text, "lxml")
        out: list[RawJob] = []
        for card in soup.select("a.heading[data-portal-job-type]"):
            href = card.get("href") or ""
            title_node = card.select_one(".job-title")
            title = title_node.get_text(strip=True) if title_node else ""
            if not title or not href:
                continue

            # The visible block reads "Bengaluru, Karnataka<br/>Full Time".
            info = card.select_one(".location-info")
            parts = (
                [p.strip() for p in info.get_text("\n", strip=True).split("\n") if p.strip()]
                if info
                else []
            )
            location = parts[0] if parts else _text(card.get("data-portal-location"))
            employment = parts[1] if len(parts) > 1 else ""

            remote_attr = _text(card.get("data-portal-remote-location")).lower()
            out.append(
                cls._job(
                    slug,
                    source_job_id=_source_id_from_href(href),
                    title=title,
                    url=f"https://{slug}.freshteam.com{href}",
                    location=location,
                    employment_type=employment,
                    remote=True if remote_attr == "true" else None,
                )
            )
        return out


def _epoch_ms_to_iso(value: Any) -> str | None:
    """Lever publishes createdAt as epoch milliseconds."""
    if not isinstance(value, (int, float)):
        return None
    from datetime import UTC, datetime

    try:
        return datetime.fromtimestamp(value / 1000, tz=UTC).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


PROVIDERS: list[type[ATSProvider]] = [
    Greenhouse,
    Ashby,
    Lever,
    SmartRecruiters,
    Rippling,
    Workday,
    Workable,
    Recruitee,
    BambooHR,
    Breezy,
    Teamtailor,
    Personio,
    Keka,
    Freshteam,
]

PROVIDERS_BY_ID: dict[str, type[ATSProvider]] = {p.id: p for p in PROVIDERS}


def provider_from_host(host: str) -> type[ATSProvider] | None:
    """When the hostname identifies the ATS outright, use that rather than guessing."""
    if not host:
        return None
    if host.endswith("greenhouse.io"):
        return Greenhouse
    if host.endswith("ashbyhq.com"):
        return Ashby
    if host.endswith("lever.co"):
        return Lever
    if "smartrecruiters.com" in host:
        return SmartRecruiters
    if "rippling.com" in host:
        return Rippling
    if "jobs.personio." in host:
        return Personio
    if re.search(r"\.myworkdayjobs\.com$", host):
        return Workday
    if host.endswith("recruitee.com"):
        return Recruitee
    if host.endswith("bamboohr.com"):
        return BambooHR
    if host.endswith("breezy.hr"):
        return Breezy
    if host.endswith("teamtailor.com"):
        return Teamtailor
    if host.endswith("keka.com"):
        return Keka
    if host.endswith("freshteam.com"):
        return Freshteam
    if "workable.com" in host:
        return Workable
    return None
