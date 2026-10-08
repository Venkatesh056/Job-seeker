"""ATS detection and fetching.

A caller never has to know which ATS a company uses — give this a domain and it
works that out.

Detection probes every provider concurrently. Probing in series means waiting out
the timeout of each ATS that is not the right one, which can exceed a minute per
company. Run time is a real cost here, not a cosmetic one.

Adapted from `shunsukefuruyama/ats-jobs` (MIT); see NOTICE.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ...config import get_settings
from ...logging import get_logger
from ...models.job import RawJob
from ...net.limits import get_limiter, retry_after_seconds
from .discover import discover_from_website
from .providers import (
    PROVIDERS,
    PROVIDERS_BY_ID,
    ATSProvider,
    Workday,
    parse_target,
    provider_from_host,
    slug_variants,
)

log = get_logger("ats.fetcher")


class RetryableStatus(Exception):
    """429 or 5xx — the other side is having a moment."""


@dataclass
class BoardResult:
    provider: str
    slug: str
    jobs: list[RawJob] = field(default_factory=list)
    discovered_via: str = "unknown"
    error: str | None = None

    # The board answered, but with fewer rows than it said it had. Kept separate from
    # `error` on purpose: the rows that did arrive are real openings and throwing them away
    # because forty were promised would be the worse mistake. But a caller that reports
    # "37 new jobs at Acme" off a partial fetch is reporting a number nobody can trust, so
    # the discrepancy travels with the result rather than being logged and forgotten.
    partial: bool = False
    expected_total: int | None = None

    @property
    def ok(self) -> bool:
        return self.error is None

    @property
    def shortfall(self) -> int:
        return max(0, (self.expected_total or 0) - len(self.jobs)) if self.partial else 0


def _claimed_total(payload: Any) -> int | None:
    """What the platform said it had, under whichever name it uses.

    Best effort, and `None` when it said nothing — a board that never claims a total cannot
    be short of one, and inventing a number to compare against would manufacture a
    discrepancy out of a platform that simply does not paginate.
    """
    if not isinstance(payload, dict):
        return None
    for name in ("totalFound", "total", "totalCount", "count"):
        value = payload.get(name)
        if isinstance(value, int) and value >= 0:
            return value
    meta = payload.get("meta")
    if isinstance(meta, dict) and isinstance(meta.get("total"), int):
        return int(meta["total"])
    return None


class ATSFetcher:
    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._settings = get_settings()
        self._client = client
        self._owns_client = client is None

    async def __aenter__(self) -> ATSFetcher:
        if self._client is None:
            self._client = httpx.AsyncClient(
                timeout=self._settings.http_timeout_seconds,
                follow_redirects=True,
                headers={
                    "user-agent": self._settings.user_agent,
                    "accept": "application/json, text/xml, */*",
                },
            )
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            raise RuntimeError("ATSFetcher must be used as an async context manager")
        return self._client

    # ─── HTTP ───────────────────────────────────────────────────────────────

    @retry(
        retry=retry_if_exception_type((RetryableStatus, httpx.TransportError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=0.6, max=8),
        reraise=True,
    )
    async def _request(
        self, url: str, method: str = "GET", body: dict[str, Any] | None = None, key: str = ""
    ) -> tuple[int, Any]:
        """One HTTP request. Parses JSON where it can, returns raw text otherwise
        (Personio answers in XML).

        404 is a definite answer — this is not that ATS — so it is never retried.

        Rate limited *inside* the retry, not outside it, which matters: a 429 is retried by
        the decorator, and a limiter that only ran on the first attempt would let the retries
        through unthrottled — hitting hardest exactly when the server has said stop.
        """
        limiter = get_limiter()
        await limiter.acquire(key or "website")

        response = await self.client.request(method, url, json=body)

        if response.status_code == 429:
            # The server's own number, not ours. Applied before the retry sleeps, so the
            # backoff and the penalty do not double up into a wait nobody chose.
            await limiter.penalise(key or "website", retry_after_seconds(response.headers))
            raise RetryableStatus(f"429 from {url}")
        if response.status_code >= 500:
            raise RetryableStatus(f"{response.status_code} from {url}")
        if response.status_code >= 400:
            return response.status_code, None
        try:
            return response.status_code, response.json()
        except (json.JSONDecodeError, ValueError):
            return response.status_code, response.text

    # ─── Single board ───────────────────────────────────────────────────────

    async def fetch_board(
        self,
        provider: type[ATSProvider],
        slug: str,
        include_description: bool = True,
        max_pages: int = 50,
    ) -> BoardResult:
        """Fetch every page of one board. Partial pages are a silent-wrong failure,
        so paging continues until the platform's own total is exhausted."""
        result = BoardResult(provider=provider.id, slug=slug)
        offset = 0
        pages = 0
        # Two independent guards against a board that pages forever. Independent because
        # they catch different servers: one that keeps returning the same rows, and one
        # whose own `next_offset` never moves.
        seen_ids: set[str] = set()
        last_offset: int | None = None

        while pages < max_pages:
            url = provider.endpoint(slug, include_description, offset)
            body = provider.body(slug, offset) if provider.method == "POST" else None
            try:
                status, payload = await self._request(url, provider.method, body, key=provider.id)
            except Exception as exc:
                result.error = f"{type(exc).__name__}: {exc}"
                return result

            if status >= 400:
                result.error = f"HTTP {status}"
                return result

            try:
                parsed = provider.parse(payload, slug)
            except Exception as exc:
                result.error = f"parse_failed: {type(exc).__name__}"
                log.warning(
                    "provider_parse_failed",
                    provider=provider.id,
                    slug=slug,
                    error=f"{type(exc).__name__}: {exc}"[:160],
                )
                return result
            if parsed is None:
                # Shape did not match: this is not that platform.
                result.error = "shape_mismatch"
                return result

            fresh = [job for job in parsed if str(job.source_job_id) not in seen_ids]
            seen_ids.update(str(job.source_job_id) for job in parsed)
            result.jobs.extend(fresh)
            pages += 1

            next_offset = provider.next_offset(payload, len(result.jobs))
            if next_offset is None or not parsed:
                break

            # A page that contained nothing new means the server is repeating itself. It
            # has told us a total it is not going to reach, so believe the rows rather than
            # the total and stop — otherwise the loop runs to `max_pages` collecting
            # duplicates and calls it a successful fetch.
            if not fresh or next_offset == last_offset:
                result.partial = True
                result.expected_total = _claimed_total(payload)
                log.warning(
                    "board_paging_stalled",
                    provider=provider.id,
                    slug=slug,
                    fetched=len(result.jobs),
                    claimed=result.expected_total,
                    pages=pages,
                )
                break

            last_offset = next_offset
            offset = next_offset

        if pages >= max_pages and not result.partial:
            # Hit the page cap with the platform still claiming more. Same class of
            # problem, different cause, and equally not worth reporting as a clean fetch.
            result.partial = True
            result.expected_total = _claimed_total(payload)
            log.warning(
                "board_page_cap_reached",
                provider=provider.id,
                slug=slug,
                fetched=len(result.jobs),
                pages=pages,
            )

        if provider is Workday and include_description:
            await self._enrich_workday(slug, result)

        return result

    async def _enrich_workday(self, slug: str, result: BoardResult) -> None:
        """Workday's listing carries no description and only a relative date. The
        detail endpoint is the only place the real values live."""
        semaphore = asyncio.Semaphore(self._settings.discovery_concurrency)

        async def one(job: RawJob) -> RawJob:
            async with semaphore:
                try:
                    _, payload = await self._request(
                        Workday.detail_endpoint(slug, job), key=Workday.id
                    )
                    return Workday.merge_detail(job, payload)
                except Exception:
                    return job  # a missing description is not worth failing the board

        result.jobs = list(await asyncio.gather(*(one(j) for j in result.jobs)))

    # ─── Detection ──────────────────────────────────────────────────────────

    async def detect(self, target: str) -> list[tuple[type[ATSProvider], str, str]]:
        """Work out which ATS (if any) serves this domain, URL or 'provider:slug'.

        Returns (provider, slug, discovered_via) candidates, best first.
        """
        # 1. Named outright — "greenhouse:stripe"
        if ":" in target and "//" not in target:
            head, _, tail = target.partition(":")
            if provider := PROVIDERS_BY_ID.get(head.strip().lower()):
                return [(provider, tail.strip(), "explicit")]

        host, _, _ = parse_target(target)

        # 2. The hostname identifies the ATS
        if (provider := provider_from_host(host)) and (slug := provider.slug_from(target)):
            return [(provider, slug, "url")]

        # 3. Guess from the domain, probing every provider concurrently
        if host:
            base = (provider_from_host(host) is None and host) or host
            candidates: list[tuple[type[ATSProvider], str]] = []
            for prov in PROVIDERS:
                if prov is Workday:
                    continue  # needs host::site; only the careers page can supply it
                for variant in slug_variants(prov.slug_from(host)):
                    if variant:
                        candidates.append((prov, variant))

            hits = await self._probe(candidates)
            if hits:
                return [(p, s, "domain_guess") for p, s in hits]

            # 4. Read the careers page. This is what makes "just give it a domain" work.
            found = await discover_from_website(host, self.client, self._settings)
            out: list[tuple[type[ATSProvider], str, str]] = []
            for provider_id, slug in found:
                provider = PROVIDERS_BY_ID.get(provider_id)
                if provider is not None:
                    out.append((provider, slug, "careers_page"))
            if out:
                log.info("ats_discovered", host=host, found=[(p.id, s) for p, s, _ in out])
                return out
            _ = base

        return []

    async def _probe(
        self, candidates: list[tuple[type[ATSProvider], str]]
    ) -> list[tuple[type[ATSProvider], str]]:
        """Ask each candidate board for a single page, concurrently.

        A provider whose `empty_means_exists` is False proves nothing by
        answering empty, so an empty response from those is not a hit.
        """
        semaphore = asyncio.Semaphore(self._settings.discovery_concurrency)

        async def probe_one(
            provider: type[ATSProvider], slug: str
        ) -> tuple[type[ATSProvider], str] | None:
            async with semaphore:
                url = provider.endpoint(slug, include_description=False, offset=0)
                body = provider.body(slug, 0) if provider.method == "POST" else None
                try:
                    status, payload = await self._request(
                        url, provider.method, body, key=provider.id
                    )
                except Exception:
                    return None
                if status >= 400:
                    return None
                try:
                    parsed = provider.parse(payload, slug)
                except Exception as exc:
                    # Provider payloads vary in shape between tenants. One
                    # unusual response must not abort a 60-company run.
                    log.warning(
                        "provider_parse_failed",
                        provider=provider.id,
                        slug=slug,
                        error=f"{type(exc).__name__}: {exc}"[:160],
                    )
                    return None
                if parsed is None:
                    return None
                if not parsed and not provider.empty_means_exists:
                    return None
                return provider, slug

        results = await asyncio.gather(*(probe_one(p, s) for p, s in candidates))
        return [r for r in results if r is not None]

    # ─── Top level ──────────────────────────────────────────────────────────

    async def fetch_for_target(
        self, target: str, include_description: bool = True
    ) -> list[BoardResult]:
        """Domain, board URL or 'provider:slug' → every job on its board(s)."""
        detected = await self.detect(target)
        results = await self._fetch_detected(target, detected, include_description)

        # A guessed slug can land on a real but ABANDONED board. Razorpay has a
        # registered `razorpay.breezy.hr` that answers 200 with an empty list,
        # while their actual openings are on Greenhouse under a different slug.
        # Accepting the empty board would report "no openings" for a company
        # that is hiring — so an empty guess is treated as no answer, and the
        # careers page gets the last word.
        guessed_and_empty = (
            results
            and all(not r.jobs for r in results)
            and all(r.discovered_via in ("domain_guess", "unknown") for r in results)
        )
        if guessed_and_empty and (host := parse_target(target)[0]):
            found = await discover_from_website(host, self.client, self._settings)
            from_page = [
                (PROVIDERS_BY_ID[pid], slug, "careers_page")
                for pid, slug in found
                if pid in PROVIDERS_BY_ID
            ]
            if from_page:
                better = await self._fetch_detected(target, from_page, include_description)
                if any(r.jobs for r in better):
                    return better

        if not results:
            return [BoardResult(provider="unknown", slug=target, error="no_ats_detected")]
        return results

    async def _fetch_detected(
        self,
        target: str,
        detected: list[tuple[type[ATSProvider], str, str]],
        include_description: bool,
    ) -> list[BoardResult]:
        results = []
        for provider, slug, via in detected:
            board = await self.fetch_board(provider, slug, include_description)
            board.discovered_via = via
            results.append(board)
            log.info(
                "board_fetched",
                target=target,
                provider=provider.id,
                slug=slug,
                via=via,
                jobs=len(board.jobs),
                error=board.error,
            )
        return results

    async def fetch_many(
        self, targets: list[str], include_description: bool = True
    ) -> dict[str, list[BoardResult]]:
        semaphore = asyncio.Semaphore(self._settings.discovery_concurrency)

        async def one(target: str) -> tuple[str, list[BoardResult]]:
            async with semaphore:
                try:
                    return target, await self.fetch_for_target(target, include_description)
                except Exception as exc:
                    # A batch run is the common case. Losing 59 companies
                    # because the 60th behaved oddly is not acceptable.
                    log.warning(
                        "target_failed", target=target, error=f"{type(exc).__name__}: {exc}"[:160]
                    )
                    return target, [
                        BoardResult(provider="unknown", slug=target, error=f"{type(exc).__name__}")
                    ]

        return dict(await asyncio.gather(*(one(t) for t in targets)))
