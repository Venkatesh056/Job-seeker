"""Job sources.

Every source produces `RawJob`s and nothing else. ATS feeds are the primary
channel: they are published by companies for exactly this purpose, carry no
prohibition on automated access, and return structured data with descriptions.

Portal adapters (LinkedIn, Indeed, Naukri, Glassdoor) live in `sources.portals`
and are disabled unless `enable_portal_adapters` is set. See docs/01-architecture.md §10.
"""
