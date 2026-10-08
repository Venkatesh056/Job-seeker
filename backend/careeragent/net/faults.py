"""Fault injection — making other people's servers misbehave on purpose.

Every network call in this system already has a happy path that is tested. What is not
tested, and what actually happens in production, is the other side being *slightly* wrong:
a 200 carrying an HTML error page, a JSON body that stops halfway, a 429 with no
`Retry-After`, a connection that opens and then never says anything.

Those are the failures that corrupt data rather than raising. A timeout produces an
exception somebody handles; a truncated JSON array produces **twelve jobs where there were
forty**, no error anywhere, and a board that silently looks half empty forever.

This is an `httpx` transport rather than a mock of our own functions, and that distinction
is the point. Patching `ATSFetcher._request` would test the code above the patch and skip
the retry decorator, the rate limiter, the status handling and the JSON parsing — which is
precisely the layer where these bugs live. A transport sits underneath all of it, so the
real client code runs, unmodified, against a hostile server.

    async with fault_client({"*greenhouse*": Fault.truncated_json()}) as client:
        async with ATSFetcher(client) as fetcher:
            result = await fetcher.fetch_board(Greenhouse, "acme")
    assert result.error  # and not: forty jobs quietly becoming twelve
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
from collections.abc import Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..logging import get_logger

log = get_logger("net.faults")


@dataclass
class Fault:
    """One way a server can be unhelpful.

    Constructed through the classmethods rather than by filling in fields, so each named
    failure mode is a thing with a name — and the name is what a test asserts against.
    """

    kind: str
    status: int = 200
    body: bytes = b""
    headers: dict[str, str] = field(default_factory=dict)
    delay_seconds: float = 0.0
    # Fail this many times, then behave. Zero means always fail.
    times: int = 0
    _seen: int = 0

    # ─── The ones that raise ────────────────────────────────────────────────

    @classmethod
    def timeout(cls, times: int = 0) -> Fault:
        """Opens, then says nothing. The most common real failure and the least dramatic."""
        return cls(kind="timeout", times=times)

    @classmethod
    def connect_error(cls, times: int = 0) -> Fault:
        """DNS gone, port closed, network down."""
        return cls(kind="connect_error", times=times)

    @classmethod
    def reset(cls, times: int = 0) -> Fault:
        """Connection accepted and then dropped mid-response."""
        return cls(kind="reset", times=times)

    # ─── The ones that return ───────────────────────────────────────────────

    @classmethod
    def status_code(cls, code: int, *, retry_after: str = "", times: int = 0) -> Fault:
        headers = {"retry-after": retry_after} if retry_after else {}
        return cls(kind=f"http_{code}", status=code, headers=headers, body=b"", times=times)

    @classmethod
    def rate_limited(cls, *, retry_after: str = "2", times: int = 0) -> Fault:
        return cls.status_code(429, retry_after=retry_after, times=times)

    @classmethod
    def slow(cls, seconds: float, body: Any = None) -> Fault:
        """Answers correctly, eventually. Tests the difference between a timeout budget and
        a hang."""
        return cls(
            kind="slow",
            status=200,
            body=json.dumps(body if body is not None else {}).encode(),
            delay_seconds=seconds,
        )

    # ─── The ones that lie ──────────────────────────────────────────────────

    @classmethod
    def malformed_json(cls) -> Fault:
        """200, `content-type: application/json`, and not JSON."""
        return cls(
            kind="malformed_json",
            status=200,
            body=b'{"jobs": [{"id": 1, "title": "Engineer"',
            headers={"content-type": "application/json"},
        )

    @classmethod
    def truncated_json(cls, keep: int = 3, total: int = 40) -> Fault:
        """A valid JSON array that is missing most of its rows.

        The worst failure in the catalogue, because nothing anywhere raises. A board with
        forty openings returns three, the parse succeeds, the rows are stored, and the
        corpus is quietly wrong until somebody notices months later that a company "stopped
        posting jobs".
        """
        jobs = [
            {"id": i, "title": f"Engineer {i}", "absolute_url": f"https://example.test/{i}"}
            for i in range(keep)
        ]
        return cls(
            kind="truncated_json",
            status=200,
            body=json.dumps({"jobs": jobs, "meta": {"total": total}}).encode(),
            headers={"content-type": "application/json"},
        )

    @classmethod
    def html_error_page(cls) -> Fault:
        """200 OK, and the body is a Cloudflare interstitial.

        Every CDN does this. The status line says success and the payload is an apology,
        which is why anything that trusts a 200 without looking at what arrived will happily
        parse "Attention Required!" into a job listing.
        """
        return cls(
            kind="html_error_page",
            status=200,
            body=(
                b"<!DOCTYPE html><html><head><title>Attention Required! | Cloudflare</title>"
                b"</head><body><h1>Sorry, you have been blocked</h1></body></html>"
            ),
            headers={"content-type": "text/html; charset=utf-8"},
        )

    @classmethod
    def empty_body(cls) -> Fault:
        """200 with nothing in it. Reads as "no jobs" to anything that does not check."""
        return cls(
            kind="empty_body",
            status=200,
            body=b"",
            headers={"content-type": "application/json"},
        )

    @classmethod
    def wrong_shape(cls, payload: Any = None) -> Fault:
        """Valid JSON of a shape the parser does not expect — an API version change."""
        return cls(
            kind="wrong_shape",
            status=200,
            body=json.dumps(payload if payload is not None else {"data": None}).encode(),
            headers={"content-type": "application/json"},
        )

    @classmethod
    def huge_body(cls, megabytes: int = 40) -> Fault:
        """A response far larger than anything legitimate. Memory, not correctness."""
        filler = json.dumps([{"id": i, "title": "x" * 512} for i in range(megabytes * 1500)])
        return cls(
            kind="huge_body",
            status=200,
            body=filler.encode(),
            headers={"content-type": "application/json"},
        )

    @classmethod
    def ok(cls, payload: Any) -> Fault:
        """Not a fault. For the "then succeeds" half of a flaky sequence."""
        return cls(
            kind="ok",
            status=200,
            body=json.dumps(payload).encode(),
            headers={"content-type": "application/json"},
        )

    # ─── Application ────────────────────────────────────────────────────────

    def _exhausted(self) -> bool:
        """Whether this fault has already fired as many times as it was asked to."""
        return self.times > 0 and self._seen >= self.times

    async def apply(self, request: httpx.Request) -> httpx.Response:
        self._seen += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)

        if self.kind == "timeout":
            raise httpx.ReadTimeout("injected timeout", request=request)
        if self.kind == "connect_error":
            raise httpx.ConnectError("injected connection failure", request=request)
        if self.kind == "reset":
            raise httpx.RemoteProtocolError("injected connection reset", request=request)

        return httpx.Response(
            status_code=self.status,
            content=self.body,
            headers=self.headers or {},
            request=request,
        )


@dataclass
class Recorded:
    method: str
    url: str
    fault: str


class FaultTransport(httpx.AsyncBaseTransport):
    """Routes requests to faults by URL glob, and records what was asked for.

    The recording is half the value: the questions worth asking about a failure are usually
    "how many times did it retry" and "did it stop", and both need the call log rather than
    the result.
    """

    def __init__(
        self,
        faults: dict[str, Fault] | None = None,
        *,
        default: Fault | None = None,
        passthrough: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._faults = dict(faults or {})
        # No default means an unmatched URL is a mistake in the test, not a silent network
        # call. A fault suite that quietly reached the real internet would be worse than
        # useless — slow, flaky, and occasionally rude to somebody's server.
        self._default = default or Fault.status_code(599)
        self._passthrough = passthrough
        self.calls: list[Recorded] = []

    def _match(self, url: str) -> Fault:
        for pattern, fault in self._faults.items():
            if fnmatch.fnmatch(url, pattern) and not fault._exhausted():
                return fault
        # A fault with `times` set has stopped firing: the server has recovered, so serve
        # the follow-up rather than the default. This is what makes `flaky` expressible.
        for pattern, fault in self._faults.items():
            if fnmatch.fnmatch(url, pattern) and fault._exhausted():
                return self._faults.get(pattern + "#then", Fault.ok({"jobs": []}))
        return self._default

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        fault = self._match(url)
        self.calls.append(Recorded(method=request.method, url=url, fault=fault.kind))
        log.debug("fault_injected", url=url[:120], fault=fault.kind)
        return await fault.apply(request)

    # ─── What a test asks afterwards ────────────────────────────────────────

    def attempts(self, pattern: str = "*") -> int:
        return sum(1 for call in self.calls if fnmatch.fnmatch(call.url, pattern))

    def kinds(self) -> list[str]:
        return [call.fault for call in self.calls]


@asynccontextmanager
async def fault_client(
    faults: dict[str, Fault] | None = None,
    *,
    default: Fault | None = None,
    timeout: float = 5.0,
    **client_kwargs: Any,
) -> Any:
    """An `httpx.AsyncClient` that talks to a server built out of faults.

    Yields the client with `.transport` attached, so a test can read the call log:

        async with fault_client({"*": Fault.timeout(times=2)}) as client:
            ...
        assert client.transport.attempts() == 3
    """
    transport = FaultTransport(faults, default=default)
    async with httpx.AsyncClient(
        transport=transport, timeout=timeout, follow_redirects=True, **client_kwargs
    ) as client:
        client.transport = transport  # type: ignore[attr-defined]
        yield client


def flaky(pattern: str, fault: Fault, then: Fault) -> Iterator[tuple[str, Fault]]:
    """Fail `fault.times` times on `pattern`, then answer with `then`.

        dict(chain(flaky("*acme*", Fault.timeout(times=2), Fault.ok({"jobs": []}))))

    Spelled as a helper because the "#then" key that makes it work is an implementation
    detail of the transport, and a test that had to know it would be testing the harness.
    """
    yield pattern, fault
    yield pattern + "#then", then
