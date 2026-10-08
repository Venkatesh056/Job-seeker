"""Job freshness — is this posting still live? Addendum §12.

Nothing re-checked this. A job discovered three weeks ago was applied to on the strength
of a database row, and a filled role produces a wasted application, a bounced URL, or —
worst — a submission into a form for a requisition nobody is reading.

Cheap by construction. One HTTP request against the application URL, before the browser
starts, before the resume is tailored. The ordering matters: tailoring costs a strong-tier
LLM call and opening Chromium costs four hundred megabytes, and both are wasted on a dead
link.

The judgement this module makes carefully is **"gone" versus "cannot tell"**. A 404 is
gone. A 403 from a bot-protected careers page is not — that page may be perfectly alive
to a real browser, and refusing to apply because a bare HTTP client was blocked would
silently drop the best applications. So:

    404 / 410            → EXPIRED, do not apply
    closed-wording       → EXPIRED, do not apply
    200 with a form      → LIVE
    403 / 429 / timeout  → UNKNOWN, proceed and let the browser decide
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

import httpx

from ..config import get_settings
from ..logging import get_logger

log = get_logger("agents.freshness")

# Wording an ATS uses when a requisition is closed. Matched against the whole page, so
# each phrase has to be specific enough not to appear in a live posting's boilerplate.
CLOSED_MARKERS = (
    r"no longer (accepting|available|open|active)",
    r"this (job|position|role|posting|requisition) (is|has) (closed|expired|been filled|no longer)",
    r"position (has been )?(filled|closed)",
    r"applications? (are )?(now )?closed",
    r"we are no longer accepting applications",
    r"this (job |role |position )?posting has expired",
    r"job not found",
    r"requisition (is )?(closed|cancelled)",
    r"the job you.{0,20}looking for (is no longer|has been removed)",
)

# Evidence that a live application form is present. A page that is merely 200 might be a
# soft-404 landing page, and these are what distinguish the two.
LIVE_MARKERS = (
    r"<form[^>]*",
    r'type=["\']file["\']',
    r"\bapply (now|for this|to this)\b",
    r"\bsubmit (your )?application\b",
    r'name=["\'](first_?name|last_?name|resume|cv)["\']',
)

# Beyond this, a posting is old enough to be worth re-checking even if nothing else
# suggests a problem. Most ATS listings that are still up after this are genuinely open;
# the check is cheap and the cost of being wrong is a wasted application.
STALE_AFTER_DAYS = 21


class Freshness(enum.StrEnum):
    LIVE = "live"
    EXPIRED = "expired"
    UNKNOWN = "unknown"


@dataclass
class FreshnessResult:
    state: Freshness
    url: str
    status_code: int | None = None
    reason: str = ""
    age_days: int | None = None
    checked_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    evidence: str = ""

    @property
    def safe_to_apply(self) -> bool:
        """UNKNOWN counts as safe. A bot-blocked page is not a closed one, and the
        browser will find out in a few seconds either way."""
        return self.state is not Freshness.EXPIRED

    def to_dict(self) -> dict[str, Any]:
        return {
            "state": self.state.value,
            "url": self.url,
            "status_code": self.status_code,
            "reason": self.reason,
            "age_days": self.age_days,
            "checked_at": self.checked_at.isoformat(),
            "evidence": self.evidence[:200],
            "safe_to_apply": self.safe_to_apply,
        }


def age_days(posted: date | None) -> int | None:
    """How old a posting is, in whole days, never negative.

    The clamp is not cosmetic. A board that stamps a posting in local time, read from a
    process comparing against UTC, produces an age of -1 for something posted today —
    and every `age >= threshold` test downstream then reads that as "not yet old
    enough", silently. A posting cannot be less than zero days old.
    """
    if posted is None:
        return None
    return max(0, (datetime.now(UTC).date() - posted).days)


def needs_recheck(posted: date | None, last_checked: datetime | None = None) -> bool:
    """Is it worth spending a request on this one?

    Skips the check for a posting verified in the last hour, so a batch applying to ten
    roles at one company does not re-fetch the same board ten times.
    """
    if last_checked is not None:
        seconds = (datetime.now(UTC) - last_checked).total_seconds()
        if seconds < 3600:
            return False
    # Past the cache window, always. One HTTP request is cheap next to a wasted
    # application, and there is no posting age at which "still open?" stops mattering.
    return True


def classify(status_code: int, body: str, url: str) -> FreshnessResult:
    """Decide from a response. Pure, so the judgement is testable without a network."""
    if status_code in (404, 410):
        return FreshnessResult(
            Freshness.EXPIRED,
            url,
            status_code,
            reason=f"the posting returns {status_code}",
        )

    if status_code in (401, 403, 429) or status_code >= 500:
        return FreshnessResult(
            Freshness.UNKNOWN,
            url,
            status_code,
            reason=(
                f"HTTP {status_code} — the page blocked a plain request, which is not "
                f"evidence the job is closed"
            ),
        )

    lowered = " ".join((body or "").lower().split())
    for pattern in CLOSED_MARKERS:
        if match := re.search(pattern, lowered):
            return FreshnessResult(
                Freshness.EXPIRED,
                url,
                status_code,
                reason="the page says the posting is closed",
                evidence=match.group(0),
            )

    for pattern in LIVE_MARKERS:
        if re.search(pattern, lowered):
            return FreshnessResult(
                Freshness.LIVE, url, status_code, reason="an application form is present"
            )

    # 200, no closed wording, no form. Often a JavaScript-rendered page — which the
    # browser will handle fine.
    return FreshnessResult(
        Freshness.UNKNOWN,
        url,
        status_code,
        reason="no form found and no closure notice; the page may be JavaScript-rendered",
    )


async def check(
    url: str,
    *,
    posted: date | None = None,
    client: httpx.AsyncClient | None = None,
) -> FreshnessResult:
    """One request against the application URL.

    Never raises. A network failure is UNKNOWN, which means "proceed" — the freshness
    check exists to catch obvious deaths cheaply, not to become a new way for the
    pipeline to stall.
    """
    if not url:
        return FreshnessResult(Freshness.UNKNOWN, url, reason="no application URL to check")

    settings = get_settings()
    owns_client = client is None
    if client is None:
        client = httpx.AsyncClient(
            timeout=12.0,
            follow_redirects=True,
            headers={"user-agent": settings.user_agent, "accept": "text/html,*/*"},
        )

    try:
        response = await client.get(url)
        result = classify(response.status_code, response.text, url)
    except httpx.HTTPError as exc:
        result = FreshnessResult(
            Freshness.UNKNOWN,
            url,
            reason=f"{type(exc).__name__} — could not reach the posting",
        )
    finally:
        if owns_client:
            await client.aclose()

    result.age_days = age_days(posted)
    if result.state is Freshness.EXPIRED:
        log.info("job_expired", url=url[:120], reason=result.reason, age=result.age_days)
    return result
