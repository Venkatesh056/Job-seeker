"""Structured logging with PII redaction.

The realistic risk in this system is not an attacker — it is the system itself
leaking the candidate's own data into logs. Redaction is therefore a processor in
the pipeline, not a convention for callers to remember.
"""

from __future__ import annotations

import logging
import re
import sys
from collections.abc import MutableMapping
from typing import Any

import structlog

# Keys whose values never appear in logs, at any level.
SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "cookie",
        "cookies",
        "password",
        "secret",
        "token",
        "session",
        "storage_state",
        "phone",
        "email",
        "address",
        "resume_text",
        "cover_letter",
        "answer",
    }
)

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
_PHONE = re.compile(r"(?<!\w)(?:\+?\d{1,3}[\s.-]?)?(?:\(?\d{3}\)?[\s.-]?)?\d{3}[\s.-]?\d{4}(?!\w)")
_BEARER = re.compile(r"(?i)\b(bearer|sk-|gsk_|xai-)[A-Za-z0-9._\-]{8,}")

REDACTED = "«redacted»"


def _scrub_text(value: str) -> str:
    value = _EMAIL.sub(REDACTED, value)
    value = _BEARER.sub(REDACTED, value)
    return _PHONE.sub(REDACTED, value)


def _scrub(value: Any, depth: int = 0) -> Any:
    if depth > 6:  # guard against pathological nesting
        return value
    if isinstance(value, str):
        return _scrub_text(value)
    if isinstance(value, dict):
        return {
            k: (REDACTED if k.lower() in SENSITIVE_KEYS else _scrub(v, depth + 1))
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        return type(value)(_scrub(v, depth + 1) for v in value)
    return value


def redact_processor(
    _logger: Any, _name: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """structlog processor: drop sensitive keys, scrub PII from what remains.

    The signature is structlog's, not ours: a processor takes the logger, the method
    name and the event dict, and returns the dict the next processor sees.
    """
    scrubbed = _scrub(dict(event_dict))
    return scrubbed if isinstance(scrubbed, dict) else dict(event_dict)


def configure_logging(level: str = "INFO", json_output: bool = False) -> None:
    logging.basicConfig(
        format="%(message)s", stream=sys.stdout, level=getattr(logging, level.upper(), logging.INFO)
    )

    renderer = (
        structlog.processors.JSONRenderer()
        if json_output
        else structlog.dev.ConsoleRenderer(colors=sys.stdout.isatty())
    )

    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            redact_processor,
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            renderer,
        ],
        wrapper_class=structlog.make_filtering_bound_logger(
            getattr(logging, level.upper(), logging.INFO)
        ),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str) -> Any:
    return structlog.get_logger(name)
