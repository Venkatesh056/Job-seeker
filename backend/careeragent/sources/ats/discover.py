"""Work out which ATS a company uses by reading its careers page.

Deriving a board slug from a domain (acme.com → acme) is easy but often wrong:
real companies use slugs like acmecorp, acme-inc or acmehq. So *only once every
guess has failed*, this reads the company's own careers page and pulls the slug
out of whichever job board is embedded in it.

This is what makes "just give it a domain" actually work — and per
docs/00-research.md §3, slug resolution is the real engineering problem in job
discovery, not the HTTP.

Boundaries kept here:
  * robots.txt is read first, and a disallowed path is not fetched
  * only public home and careers pages are read; nothing behind a login
  * a handful of requests per company, never a flood

Adapted from `shunsukefuruyama/ats-jobs` (MIT); see NOTICE.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING
from urllib.parse import urlparse

import httpx

from ...logging import get_logger

if TYPE_CHECKING:
    from ...config import Settings

log = get_logger("ats.discover")

# Where careers pages usually live. Tried in order; the first hit wins.
CAREER_PATHS = ["/careers", "/jobs", "/careers/", "/about/careers", "/company/careers", "/"]

# Rules for pulling an ATS slug out of a page. Adding a rule here widens coverage.
SIGNATURES: list[tuple[str, re.Pattern[str]]] = [
    (
        "greenhouse",
        re.compile(
            r"(?:job-)?boards(?:-api)?\.greenhouse\.io/(?:embed/job_board\?for=)?([a-z0-9_-]{2,})",
            re.I,
        ),
    ),
    ("greenhouse", re.compile(r"greenhouse\.io/embed/job_board\?for=([a-z0-9_-]{2,})", re.I)),
    ("ashby", re.compile(r"jobs\.ashbyhq\.com/([a-z0-9_-]{2,})", re.I)),
    ("ashby", re.compile(r"api\.ashbyhq\.com/posting-api/job-board/([a-z0-9_-]{2,})", re.I)),
    ("lever", re.compile(r"jobs\.(?:eu\.)?lever\.co/([a-z0-9_-]{2,})", re.I)),
    (
        "smartrecruiters",
        re.compile(r"(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9_-]{2,})", re.I),
    ),
    ("rippling", re.compile(r"ats\.rippling\.com/([a-z0-9_-]{2,})", re.I)),
    ("personio", re.compile(r"([a-z0-9-]{2,})\.jobs\.personio\.(?:de|com)", re.I)),
    ("workable", re.compile(r"apply\.workable\.com/([a-z0-9_-]{2,})", re.I)),
    ("recruitee", re.compile(r"([a-z0-9-]{2,})\.recruitee\.com", re.I)),
    ("bamboohr", re.compile(r"([a-z0-9-]{2,})\.bamboohr\.com", re.I)),
    ("breezy", re.compile(r"([a-z0-9-]{2,})\.breezy\.hr", re.I)),
    ("teamtailor", re.compile(r"([a-z0-9-]{2,})\.teamtailor\.com", re.I)),
    ("keka", re.compile(r"([a-z0-9-]{2,})\.keka\.com", re.I)),
    ("freshteam", re.compile(r"([a-z0-9-]{2,})\.freshteam\.com", re.I)),
    # Embedded widgets. When a careers page is built by JavaScript, no ATS link
    # appears in the HTML — only the widget's attributes do.
    ("rippling", re.compile(r"data-job-board-id=[\"']([a-z0-9_-]{2,})[\"']", re.I)),
    ("greenhouse", re.compile(r"data-(?:board-)?token=[\"']([a-z0-9_-]{2,})[\"']", re.I)),
    ("ashby", re.compile(r"data-ashby-jobs?-board=[\"']([a-z0-9_-]{2,})[\"']", re.I)),
]

# Workday needs both a host and a site name, so its match is assembled into one
# slug. Finding a myworkdayjobs.com link on the careers page is the only
# practical way in.
_WORKDAY = re.compile(
    r"([a-z0-9-]+\.wd\d+\.myworkdayjobs\.com)/(?:[a-z]{2}(?:-[A-Z]{2})?/)?([A-Za-z0-9_-]{2,})"
)

_NON_SLUGS = re.compile(r"^(www|jobs|careers|embed|api|static|assets)$", re.I)
_WORKDAY_INTERNAL = re.compile(r"(^|::)(wday|cxs)$", re.I)


def extract_signatures(html: str) -> list[tuple[str, str]]:
    """Return the (provider, slug) pairs found in a page, in order, deduplicated."""
    found: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()

    if m := _WORKDAY.search(html):
        pair = ("workday", f"{m.group(1)}::{m.group(2)}")
        seen.add(pair)
        found.append(pair)

    for provider, pattern in SIGNATURES:
        if not (m := pattern.search(html)):
            continue
        slug = m.group(1)
        # Drop obvious non-slugs. A false positive is not as bad as wrong data,
        # but it wastes a request.
        if _NON_SLUGS.match(slug) or _WORKDAY_INTERNAL.search(slug):
            continue
        pair = (provider, slug)
        if pair in seen:
            continue
        seen.add(pair)
        found.append(pair)
    return found


async def _robots_allows(client: httpx.AsyncClient, origin: str, path: str, timeout: float) -> bool:
    """Read robots.txt and decide whether this path may be fetched.

    Only the `User-agent: *` section applies to us; directives aimed at other
    bots are ignored.
    """
    try:
        response = await client.get(f"{origin}/robots.txt", timeout=timeout)
    except httpx.HTTPError:
        return True  # unreachable robots.txt is not a prohibition
    if response.status_code >= 400:
        return True  # no robots.txt means no restriction

    in_star = False
    disallowed: list[str] = []
    for raw_line in response.text.splitlines():
        line = raw_line.split("#")[0].strip()
        if not line or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key, value = key.strip().lower(), value.strip()
        if key == "user-agent":
            in_star = value == "*"
        elif in_star and key == "disallow" and value:
            disallowed.append(value)
        elif in_star and key == "allow" and value == "/":
            return True
    return not any(d == "/" or path.startswith(d) for d in disallowed)


async def discover_from_website(
    host: str, client: httpx.AsyncClient, settings: Settings | None = None
) -> list[tuple[str, str]]:
    """Find the ATS and slug for a company domain.

    Returns an empty list when nothing is found; never raises.
    """
    if not host:
        return []

    from ...config import get_settings

    cfg = settings or get_settings()
    origin = f"https://{host}"
    timeout = cfg.http_timeout_seconds

    for path in CAREER_PATHS:
        if cfg.respect_robots_txt and not await _robots_allows(client, origin, path, timeout):
            log.debug("robots_disallowed", host=host, path=path)
            continue
        try:
            response = await client.get(
                f"{origin}{path}", timeout=timeout, headers={"accept": "text/html,*/*"}
            )
        except httpx.HTTPError:
            continue
        if response.status_code >= 400:
            continue

        if found := extract_signatures(response.text):
            found = await _complete_two_part_slugs(found, client, timeout)
            log.info("careers_page_hit", host=host, path=path, found=found)
            return found

    return []


_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)


async def _complete_two_part_slugs(
    found: list[tuple[str, str]], client: httpx.AsyncClient, timeout: float
) -> list[tuple[str, str]]:
    """Resolve providers whose board needs a second identifier.

    Keka's feed is keyed by a careers-portal UUID that appears nowhere in the
    company's own site — only on the Keka-hosted careers page. One extra request
    per tenant turns an unusable subdomain into a working board, and a Keka slug
    without it would silently fetch nothing.
    """
    completed: list[tuple[str, str]] = []
    for provider, slug in found:
        if provider != "keka" or "::" in slug:
            completed.append((provider, slug))
            continue
        try:
            response = await client.get(
                f"https://{slug}.keka.com/careers/",
                timeout=timeout,
                headers={"accept": "text/html,*/*"},
            )
            if response.status_code < 400 and (m := _UUID.search(response.text)):
                completed.append((provider, f"{slug}::{m.group(0)}"))
                continue
            log.debug("keka_uuid_not_found", slug=slug)
        except httpx.HTTPError:
            log.debug("keka_careers_unreachable", slug=slug)
    return completed


def domain_of(url: str) -> str:
    parsed = urlparse(url if "://" in url else f"https://{url}")
    return (parsed.hostname or "").lower().removeprefix("www.")
