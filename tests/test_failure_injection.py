"""What happens when the other side misbehaves.

Every network call in this system has a tested happy path. This file tests the rest, and it
exists because the failures that matter are not the dramatic ones. A timeout raises and
somebody catches it. The dangerous failures return **200 OK** with something almost right in
the body:

* a JSON array that stops halfway — forty jobs become three, nothing raises, and the board
  looks like it stopped hiring
* a Cloudflare interstitial served as `200 text/html` — parsed, if anything trusts a status
  line over a payload
* an empty body, which reads as "no openings" to anything that does not check

The tests run the **real** fetcher against an `httpx` transport that misbehaves. Patching
`ATSFetcher._request` would skip the retry decorator, the rate limiter, the status handling
and the JSON parsing — which is exactly the layer where these bugs live.
"""

from __future__ import annotations

from itertools import chain

import httpx
import pytest

from careeragent.net.faults import Fault, fault_client, flaky
from careeragent.net.limits import Limit, Limiter, MemoryBackend, retry_after_seconds
from careeragent.sources.ats.fetcher import ATSFetcher
from careeragent.sources.ats.providers import Greenhouse

GREENHOUSE = "*greenhouse*"


async def _board(faults: dict[str, Fault], *, slug: str = "acme"):
    """Fetch one board through a hostile transport. Returns (result, transport)."""
    async with fault_client(faults, timeout=2.0) as client:
        async with ATSFetcher(client) as fetcher:
            result = await fetcher.fetch_board(Greenhouse, slug, include_description=False)
        return result, client.transport


# ─── The ones that lie ──────────────────────────────────────────────────────


async def test_an_html_error_page_served_as_200_is_not_parsed_as_jobs():
    """Every CDN does this. The status line says success and the body is an apology.

    Anything that trusts a 200 without looking at what arrived will parse "Attention
    Required!" into a job listing — and a company's board will fill with one opening called
    "Sorry, you have been blocked".
    """
    result, _ = await _board({GREENHOUSE: Fault.html_error_page()})

    assert not result.jobs, f"parsed {len(result.jobs)} 'jobs' out of a Cloudflare page"
    assert result.error, "an HTML error page was accepted as a successful fetch"


async def test_malformed_json_fails_the_board_rather_than_returning_nothing():
    """The distinction that matters downstream: a board that *failed* is retried later; a
    board that returned zero jobs is a company that stopped hiring."""
    result, _ = await _board({GREENHOUSE: Fault.malformed_json()})

    assert result.error, "unparseable JSON was reported as an empty board"
    assert not result.ok


async def test_an_empty_body_is_not_reported_as_a_successful_empty_board():
    result, _ = await _board({GREENHOUSE: Fault.empty_body()})
    assert result.error or not result.jobs
    # The real requirement: whatever else happens, this must not look like a healthy fetch
    # that found nothing.
    assert not (result.ok and result.jobs == [])


async def test_a_response_of_the_wrong_shape_does_not_raise_out_of_the_fetcher():
    """An API version change is a Tuesday. It should produce an error on one board, not an
    exception that takes down a run over twenty-four of them."""
    result, _ = await _board({GREENHOUSE: Fault.wrong_shape({"data": None})})
    assert not result.ok or not result.jobs


# ─── The ones that raise ────────────────────────────────────────────────────


async def test_a_timeout_is_retried_and_then_given_up_on():
    """Bounded. An unbounded retry against a server that is down is a way to be banned by
    one that is up."""
    result, transport = await _board({GREENHOUSE: Fault.timeout()})

    assert result.error, "a timeout was reported as success"
    attempts = transport.attempts(GREENHOUSE)
    assert 1 < attempts <= 4, f"{attempts} attempts — retries are unbounded or absent"


async def test_a_connection_failure_is_reported_not_swallowed():
    result, _ = await _board({GREENHOUSE: Fault.connect_error()})
    assert result.error and not result.jobs


async def test_a_flaky_board_recovers_without_a_human():
    """Two failures then an answer is the commonest real pattern, and the one retries exist
    for. If this did not pass, the retry policy would be costing time and buying nothing."""
    faults = dict(
        chain(
            flaky(
                GREENHOUSE,
                Fault.timeout(times=2),
                Fault.ok({"jobs": [], "meta": {"total": 0}}),
            )
        )
    )
    result, transport = await _board(faults)

    assert result.ok, f"gave up on a board that recovered: {result.error}"
    assert transport.attempts(GREENHOUSE) == 3


async def test_a_server_error_is_retried_but_a_404_is_not():
    """404 is a definite answer — this company does not use this ATS — and retrying a
    definite answer is how a detection sweep across fourteen providers becomes slow enough
    to be useless."""
    _, five_hundred = await _board({GREENHOUSE: Fault.status_code(503)})
    _, four_oh_four = await _board({GREENHOUSE: Fault.status_code(404)})

    assert five_hundred.attempts(GREENHOUSE) > 1, "a 5xx was treated as final"
    assert four_oh_four.attempts(GREENHOUSE) == 1, "a 404 was retried"


# ─── Rate limiting under pressure ───────────────────────────────────────────


async def test_a_429_makes_the_limiter_back_off_for_the_time_the_server_asked_for():
    """The server's own number outranks ours. A limiter that ignores `Retry-After` is
    guessing against a server that has stopped guessing."""
    limiter = Limiter(backend=MemoryBackend(), limits={"greenhouse": Limit(rate=100.0, burst=10)})

    assert await limiter.try_acquire("greenhouse") is True
    await limiter.penalise("greenhouse", 30.0)
    assert await limiter.try_acquire("greenhouse") is False, "a 429 penalty was not applied"


async def test_a_penalty_longer_than_the_patience_budget_raises_rather_than_hangs():
    """So a caller can report "this provider is saturated" instead of appearing to freeze
    for half an hour."""
    from careeragent.net.limits import RateLimited

    limiter = Limiter(
        backend=MemoryBackend(),
        limits={"tavily": Limit(rate=100.0, burst=10)},
        max_wait_seconds=1.0,
    )
    await limiter.penalise("tavily", 600.0)

    with pytest.raises(RateLimited):
        await limiter.acquire("tavily")


async def test_the_limiter_spaces_out_a_burst_that_exceeds_the_bucket():
    """The bucket has to actually delay something, or it is decoration."""
    import time

    limiter = Limiter(backend=MemoryBackend(), limits={"slow": Limit(rate=20.0, burst=2)})

    started = time.monotonic()
    for _ in range(5):
        await limiter.acquire("slow")
    elapsed = time.monotonic() - started

    # Two free from the burst, three at 20/second ≈ 0.15s. Asserted as a floor with plenty
    # of headroom, because a timing test that asserts a ceiling fails on a loaded machine
    # for reasons that have nothing to do with the code.
    assert elapsed >= 0.1, f"five calls against a burst of two took {elapsed:.3f}s — no limiting"


async def test_a_token_is_never_partially_spent():
    """A caller that took half a permit and waited would let two callers each hold half and
    both proceed — which is the over-spend the bucket exists to prevent."""
    backend = MemoryBackend()
    limit = Limit(rate=1.0, burst=1)

    assert await backend.take("k", limit, 1.0) == 0.0
    first = await backend.take("k", limit, 1.0)
    second = await backend.take("k", limit, 1.0)
    assert first > 0 and second > 0
    # The second refusal must not be cheaper than the first: if the first call had consumed
    # what was left in the bucket, it would be.
    assert second >= first - 0.05


@pytest.mark.parametrize(
    ("header", "expected"),
    [("5", 5.0), ("0", 0.0), ("", 5.0), ("not-a-number", 5.0), (None, 5.0)],
    ids=["seconds", "zero", "missing", "garbage", "absent"],
)
def test_retry_after_is_parsed_or_defaulted_but_never_zero_by_accident(header, expected):
    """A 429 with no header still means slow down. Reading a missing header as "no delay
    requested" retries immediately into the same wall."""
    headers = httpx.Headers({"retry-after": header} if header is not None else {})
    assert retry_after_seconds(headers) == pytest.approx(expected, abs=0.01)


# ─── The harness itself ─────────────────────────────────────────────────────


async def test_an_unmatched_url_is_a_failure_rather_than_a_real_request():
    """A fault suite that quietly reached the real internet would be slow, flaky, and
    occasionally rude to somebody's server."""
    async with fault_client({"*example.test*": Fault.ok({"ok": True})}) as client:
        response = await client.get("https://api.greenhouse.io/v1/boards/acme/jobs")
        assert response.status_code == 599, "an unmatched URL escaped to the network"


async def test_the_transport_records_what_was_asked_for():
    async with fault_client({"*": Fault.ok({"jobs": []})}) as client:
        await client.get("https://example.test/one")
        await client.get("https://example.test/two")
        assert client.transport.attempts() == 2
        assert client.transport.kinds() == ["ok", "ok"]


# ─── Paging against a server that is wrong about itself ─────────────────────
#
# The bug this section found, and the reason the file exists. A board claiming
# `totalFound: 40` that kept returning the same three rows produced **42 jobs from 3
# distinct openings**, `ok=True`, no error, after 14 HTTP requests. Nothing was broken from
# the loop's point of view: it asked for more, it got rows, it counted them.


def _smartrecruiters():
    from careeragent.sources.ats.providers import PROVIDERS_BY_ID

    return PROVIDERS_BY_ID["smartrecruiters"]


def _posting(i: int) -> dict:
    return {
        "id": str(i),
        "name": f"Engineer {i}",
        "company": {"identifier": "acme"},
        "location": {"city": "Bangalore"},
        "releasedDate": "2026-09-01",
    }


def _page(rows: list[dict], total: int) -> Fault:
    import json

    return Fault(
        kind="page",
        status=200,
        body=json.dumps({"totalFound": total, "content": rows}).encode(),
        headers={"content-type": "application/json"},
    )


async def _smart_board(faults: dict[str, Fault]):
    async with fault_client(faults, timeout=2.0) as client:
        async with ATSFetcher(client) as fetcher:
            result = await fetcher.fetch_board(
                _smartrecruiters(), "acme", include_description=False
            )
        return result, client.transport


async def test_a_board_that_repeats_a_page_is_stopped_rather_than_paged_forever():
    """The measured failure: 42 jobs from 3 openings, reported as a clean fetch."""
    stuck = _page([_posting(i) for i in range(3)], total=40)
    result, transport = await _smart_board({"*smartrecruiters*": stuck})

    ids = [job.source_job_id for job in result.jobs]
    assert len(ids) == len(set(ids)), f"{len(ids)} rows from {len(set(ids))} openings"
    assert len(result.jobs) == 3
    assert transport.attempts() <= 3, f"{transport.attempts()} requests against a stuck board"


async def test_a_short_board_says_it_was_short_instead_of_looking_healthy():
    """The rows that arrived are real openings and are kept — discarding three because
    forty were promised would be the worse mistake. But a caller reporting "3 new jobs at
    Acme" off this is reporting a number nobody can trust, so the gap travels with it."""
    stuck = _page([_posting(i) for i in range(3)], total=40)
    result, _ = await _smart_board({"*smartrecruiters*": stuck})

    assert result.partial is True
    assert result.expected_total == 40
    assert result.shortfall == 37


async def test_a_genuinely_paginated_board_still_pages():
    """The regression risk in the fix above. Stopping early on a real multi-page board
    would turn a silent over-count into a silent under-count, which is worse — the first
    is caught by deduplication downstream and the second is not."""
    import json

    from careeragent.net.faults import FaultTransport

    pages = [
        _page([_posting(i) for i in range(0, 3)], total=7),
        _page([_posting(i) for i in range(3, 6)], total=7),
        _page([_posting(i) for i in range(6, 7)], total=7),
    ]

    class Sequence(FaultTransport):
        """Answers with the next page each time, like a board that works."""

        def __init__(self) -> None:
            super().__init__({})
            self._index = 0

        def _match(self, url: str) -> Fault:
            fault = pages[min(self._index, len(pages) - 1)]
            self._index += 1
            return fault

    transport = Sequence()
    async with httpx.AsyncClient(transport=transport, timeout=2.0) as client:
        client.transport = transport
        async with ATSFetcher(client) as fetcher:
            result = await fetcher.fetch_board(
                _smartrecruiters(), "acme", include_description=False
            )

    assert len(result.jobs) == 7, f"paged board returned {len(result.jobs)} of 7"
    assert result.partial is False
    assert json  # the import is used by the page builder above
