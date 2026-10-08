"""LLM routing, caching and budget control.

One entry point for every model call in the system. It owns four things that
must not be re-decided per call site:

  * **Tiering.** Cheap models do extraction, classification and batch scoring —
    the bulk of calls. Strong models are reserved for tailoring, company
    reasoning and open questions, at roughly 50x the cost.
  * **Failover.** Providers retire models without warning (both obvious first
    picks for this project were already dead), so a chain is tried in order.
  * **Caching.** The same job description is re-analysed on every run. Caching
    on a content hash is what makes repeat runs nearly free.
  * **Budget.** A hard daily ceiling that degrades to "no LLM" rather than
    quietly spending. The funnel already limits volume; this is the backstop.

Everything degrades rather than raises. A missing key, an exhausted budget or a
dead provider returns None, and callers fall back to the deterministic path —
the system is designed to be useful without an LLM at all.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from ..config import get_api_key, get_settings
from ..logging import get_logger

log = get_logger("llm.router")


class Tier(StrEnum):
    CHEAP = "cheap"
    STRONG = "strong"


@dataclass
class LLMResult:
    text: str
    model: str
    tier: Tier
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cost_usd: float = 0.0
    latency_ms: int = 0
    cache_hit: bool = False
    agent: str = "unknown"
    attempts: list[str] = field(default_factory=list)
    # The model hit `max_tokens` and stopped mid-sentence. Distinguishing this from a
    # malformed reply is the difference between "raise the cap" and "the model is broken",
    # and getting it wrong cost a subsystem that was quietly falling back on most jobs.
    truncated: bool = False

    def json(self) -> Any | None:
        """Parse the reply as JSON, tolerating the wrappers models add.

        Fenced blocks and a sentence of preamble are common even with explicit
        instructions, so this recovers the object rather than failing the call.
        """
        return extract_json(self.text)


def extract_json(text: str) -> Any | None:
    """Pull the first JSON value out of a model reply. None if there is none."""
    if not text:
        return None
    cleaned = text.strip()

    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fall back to the outermost balanced object or array.
    for opener, closer in (("{", "}"), ("[", "]")):
        start = cleaned.find(opener)
        end = cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


# ─── Budget ─────────────────────────────────────────────────────────────────


class Budget:
    """A per-day spend ceiling, persisted so a restart cannot reset it."""

    def __init__(self, limit_usd: float, path: Path) -> None:
        self.limit = limit_usd
        self.path = path
        self._today = datetime.now(UTC).strftime("%Y-%m-%d")
        self._spent = self._load()

    def _load(self) -> float:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
            return float(data.get(self._today, 0.0))
        except (OSError, json.JSONDecodeError, ValueError):
            return 0.0

    def _save(self) -> None:
        try:
            data = {}
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
            data[self._today] = round(self._spent, 6)
            # Keep a fortnight of history; the rest is noise.
            for day in sorted(data)[:-14]:
                data.pop(day, None)
            self.path.write_text(json.dumps(data, indent=1), encoding="utf-8")
        except (OSError, json.JSONDecodeError):
            pass  # an unwritable ledger must not block work

    @property
    def spent(self) -> float:
        return self._spent

    @property
    def remaining(self) -> float:
        return max(0.0, self.limit - self._spent)

    def allows(self) -> bool:
        return self._spent < self.limit

    def record(self, cost: float) -> None:
        self._spent += max(0.0, cost)
        self._save()


# ─── Router ─────────────────────────────────────────────────────────────────

# Current models spend completion tokens on hidden reasoning before emitting a single
# visible token, and that reasoning is charged against `max_tokens`. It is mostly a fixed
# cost rather than a multiple of the answer: Gemini spent 893 tokens producing a 120-word
# answer (~200 tokens of text), then 1596 on the same shape of question. Both were cut off.
#
# The important economics: `max_tokens` is a **ceiling, not a charge**. You pay for what the
# model emits, so an over-generous cap costs nothing when the model finishes early, while a
# tight one silently destroys the whole reply. Runaway generation is bounded by the daily
# budget, which is the right place for that limit. So this is deliberately generous.
REASONING_FLOOR = 3_000
VISIBLE_MULTIPLIER = 2


def tokens_for(visible_tokens: int) -> int:
    """A completion cap for an answer of roughly `visible_tokens` of actual text.

    Call sites state the size of the answer they want rather than guessing at a raw cap,
    because the gap between those two numbers is not obvious and getting it wrong is
    invisible: the reply is cut mid-string, parses as nothing, and the caller falls back
    having already paid for the call.
    """
    return REASONING_FLOOR + visible_tokens * VISIBLE_MULTIPLIER


PROVIDER_OF_MODEL = {
    "groq": "groq",
    "gemini": "gemini",
    "anthropic": "anthropic",
    "openai": "openai",
    "xai": "xai",
    "ollama": "ollama",  # local; needs no key
}


class LLMRouter:
    def __init__(self, budget: Budget | None = None) -> None:
        self.settings = get_settings()
        cache_root = Path(self.settings.cache_dir)
        self.cache_dir = cache_root / "llm"
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.budget = budget or Budget(
            self.settings.llm_daily_budget_usd, cache_root / "llm_budget.json"
        )
        self._calls: list[LLMResult] = []

    # ─── Availability ───────────────────────────────────────────────────────

    def chain(self, tier: Tier) -> list[str]:
        models = (
            self.settings.llm_chain_cheap if tier is Tier.CHEAP else self.settings.llm_chain_strong
        )
        return [m for m in models if self._usable(m)]

    @staticmethod
    def _usable(model: str) -> bool:
        provider = model.split("/", 1)[0]
        if provider == "ollama":
            return True
        key_name = PROVIDER_OF_MODEL.get(provider)
        return bool(key_name and get_api_key(key_name))

    def available(self) -> bool:
        return bool(self.chain(Tier.CHEAP) or self.chain(Tier.STRONG))

    # ─── Cache ──────────────────────────────────────────────────────────────

    def _cache_key(self, model: str, prompt: str, system: str, temperature: float) -> str:
        basis = f"{model}|{temperature}|{system}|{prompt}"
        return hashlib.sha256(basis.encode("utf-8")).hexdigest()[:40]

    def _cached(self, key: str) -> str | None:
        if not self.settings.llm_cache_enabled:
            return None
        path = self.cache_dir / f"{key}.json"
        try:
            return json.loads(path.read_text(encoding="utf-8"))["text"]
        except (OSError, json.JSONDecodeError, KeyError):
            return None

    def _store(self, key: str, text: str, model: str) -> None:
        if not self.settings.llm_cache_enabled:
            return
        with contextlib.suppress(OSError):
            (self.cache_dir / f"{key}.json").write_text(
                json.dumps({"text": text, "model": model, "at": datetime.now(UTC).isoformat()}),
                encoding="utf-8",
            )

    # ─── Completion ─────────────────────────────────────────────────────────

    def complete(
        self,
        prompt: str,
        *,
        tier: Tier = Tier.CHEAP,
        system: str = "",
        temperature: float = 0.1,
        max_tokens: int | None = None,
        json_mode: bool = False,
        agent: str = "unknown",
    ) -> LLMResult | None:
        """One completion. Returns None when no model could answer.

        None is a normal outcome, not an error: no key, no budget, every model
        in the chain failing. Callers fall back to deterministic behaviour.
        """
        chain = self.chain(tier)
        if not chain:
            log.debug("llm_unavailable", tier=tier.value, agent=agent)
            return None

        # gpt-oss models spend completion tokens on hidden reasoning before
        # emitting anything; too low a cap returns an empty string.
        cap = max(max_tokens or self.settings.llm_min_max_tokens, self.settings.llm_min_max_tokens)

        attempts: list[str] = []
        for model in chain:
            key = self._cache_key(model, prompt, system, temperature)
            if (hit := self._cached(key)) is not None:
                result = LLMResult(
                    text=hit,
                    model=model,
                    tier=tier,
                    cache_hit=True,
                    agent=agent,
                    attempts=attempts,
                )
                self._calls.append(result)
                return result

            if not self.budget.allows():
                log.warning(
                    "llm_budget_exhausted",
                    spent=round(self.budget.spent, 4),
                    limit=self.budget.limit,
                    agent=agent,
                )
                return None

            attempts.append(model)
            started = time.perf_counter()
            try:
                import litellm

                litellm.suppress_debug_info = True

                messages = []
                if system:
                    messages.append({"role": "system", "content": system})
                messages.append({"role": "user", "content": prompt})

                kwargs: dict[str, Any] = {
                    "model": model,
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": cap,
                    "timeout": 90,
                }
                if json_mode:
                    kwargs["response_format"] = {"type": "json_object"}

                response = litellm.completion(**kwargs)
            except Exception as exc:
                log.warning(
                    "llm_model_failed",
                    model=model,
                    agent=agent,
                    error=f"{type(exc).__name__}: {exc}".splitlines()[0][:160],
                )
                continue

            latency = int((time.perf_counter() - started) * 1000)
            choice = response.choices[0]
            text = (choice.message.content or "").strip()
            truncated = str(getattr(choice, "finish_reason", "") or "").lower() in {
                "length",
                "max_tokens",
            }
            usage = getattr(response, "usage", None)
            cost = float(getattr(response, "_hidden_params", {}).get("response_cost") or 0.0)
            self.budget.record(cost)

            if not text:
                # Almost always the reasoning budget eating the whole cap.
                log.warning("llm_empty_response", model=model, agent=agent, max_tokens=cap)
                continue

            if truncated:
                log.warning(
                    "llm_response_truncated",
                    model=model,
                    agent=agent,
                    max_tokens=cap,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    effect="the reply was cut off; raise max_tokens for this agent",
                )
            # A truncated reply is not cached: it would be served forever as though it were
            # the model's actual answer.
            if not truncated:
                self._store(key, text, model)
            result = LLMResult(
                text=text,
                model=model,
                tier=tier,
                truncated=truncated,
                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                cost_usd=cost,
                latency_ms=latency,
                agent=agent,
                attempts=attempts,
            )
            self._calls.append(result)
            log.info(
                "llm_call",
                agent=agent,
                model=model,
                tier=tier.value,
                tokens=result.prompt_tokens + result.completion_tokens,
                cost_usd=round(cost, 6),
                latency_ms=latency,
            )
            return result

        log.warning("llm_all_models_failed", tier=tier.value, agent=agent, tried=attempts)
        return None

    def complete_json(self, prompt: str, *, tier: Tier = Tier.CHEAP, **kwargs: Any) -> Any | None:
        """A completion parsed as JSON. None if the model failed or replied with
        something unparseable — never a half-built object."""
        result = self.complete(prompt, tier=tier, json_mode=True, **kwargs)
        if result is None:
            return None
        parsed = result.json()
        if parsed is None:
            # Truncation and malformation look identical in a preview — both show JSON that
            # stops — but they call for opposite responses, so they are reported apart.
            log.warning(
                "llm_response_truncated_unparseable"
                if result.truncated
                else "llm_unparseable_json",
                model=result.model,
                agent=kwargs.get("agent", "unknown"),
                preview=result.text[:120],
                tail=result.text[-80:] if result.truncated else None,
            )
        return parsed

    # ─── Reporting ──────────────────────────────────────────────────────────

    def drain_calls(self) -> list[LLMResult]:
        """Hand over the calls made so far and forget them.

        The router is synchronous and the database is not, so it cannot persist its
        own calls. Draining lets the async caller write them once per run — which
        also means `llm_call` rows are grouped by run, which is what the cost
        breakdown needs.
        """
        calls, self._calls = self._calls, []
        return calls

    @property
    def stats(self) -> dict[str, Any]:
        calls = self._calls
        return {
            "calls": len(calls),
            "cache_hits": sum(1 for c in calls if c.cache_hit),
            "cost_usd": round(sum(c.cost_usd for c in calls), 6),
            "tokens": sum(c.prompt_tokens + c.completion_tokens for c in calls),
            "budget_spent_usd": round(self.budget.spent, 6),
            "budget_remaining_usd": round(self.budget.remaining, 6),
        }


_router: LLMRouter | None = None


def get_router() -> LLMRouter:
    global _router
    if _router is None:
        _router = LLMRouter()
    return _router
