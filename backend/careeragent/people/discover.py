"""Finding people from what a company publishes about itself.

Every automated contact provider is unavailable on this project's plans — Apollo's free
tier returns 403 for people search, Vibe's host does not resolve, Clay has no free API. So
this reads the source that needs no provider at all and is the most defensible one there
is: the company's own leadership and team pages. A company that publishes a page saying
"Priya Sharma, VP Engineering" published it deliberately, for exactly this purpose.

The hard part is not fetching. It is knowing when to stop.

An arbitrary team page has no schema. Names sit in headings, in `alt` text, in `<figcaption>`,
in divs with class names nobody standardised. A parser that tries hard enough to handle all
of them will start seeing people in navigation menus, cookie banners and blog bylines — and
a person who does not exist is worse than a person not found, because it goes into a contact
list and someone writes to them.

So this is deliberately conservative and expects to find nothing most of the time:

* a name must look like a name — two to four capitalised words, no digits, not on the
  list of things that merely look like names ("Privacy Policy", "Contact Us")
* a title must be recognisable as a job title by the same function vocabulary the ranker
  uses, so "Chief Marketing Officer" is a person and "Read More" is not
* the two must be **adjacent** in the document, because proximity is the only structural
  signal that generalises across templates
* an email is attached only when it appears in the same block as the person, and only
  when its local part relates to their name — otherwise it is the company's switchboard
  address sitting in a footer, and attributing it to a person would be a fabrication

Everything found carries the URL it was found at. Nothing is inferred.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html import unescape
from typing import Any

import httpx

from ..config import get_settings
from ..logging import get_logger
from ..models.person import (
    ContactDetail,
    Person,
    SourceKind,
    Verification,
    is_person_profile,
    merge,
    normalize_name,
    same_person,
)
from .relevance import function_of

log = get_logger("people.discover")

# Pages a company puts its people on. Ordered by how likely they are to be about people
# rather than about the product.
PEOPLE_PATHS = (
    "/team",
    "/our-team",
    "/leadership",
    "/about/team",
    "/about/leadership",
    "/about-us/team",
    "/company/team",
    "/people",
    "/about",
    "/about-us",
    "/management",
    "/founders",
    "/who-we-are",
)

# Anchor text that means "this link goes to the people". Matched on the visible words a
# company chose, which is far more reliable than guessing at URLs: measured across five
# real sites, *every* guessed path either 404'd or silently redirected to the homepage.
PEOPLE_LINK_WORDS = (
    "leadership",
    "our team",
    "meet the team",
    "the team",
    "our people",
    "management team",
    "executive team",
    "founders",
    "who we are",
    "about us",
    "about",
    "team",
    "people",
)

MAX_PAGES = 5
MAX_HTML = 500_000
# More than this from one page and we are reading a directory, a blog roll or a parse
# gone wrong. A real leadership page has a dozen people on it.
MAX_PER_PAGE = 40


@dataclass
class DiscoveryResult:
    company: str = ""
    domain: str = ""
    people: list[Person] = field(default_factory=list)
    pages_read: list[str] = field(default_factory=list)
    pages_tried: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error

    def note(self) -> str:
        if self.error:
            return f"Could not read {self.domain}: {self.error}"
        if not self.pages_read:
            return (
                f"{self.domain} publishes no team or leadership page that could be read. "
                "Nothing was guessed."
            )
        if not self.people:
            return (
                f"Read {len(self.pages_read)} page(s) at {self.domain} and found no one "
                "named with a job title. Their team page may be JavaScript-rendered."
            )
        with_email = sum(1 for p in self.people if p.email)
        return (
            f"{len(self.people)} person/people from {len(self.pages_read)} page(s) they "
            f"publish. {with_email} with an email they published; the rest have no "
            "address on record, and none was guessed."
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "company": self.company,
            "domain": self.domain,
            "people": [p.to_dict() for p in self.people],
            "count": len(self.people),
            "pages_read": self.pages_read,
            "pages_tried": self.pages_tried,
            "error": self.error or None,
            "note": self.note(),
        }


# ─── Telling a name from a phrase that looks like one ───────────────────────

# Two to four capitalised words. Permits "D'Souza", "Al-Rashid", "van der Berg" through the
# lowercase particles, and initials like "R. Kumar".
_NAME = re.compile(
    r"^(?:[A-Z][a-z'’\-]+\.?|[A-Z]\.|(?:van|von|de|del|da|di|la|le|bin|al)"
    r"(?:\s|-))(?:\s+(?:[A-Z][a-zA-Z'’\-]+\.?|[A-Z]\.|(?:van|von|de|del|da|di|la|le|"
    r"bin|al)))+$"
)

# Capitalised phrases that are not people. Every one of these was seen on a real page.
_NOT_A_NAME = frozenset(
    {
        "about us",
        "contact us",
        "privacy policy",
        "terms of service",
        "terms conditions",
        "cookie policy",
        "read more",
        "learn more",
        "get started",
        "our team",
        "our story",
        "our mission",
        "our values",
        "join us",
        "work with us",
        "case studies",
        "blog posts",
        "press releases",
        "all rights",
        "rights reserved",
        "sign in",
        "sign up",
        "log in",
        "view profile",
        "view more",
        "view all",
        "show more",
        "load more",
        "see more",
        "read story",
        "top trending",
        "press release",
        "media releases",
        "follow us",
        "quick links",
        "head office",
        "united states",
        "united kingdom",
        "new delhi",
        "san francisco",
        "new york",
    }
)

# Words no given name starts with. A capitalised phrase beginning with one of these is a
# section heading or a sentence fragment, however much it looks like a name.
_HEADING_OPENERS = frozenset(
    {
        "our",
        "the",
        "a",
        "an",
        "all",
        "more",
        "other",
        "another",
        "additional",
        "new",
        "latest",
        "recent",
        "featured",
        "related",
        "further",
        "explore",
        "discover",
        "learn",
        "read",
        "see",
        "view",
        "meet",
        "why",
        "how",
        "what",
        "who",
        "where",
        "when",
        "this",
        "these",
        "those",
        "your",
        "my",
        "their",
        "his",
        "her",
        "its",
    }
)

_TAG = re.compile(r"<[^>]+>")
_SCRIPTY = re.compile(r"<(script|style|noscript|svg)\b.*?</\1>", re.S | re.I)
_EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_LINKEDIN = re.compile(r"https?://(?:[a-z]{2,3}\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+")


# Words that appear in product and marketing names but never in a person's name. Every
# one of these was pulled out of a real page as a "person" before this list existed.
_PRODUCT_WORDS = frozenset(
    {
        "ai",
        "api",
        "app",
        "cloud",
        "platform",
        "server",
        "suite",
        "studio",
        "mode",
        "inventory",
        "expense",
        "desk",
        "crm",
        "hrms",
        "analytics",
        "insights",
        "awards",
        "summit",
        "webinar",
        "ebook",
        "report",
        "pricing",
        "demo",
        "trial",
        "solutions",
        "services",
        "products",
        "resources",
        "partners",
        "customers",
        "enterprise",
        "talk",
        "story",
        "stories",
        "engineering",
        "expertise",
        "leadership",
        "leader",
        "leaders",
        "information",
        "management",
        "team",
        "overview",
        "questions",
        "support",
        "community",
        "download",
        "guide",
        "docs",
        "trending",
        "press",
        "media",
        "release",
        "releases",
        "newsroom",
        "blog",
        "news",
        "more",
    }
)


def looks_like_a_name(text: str, *, company: str = "") -> bool:
    clean = " ".join((text or "").split())
    if not (4 <= len(clean) <= 60):
        return False
    if any(ch.isdigit() for ch in clean):
        return False
    words = clean.split()
    if not (2 <= len(words) <= 4):
        return False

    lowered = [w.lower().strip(".,") for w in words]
    if " ".join(lowered) in _NOT_A_NAME:
        return False
    # A heading, not a person. Nobody is called "Our Leaders" or "Additional Information",
    # and both were stored as employees — of Kredx and Druva respectively.
    if lowered[0] in _HEADING_OPENERS:
        return False
    # "Zoho Expense" and "Postman MCP Server" are products, not colleagues.
    if any(word in _PRODUCT_WORDS for word in lowered):
        return False
    if company and company.strip().lower() in lowered:
        return False
    # A capitalised phrase that is really a job title is not a name. Both tests are
    # needed: `function_of` catches "Head of Data", and the role-word test catches
    # "Chief Strategy Officer", whose subject matter is in no function vocabulary and which
    # was being stored as an employee of Freshworks.
    if function_of(clean) or _ROLE_WORD.search(clean):
        return False
    return bool(_NAME.match(clean))


# Words that make a phrase somebody's *role* rather than a topic that merely mentions the
# same field. "Head of Machine Learning" is a job; "AI Agents Engineered" is a headline, and
# both contain "ai". Without this the parser read Postman's product names as job titles.
_ROLE_WORDS = (
    "manager",
    "director",
    "head",
    "lead",
    "chief",
    "officer",
    "president",
    "founder",
    "ceo",
    "cto",
    "cfo",
    "coo",
    "vp",
    "vice president",
    "engineer",
    "developer",
    "scientist",
    "analyst",
    "architect",
    "recruiter",
    "specialist",
    "consultant",
    "partner",
    "principal",
    "associate",
    "coordinator",
    "administrator",
    "designer",
    "researcher",
    "advisor",
    "strategist",
    "executive",
)

_ROLE_WORD = re.compile(
    # Grouped: `(?<![a-z])a|b|c(?![a-z])` binds the lookarounds to the first and last
    # branch only, so "Engineered" matched the bare `engineer` branch and "AI Agents
    # Engineered" read as a job title.
    rf"(?<![a-z])(?:{'|'.join(_ROLE_WORDS)})(?![a-z])",
    re.I,
)


def looks_like_a_title(text: str) -> bool:
    """Is this somebody's job title?

    Two conditions, and both are needed. The function vocabulary alone accepted "AI Agents
    Engineered" and "Automate API workflows" as titles, because they mention the same
    fields a real title does — so a phrase must *also* contain a word that names a role.
    """
    clean = " ".join((text or "").split())
    if not (2 <= len(clean) <= 80):
        return False
    if not _ROLE_WORD.search(clean):
        return False
    # The same vocabulary the ranker uses, so the two agree by construction.
    return bool(function_of(clean))


def _blocks(html: str) -> list[str]:
    """The document as text blocks, in document order.

    Tag boundaries are kept as block boundaries rather than collapsed, because adjacency
    between a name and a title is the one structural signal that survives every template.
    """
    html = _SCRIPTY.sub(" ", html[:MAX_HTML])
    # Keep hrefs: a LinkedIn URL on a person's card is the strongest identity we can get.
    html = re.sub(r'<a\b[^>]*href="([^"]*)"[^>]*>', r" \1 | ", html, flags=re.I)
    text = _TAG.sub("\n", html)
    # Decode properly rather than by hand: a partial table left "Employers&#x27; Forum"
    # sitting in a person's title.
    text = unescape(text)
    out = []
    for raw in text.split("\n"):
        block = " ".join(raw.split())
        if block:
            out.append(block)
    return out


def extract_people(
    html: str,
    *,
    company: str,
    source_url: str,
    kind: SourceKind,
    require_corroboration: bool = False,
) -> list[Person]:
    """People named on one page, from a name sitting next to a job title.

    Returns an empty list far more often than not, and that is the intended behaviour: a
    page with no recognisable people should yield no people rather than yield noise.

    `require_corroboration` drops anybody without a personal LinkedIn URL or a name-matching
    email. Off by default, because measured against real sites it found nobody — the pages
    that name people do not link their profiles. Uncorroborated people are kept and marked
    unverified instead, and nothing unverified is ever contacted automatically.
    """
    blocks = _blocks(html)
    found: list[Person] = []
    # A title belongs to one person. Without this, a card whose own title is unrecognised
    # reaches backwards and steals its neighbour's — which put "Head of Machine Learning"
    # on three different people in a four-card test page.
    claimed: set[int] = set()

    for index, block in enumerate(blocks):
        if not looks_like_a_name(block, company=company):
            continue

        # Forward first and further, because every template puts the title after the name
        # far more often than before. Backwards reaches only the immediately preceding
        # block, which is the one layout where a title leads.
        candidates = [
            *range(index + 1, min(index + 4, len(blocks))),
            *([index - 1] if index > 0 else []),
        ]
        title_at = next(
            (i for i in candidates if i not in claimed and looks_like_a_title(blocks[i])),
            None,
        )
        if title_at is None:
            continue
        claimed.add(title_at)
        title = blocks[title_at]

        # A contact detail belongs to exactly one person. The window must not reach back
        # past the previous name, or a card with no details of its own inherits its
        # neighbour's — which attached Arjun Mehta's LinkedIn profile to Ravi Nair, and
        # would have put one person's name on another person's message.
        # Starts at the person's own block, never before it. A *title* may precede a name
        # in some layouts, which is why the title search looks back one — but a link or an
        # address sitting before a name almost always belongs to the person above, and
        # reaching back for it attached Arjun Mehta's LinkedIn profile to Ravi Nair.
        window_start = index
        window_end = min(len(blocks), index + 5)
        for other in range(index + 1, window_end):
            if looks_like_a_name(blocks[other], company=company):
                window_end = other
                break
        nearby = " ".join(blocks[window_start:window_end])
        # A title naming a different employer is a customer quote or an advisory board
        # entry, not somebody who works here. Quantiphi's leadership page carries
        # "President & CEO, Employers' Forum of Indiana" — a real person at a real
        # company, neither of which is Quantiphi.
        if _names_another_employer(title, company):
            log.debug("person_belongs_elsewhere", name=block, title=title, url=source_url)
            continue

        person = Person(
            name=" ".join(block.split()),
            title=title,
            company=company,
            sources=[{"url": source_url, "kind": kind.value}],
        )

        if linkedin := _LINKEDIN.search(nearby):
            url = linkedin.group(0)
            if is_person_profile(url):
                person.linkedin = ContactDetail(
                    value=url,
                    kind=kind,
                    source_url=source_url,
                    verification=Verification.VERIFIED,
                    confidence=0.85,
                )

        for candidate in _EMAIL.finditer(nearby):
            address = candidate.group(0)
            if _email_belongs_to(address, person.name):
                person.email = ContactDetail(
                    value=address,
                    kind=kind,
                    source_url=source_url,
                    verification=Verification.VERIFIED,
                    confidence=0.9,
                )
                break

        # Corroboration decides how much this record is trusted, not whether it exists.
        #
        # Measured against five real company sites: requiring a personal LinkedIn URL or a
        # name-matching email found *nobody at all*, because none of those pages contains
        # one — Quantiphi's leadership page names Shawn Duggan, VP of Application
        # Development, with no profile link anywhere on it. Dropping him would lose a real
        # person to a rule about evidence he never had a chance to provide.
        #
        # So an uncorroborated person is kept and labelled `unverified`. They appear, with
        # the page they came from, and are never contacted automatically — which is what
        # `Verification` is for. The user decides.
        person.corroborated = bool(person.linkedin or person.email)
        if require_corroboration and not person.corroborated:
            log.debug("person_uncorroborated", name=person.name, url=source_url)
            continue

        found.append(person)
        if len(found) >= MAX_PER_PAGE:
            log.debug("people_page_capped", url=source_url)
            break

    return found


def _names_another_employer(title: str, company: str) -> bool:
    """Does this title say the person works somewhere else?

    A title like "President & CEO, Employers' Forum of Indiana" carries its own employer
    after a comma or "at". When that employer is not the company whose page we are reading,
    the person is a customer, an advisor or a quote — not a colleague.
    """
    tail = re.split(r",| at | @ ", title, maxsplit=1)
    if len(tail) < 2:
        return False
    other = " ".join(tail[1].split()).strip(" .,")
    if len(other) < 3:
        return False
    ours = (company or "").strip().lower()
    return bool(ours) and ours not in other.lower()


def _email_belongs_to(address: str, name: str) -> bool:
    """Is this address this person's, or the company's switchboard sitting in a footer?

    The test is whether the local part actually relates to the name. `priya@acme.com` next
    to Priya Sharma is hers; `info@acme.com` next to Priya Sharma is not, and attributing
    it to her would turn a real address into a false claim about who reads it.
    """
    local = address.split("@", 1)[0].lower()
    parts = [p for p in normalize_name(name).split() if len(p) > 2]
    if not parts:
        return False
    first, last = parts[0], parts[-1]

    # firstname, firstname.lastname, f.lastname, firstnamel — the published conventions.
    return (
        first in local
        or last in local
        or (len(first) > 0 and local.startswith(first[0]) and last in local)
    )


def deduplicate(people: list[Person]) -> list[Person]:
    """One record per human being, across the pages they appeared on."""
    canonical: list[Person] = []
    for person in people:
        for existing in canonical:
            if same_person(existing, person):
                merge(existing, person)
                break
        else:
            canonical.append(person)
    return canonical


_ANCHOR = re.compile(
    r"""<a\s[^>]*href=["']([^"'>\s]+)["'][^>]*>(.*?)</a>""",
    re.I | re.S,
)


def people_links(html: str, origin: str) -> list[str]:
    """Links on this page that the site itself says lead to its people.

    Reads the anchor text rather than guessing at paths. A company that has a leadership
    page links to it and says so; a company that does not has nothing to find, and a
    guessed URL against it returns the homepage with a 200 — which is how a crawler ends
    up reporting marketing copy as staff.
    """
    from urllib.parse import urljoin, urlparse

    host = urlparse(origin).hostname or ""
    found: list[str] = []
    seen: set[str] = set()

    for match in _ANCHOR.finditer(html[:MAX_HTML]):
        href, label = match.group(1), " ".join(_TAG.sub(" ", match.group(2)).split())
        text = label.strip().lower()
        if not text or len(text) > 40:
            continue
        if not any(word == text or word in text for word in PEOPLE_LINK_WORDS):
            continue

        url = urljoin(origin, href)
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https"):
            continue
        # Stay on the company's own site. An "About us" link to a press article is not
        # their team page.
        if parsed.hostname and host and parsed.hostname.split(".")[-2:] != host.split(".")[-2:]:
            continue
        clean = url.split("#")[0].rstrip("/")
        if clean and clean not in seen and clean.rstrip("/") != origin.rstrip("/"):
            seen.add(clean)
            found.append(clean)

    # Most specific first: a "Leadership" link is a better bet than "About".
    def rank(url: str) -> int:
        lowered = url.lower()
        for index, word in enumerate(PEOPLE_LINK_WORDS):
            if word.replace(" ", "-") in lowered or word.replace(" ", "") in lowered:
                return index
        return len(PEOPLE_LINK_WORDS)

    return sorted(found, key=rank)[:8]


async def _search_for_people_page(company: str, host: str) -> list[str]:
    """Ask a search provider for this company's own leadership page.

    Restricted to the company's own domain: `theorg.com/org/razorpay/teams/leadership-team`
    is a third-party directory, and this project reads what a company publishes about
    itself rather than what others publish about it.
    """
    from ..sources import search as websearch

    try:
        result = await websearch.search(
            f"{company} {host} leadership team management executives", limit=8
        )
    except Exception as exc:
        log.debug("people_search_unavailable", error=f"{type(exc).__name__}")
        return []
    if not result.ok:
        return []

    root = ".".join(host.split(".")[-2:])
    out: list[str] = []
    for hit in result.hits:
        if root not in (hit.host or ""):
            continue
        url = hit.url.split("#")[0].rstrip("/")
        lowered = url.lower()
        # A press release announcing one appointment is about a person; it is not a page
        # of people, and parsing it yields the headline furniture around the quote.
        if any(
            marker in lowered for marker in ("/media-release", "/newsroom/20", "/press/", "/blog/")
        ):
            continue
        if any(word.replace(" ", "-") in lowered for word in PEOPLE_LINK_WORDS):
            out.append(url)
    return out[:4]


def _same_page(landed: str, asked: str) -> bool:
    """Did the request end up where it was sent?

    Compares the path only: a redirect from `http` to `https`, or one that adds or drops a
    trailing slash, is the same page. A redirect to `/` or to anywhere else is not.
    """
    from urllib.parse import urlparse

    try:
        here = urlparse(landed).path.rstrip("/").lower()
        there = urlparse(asked).path.rstrip("/").lower()
    except ValueError:
        return False
    return here == there


def _kind_for(path: str) -> SourceKind:
    if "leader" in path or "management" in path or "founder" in path:
        return SourceKind.LEADERSHIP_PAGE
    if "team" in path or "people" in path:
        return SourceKind.TEAM_PAGE
    return SourceKind.COMPANY_WEBSITE


async def discover(
    company: str,
    domain: str,
    *,
    client: httpx.AsyncClient | None = None,
    max_pages: int = MAX_PAGES,
    require_corroboration: bool = False,
    use_search: bool = True,
) -> DiscoveryResult:
    """Read a company's own people pages and return who they name.

    Never raises. A company with no readable team page is a normal outcome and yields an
    empty result with a note saying so, because "we found nobody" and "we could not look"
    must stay distinguishable — the same rule the scoring now follows.
    """
    from ..sources.ats.discover import _robots_allows, domain_of

    result = DiscoveryResult(company=company, domain=domain)
    if not domain:
        result.error = "no domain known for this company"
        return result

    settings = get_settings()
    host = domain_of(domain)
    origin = f"https://{host}"
    result.domain = host

    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=settings.http_timeout_seconds,
            follow_redirects=True,
            headers={"user-agent": settings.user_agent, "accept": "text/html,*/*"},
        )

    raw: list[Person] = []
    try:
        # The homepage first, for the links the company itself publishes to its people.
        # This replaces guessing at paths, which measured badly: every guessed URL across
        # five real sites either 404'd or redirected silently to the homepage, and the
        # crawler then parsed the homepage and called its marketing copy staff.
        candidates: list[str] = []
        if settings.respect_robots_txt is False or await _robots_allows(
            client, origin, "/", settings.http_timeout_seconds
        ):
            try:
                home = await client.get(origin)
                if home.status_code < 400 and "html" in home.headers.get("content-type", ""):
                    candidates = people_links(home.text, origin)
            except httpx.HTTPError:
                candidates = []
        result.pages_tried += 1

        # Then the conventional paths, as a cheap fallback.
        candidates.extend(f"{origin}{path}" for path in PEOPLE_PATHS)

        # Then ask a search provider where the page actually is.
        #
        # This is the one that works. Measured across five real sites: guessing paths found
        # nothing (every guess 404'd or redirected to the homepage), and reading the site's
        # own anchors found nothing either, because modern navigation is a JavaScript
        # dropdown whose links are `href="#"`. Search engines have already rendered these
        # sites, so they know the real URL — `quantiphi.com/company/executive-team` and
        # `razorpay.com/newsroom/leadership-team`, neither of which any path list would
        # produce. One credit per company, against a 1,000-credit monthly budget.
        if use_search:
            candidates.extend(await _search_for_people_page(company, host))

        seen_urls: set[str] = set()
        for candidate in candidates:
            if len(result.pages_read) >= max_pages:
                break
            from urllib.parse import urlparse

            path = urlparse(candidate).path or "/"
            if candidate.rstrip("/") in seen_urls:
                continue
            seen_urls.add(candidate.rstrip("/"))
            result.pages_tried += 1

            if settings.respect_robots_txt and not await _robots_allows(
                client, origin, path, settings.http_timeout_seconds
            ):
                log.debug("people_robots_disallowed", host=host, path=path)
                continue
            try:
                response = await client.get(candidate)
            except httpx.HTTPError:
                continue
            if response.status_code >= 400:
                continue
            if "html" not in response.headers.get("content-type", ""):
                continue

            # A guessed path that quietly redirects elsewhere is not the page we asked for.
            # Measured: `quantiphi.com/leadership` does not exist and 302s to the homepage,
            # which returns 200 with `content-type: text/html` — so the crawler happily
            # parsed the *homepage* and reported its marketing copy as staff. Every name it
            # found there ("Industry Leadership", "Phi Labs") came from this one mistake,
            # and the two real people were customer testimonials on the front page.
            landed = str(response.url)
            if not _same_page(landed, candidate):
                log.debug("people_path_redirected", asked=path, landed=landed)
                continue

            url = landed
            people = extract_people(
                response.text,
                company=company,
                source_url=url,
                kind=_kind_for(path),
                require_corroboration=require_corroboration,
            )
            if people:
                result.pages_read.append(url)
                raw.extend(people)
    finally:
        if owns_client:
            await client.aclose()

    result.people = deduplicate(raw)
    log.info(
        "people_discovered",
        company=company,
        domain=host,
        found=len(result.people),
        pages=len(result.pages_read),
    )
    return result
