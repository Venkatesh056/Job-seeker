"""Application settings.

Secrets are never read from the database and never returned by the API. API keys
resolve in this order: explicit env var, then the OS keyring. Keyring is the
default store so that keys never sit in a .env file on disk.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

KEYRING_SERVICE = "careeragent"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_prefix="CAREERAGENT_",
        extra="ignore",
    )

    # Which database this process may damage. Read by `storage.safety`, which refuses
    # destructive operations outside "test". Both spellings of each name are accepted so
    # an existing .env keeps working; "development" is the default because a production
    # deployment should declare itself, and because every *unforced* drop is refused in
    # every environment except test regardless of what this says.
    env: Literal["dev", "development", "test", "staging", "prod", "production"] = "development"
    log_level: str = "INFO"
    log_json: bool = False

    # Unset means SQLite in the project data directory (see storage.resolve_url).
    # Deliberately a plain str rather than PostgresDsn: the default backend is
    # SQLite, and a Postgres-only type would make the default unconfigurable.
    database_url: str | None = None
    # Off unless asked for. A default-on Redis would mean every fresh checkout tries to
    # connect to a server that is not there, and the first thing a new user sees is a
    # connection error for a feature they did not want.
    redis_enabled: bool = False
    redis_url: str = "redis://localhost:6379/0"

    # Tracing. Without a key this is inert and `/api/v1/status` reports `enabled: false`
    # rather than implying traces are going somewhere. The durable audit trail —
    # llm_call, provider_call, agent_decision — does not depend on this or on anything
    # being reachable.
    langsmith_api_key: str | None = None
    langsmith_project: str = "careeragent"

    storage_dir: Path = PROJECT_ROOT / "data" / "storage"
    cache_dir: Path = PROJECT_ROOT / "data" / "cache"

    # ─── Identity ───────────────────────────────────────────────────────────
    # Off by default: the shipped configuration is one person running this against their
    # own job hunt on their own machine, and demanding a login to read your own resume
    # would be theatre with a real cost.
    #
    # On, it is not partially on. The same switch that requires a token on every request
    # is the one that turns the tenancy fallback into an exception, so there is no
    # deployment where requests are authenticated but queries are unscoped.
    multi_user: bool = False
    # Signs session tokens. Unset in single-user mode is fine — nothing is signed. Unset
    # with multi_user on is refused at startup rather than defaulted, because a default
    # signing key is a published one.
    auth_secret: str | None = None
    auth_token_ttl_hours: int = 12
    # Supabase issues HS256 tokens signed with the project's JWT secret. Setting it lets a
    # Supabase session be accepted by the same verifier, with no second notion of identity.
    supabase_url: str | None = None
    supabase_jwt_secret: str | None = None
    # Storage for resumes, renders and submission screenshots. Both this and the URL must
    # be set before the remote backend is used: a URL with no key produces 401s at the
    # moment somebody saves a resume, which is the worst time to find a half-configuration.
    supabase_service_key: str | None = None
    supabase_bucket: str = "careeragent"

    # ─── Discovery ──────────────────────────────────────────────────────────
    # A courteous, identifiable user agent. Several ATS APIs are public but
    # unversioned; being identifiable is the difference between a client and a bot.
    user_agent: str = "careeragent/0.1 (personal job-search assistant; +https://github.com/)"
    http_timeout_seconds: float = 20.0
    http_max_retries: int = 2
    discovery_concurrency: int = 8
    respect_robots_txt: bool = True

    # ─── LLM ────────────────────────────────────────────────────────────────
    llm_cache_enabled: bool = True
    llm_daily_budget_usd: float = 2.0
    # Model chains per task tier, in LiteLLM's "provider/model" form. First entry
    # wins; the rest are failover.
    #
    # Verified against live keys on 2026-09-12. Never assume a model id from
    # memory — providers retire them quietly, and both obvious first picks
    # (llama-3.3-70b-versatile, gemini-2.5-flash) were already gone. Run
    # `careeragent models` to see what your keys can actually reach.
    #
    # Cheap tier carries extraction, classification and batch scoring: the bulk
    # of all calls. gpt-oss-20b is the cheapest option that still supports tools
    # and structured output, which the extraction agents need.
    llm_chain_cheap: list[str] = Field(
        default_factory=lambda: [
            "groq/openai/gpt-oss-20b",  # ~$0.07/1M in, tools + structured output
            "gemini/gemini-3.1-flash-lite",  # comparable cost, different provider
            "groq/openai/gpt-oss-120b",
        ]
    )
    # Strong tier is reserved for resume tailoring, company reasoning and open
    # application questions — around 50x the cheap tier per call, which is
    # exactly why the funnel gates so hard before anything reaches it.
    llm_chain_strong: list[str] = Field(
        default_factory=lambda: [
            "gemini/gemini-3.6-flash",
            "groq/openai/gpt-oss-120b",
        ]
    )
    # gpt-oss models spend completion tokens on hidden reasoning before emitting
    # any content: a 40-token cap returned an empty string with 23 tokens burned
    # on reasoning. Anything lower risks truncating the answer away entirely.
    llm_min_max_tokens: int = 512
    llm_embedding_model: str = "BAAI/bge-small-en-v1.5"  # local, via fastembed
    llm_embedding_dim: int = 384

    # ─── Autonomy ───────────────────────────────────────────────────────────
    # Level 4 = prepare applications and queue them for approval. Never auto-submit
    # by default. See docs/01-architecture.md §9.
    autonomy_level: int = Field(default=4, ge=0, le=5)
    auto_submit_threshold: float = 0.92
    daily_application_cap: int = 15

    # ─── Career monitoring ──────────────────────────────────────────────────
    # Off by default: a background loop that fetches job boards is something the
    # user should switch on knowingly, not discover in their network log.
    monitor_enabled: bool = False
    # How often the sweep wakes. Each board has its own interval; this is only
    # how often we check whether any board is due.
    monitor_tick_minutes: int = 30
    # Boards per sweep. Keeps one wake-up bounded regardless of how many
    # companies have accumulated.
    monitor_boards_per_sweep: int = 25
    # The adaptive band. A board that posts is checked every 6 hours; one that
    # has been static for a fortnight drops to weekly.
    monitor_min_interval_hours: int = 6
    monitor_max_interval_hours: int = 168

    # ─── Platform rules ─────────────────────────────────────────────────────
    # LinkedIn/Indeed/Naukri/Glassdoor prohibit automated access in their terms.
    # Adapters exist but stay off unless deliberately enabled. docs/01 §10.
    enable_portal_adapters: bool = False

    def ensure_dirs(self) -> None:
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.cache_dir.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def get_settings() -> Settings:
    # Load .env into the process environment as well as into Settings. LiteLLM
    # reads provider keys straight from os.environ, so a key that only lived in
    # the Settings object would authenticate nothing.
    try:
        from dotenv import load_dotenv

        load_dotenv(PROJECT_ROOT / ".env", override=False)
    except ImportError:
        pass

    settings = Settings()
    settings.ensure_dirs()
    return settings


def get_api_key(provider: str) -> str | None:
    """Resolve a provider API key from the environment, then the OS keyring.

    Returns None rather than raising: a missing key for one provider should
    degrade routing to another, not crash the process.
    """
    import os

    env_names = {
        "openai": "OPENAI_API_KEY",
        "anthropic": "ANTHROPIC_API_KEY",
        "gemini": "GEMINI_API_KEY",
        "groq": "GROQ_API_KEY",
        "xai": "XAI_API_KEY",
    }
    if (name := env_names.get(provider)) and (value := os.environ.get(name)):
        return value

    try:
        import keyring

        return keyring.get_password(KEYRING_SERVICE, provider)
    except Exception:  # keyring backend unavailable (headless CI, etc.)
        return None
