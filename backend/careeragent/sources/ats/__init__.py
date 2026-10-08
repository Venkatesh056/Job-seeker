"""Public ATS job-board feed readers."""

from .discover import discover_from_website, domain_of, extract_signatures
from .fetcher import ATSFetcher, BoardResult
from .providers import PROVIDERS, PROVIDERS_BY_ID, ATSProvider, provider_from_host, slug_variants

__all__ = [
    "PROVIDERS",
    "PROVIDERS_BY_ID",
    "ATSFetcher",
    "ATSProvider",
    "BoardResult",
    "discover_from_website",
    "domain_of",
    "extract_signatures",
    "provider_from_host",
    "slug_variants",
]
