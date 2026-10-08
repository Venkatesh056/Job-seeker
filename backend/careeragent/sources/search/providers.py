"""Tavily and SerpAPI.

Two backends for the same question, with very different economics, and the difference
decides which one is asked first. Tavily's free tier is 1,000 credits a month; SerpAPI's
is 250 searches. So Tavily leads and SerpAPI is the fallback — the opposite of the usual
"best result first" instinct, because a company hunt that burns 250 searches in one run
leaves nothing for the rest of the month.

Neither is asked to summarise or judge. They return links; deciding what a link means
happens elsewhere, against the company's own site.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

import httpx

from ...logging import get_logger
from .base import SearchHit, SearchResult

log = get_logger("sources.search.providers")


def _env(*names: str) -> str:
    """A key from the environment, then the OS keyring. Never returned by the API."""
    for name in names:
        if value := os.environ.get(name, "").strip():
            return value
    try:
        import keyring

        from ...config import KEYRING_SERVICE

        for name in names:
            stored = keyring.get_password(KEYRING_SERVICE, name.lower())
            if stored:
                return stored.strip()
    except Exception:
        pass
    return ""


class Tavily:
    """https://tavily.com — 1,000 credits a month free, built for agent use.

    Asked first because of the credit budget, not because its results are better. Its
    `include_domains`/`exclude_domains` support is what keeps job boards out of a company
    hunt at the source rather than filtering them afterwards, which saves a credit.
    """

    id = "tavily"
    label = "Tavily"
    signup = "https://tavily.com"
    env_vars = ("TAVILY_API_KEY",)

    def configured(self) -> bool:
        return bool(_env(*self.env_vars))

    async def search(
        self, query: str, *, client: httpx.AsyncClient, limit: int = 10, region: str = ""
    ) -> SearchResult:
        result = SearchResult(provider=self.id, query=query)
        key = _env(*self.env_vars)
        if not key:
            result.error = "no Tavily key configured"
            return result

        payload: dict[str, Any] = {
            "query": query,
            "max_results": max(1, min(limit, 20)),
            "search_depth": "basic",
            "include_answer": False,
            "include_raw_content": False,
        }
        if region:
            # A name, not a code: Tavily rejects "in" with HTTP 400 and accepts "india".
            from ..directory.providers import COUNTRY_NAMES

            if name := COUNTRY_NAMES.get(region.lower()):
                payload["country"] = name.lower()

        try:
            response = await client.post(
                "https://api.tavily.com/search",
                json=payload,
                headers={"authorization": f"Bearer {key}"},
            )
            result.calls = 1
        except httpx.HTTPError as exc:
            result.error = f"{type(exc).__name__}"
            return result

        if response.status_code == 429:
            result.error = "HTTP 429"
            result.quota_exhausted = True
            return result
        if response.status_code == 432:
            # Tavily's own "plan limit reached" code.
            result.error = "plan limit reached"
            result.quota_exhausted = True
            return result
        if response.status_code >= 400:
            result.error = f"HTTP {response.status_code}"
            return result

        try:
            body = response.json()
        except ValueError:
            result.error = "unparseable JSON"
            return result

        for row in (body.get("results") or [])[:limit]:
            url = (row.get("url") or "").strip()
            if not url:
                continue
            result.hits.append(
                SearchHit(
                    title=(row.get("title") or "").strip()[:300],
                    url=url,
                    snippet=(row.get("content") or "").strip()[:600],
                    source=self.id,
                )
            )
        return result


class SerpAPI:
    """https://serpapi.com — Google's actual results, 250 searches a month free.

    The fallback, because that budget is small. Worth having: it returns what Google
    returns, which finds company sites that an index-based search misses entirely.
    """

    id = "serpapi"
    label = "SerpAPI"
    signup = "https://serpapi.com"
    env_vars = ("SERPAPI_API_KEY",)

    # Google's country codes, for the regions this is built to search.
    REGIONS: ClassVar[dict[str, tuple[str, str]]] = {
        "in": ("in", "google.co.in"),
        "us": ("us", "google.com"),
        "gb": ("uk", "google.co.uk"),
        "sg": ("sg", "google.com.sg"),
        "ae": ("ae", "google.ae"),
        "au": ("au", "google.com.au"),
        "ca": ("ca", "google.ca"),
        "de": ("de", "google.de"),
    }

    def configured(self) -> bool:
        return bool(_env(*self.env_vars))

    async def search(
        self, query: str, *, client: httpx.AsyncClient, limit: int = 10, region: str = ""
    ) -> SearchResult:
        result = SearchResult(provider=self.id, query=query)
        key = _env(*self.env_vars)
        if not key:
            result.error = "no SerpAPI key configured"
            return result

        params: dict[str, Any] = {
            "engine": "google",
            "q": query,
            "api_key": key,
            "num": max(1, min(limit, 20)),
        }
        if region and (mapped := self.REGIONS.get(region.lower())):
            params["gl"], params["google_domain"] = mapped

        try:
            response = await client.get("https://serpapi.com/search.json", params=params)
            result.calls = 1
        except httpx.HTTPError as exc:
            result.error = f"{type(exc).__name__}"
            return result

        if response.status_code == 429:
            result.error = "HTTP 429"
            result.quota_exhausted = True
            return result
        if response.status_code >= 400:
            result.error = f"HTTP {response.status_code}"
            return result

        try:
            body = response.json()
        except ValueError:
            result.error = "unparseable JSON"
            return result

        if error := body.get("error"):
            result.error = str(error)[:120]
            # SerpAPI reports an exhausted plan in the body, not the status code.
            result.quota_exhausted = "run out" in str(error).lower() or (
                "limit" in str(error).lower()
            )
            return result

        for row in (body.get("organic_results") or [])[:limit]:
            url = (row.get("link") or "").strip()
            if not url:
                continue
            result.hits.append(
                SearchHit(
                    title=(row.get("title") or "").strip()[:300],
                    url=url,
                    snippet=(row.get("snippet") or "").strip()[:600],
                    source=self.id,
                )
            )
        return result


# Ordered by how much free budget each has, not by result quality. The first configured
# provider that answers wins; the rest are never called.
PROVIDERS: list[Any] = [Tavily(), SerpAPI()]
PROVIDERS_BY_ID = {p.id: p for p in PROVIDERS}


def available() -> list[Any]:
    return [p for p in PROVIDERS if p.configured()]


def describe() -> list[dict[str, Any]]:
    return [
        {
            "id": p.id,
            "label": p.label,
            "configured": p.configured(),
            "signup": p.signup,
            "env_vars": list(p.env_vars),
        }
        for p in PROVIDERS
    ]
