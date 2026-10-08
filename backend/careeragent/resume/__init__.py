"""Resume ingestion, tailoring, validation and rendering.

Parsing is non-LLM by design: every fact must trace to a line the candidate
actually wrote (parser.py). Tailoring is selection-first, with LLM rephrasing as
an optional refinement that the validator can reject (tailor.py).

The order matters and is enforced by the module boundaries:

    parse → Fact Bank → tailor → validate → render

`render_pdf` refuses an unvalidated document, so there is no path from a
fabricated claim to a file that could be attached to an application.
"""

from .archetypes import ARCHETYPES, Archetype, for_title
from .documents import Bullet, Entry, TailoredResume
from .parser import ParsedResume, parse_resume, to_candidate
from .render import NotValidated, render_pdf, to_html, to_text, write_html, write_text

# `tailor` the function is deliberately NOT re-exported. It would shadow
# `careeragent.resume.tailor` the module — `from careeragent.resume import tailor` would
# hand back the callable, and every attribute lookup on it would fail with a confusing
# AttributeError. That cost two debugging sessions. Import it from the submodule:
#
#     from careeragent.resume.tailor import tailor
#
from .tailor import TailorResult, job_keywords, tailor_deterministic
from .validate import Report, Violation, validate

__all__ = [
    "ARCHETYPES",
    "Archetype",
    "Bullet",
    "Entry",
    "NotValidated",
    "ParsedResume",
    "Report",
    "TailorResult",
    "TailoredResume",
    "Violation",
    "for_title",
    "job_keywords",
    "parse_resume",
    "render_pdf",
    "tailor_deterministic",
    "to_candidate",
    "to_html",
    "to_text",
    "validate",
    "write_html",
    "write_text",
]
