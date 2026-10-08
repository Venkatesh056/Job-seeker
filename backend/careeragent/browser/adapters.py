"""ATS adapters.

Each adapter answers three questions about one platform: is this page mine, how
do I read its form, and how do I submit it. Everything else — which value goes
in which field, and whether the form is safe to submit — is shared, because it
is the same problem on every platform.

That is why adapters are this small. The canonical field catalog and the
pre-submit verdict do the work that would otherwise be duplicated, badly, once
per ATS.

Interface (docs/01-architecture.md §7):

    detect()   is this page this ATS?
    extract()  DOM -> canonical fields          (gather only)
    fill()     canonical values -> DOM          (verified after every write)
    verify()   re-read state                    (gather only)
    submit()   activate the submit control      (guarded)
    recover()  what to do about a failed step
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..logging import get_logger
from .catalog import FieldKind, classify
from .verdict import FieldState

log = get_logger("browser.adapters")

EXTRACT_JS = (Path(__file__).parent / "extract.js").read_text(encoding="utf-8")


@dataclass
class FormField:
    """One control on the page, with its canonical meaning attached."""

    label: str
    kind: str
    selector: str
    canonical_field: str
    required: bool = False
    evidence: str = "none"
    value: str = ""
    placeholder: str = ""
    options: list[dict[str, Any]] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)

    def to_state(self) -> FieldState:
        merged = {**self.raw, "canonical_field": self.canonical_field}
        return FieldState.from_dict(merged)


@dataclass
class FormSpec:
    ats: str
    url: str
    title: str = ""
    fields: list[FormField] = field(default_factory=list)
    submits: list[dict[str, Any]] = field(default_factory=list)
    multi_step: bool = False

    def by_canonical(self, field_id: str) -> FormField | None:
        return next((f for f in self.fields if f.canonical_field == field_id), None)

    @property
    def required_fields(self) -> list[FormField]:
        return [f for f in self.fields if f.required]


class ATSAdapter(Protocol):
    name: str

    def detect(self, url: str, html: str) -> float: ...
    def parse(self, payload: dict[str, Any], url: str) -> FormSpec: ...


# ─── Shared implementation ──────────────────────────────────────────────────


class BaseAdapter:
    """Everything common to every ATS. A platform adapter overrides only what
    genuinely differs, which in practice is detection and a handful of quirks."""

    name = "generic"
    host_patterns: tuple[str, ...] = ()
    dom_markers: tuple[str, ...] = ()

    def detect(self, url: str, html: str) -> float:
        """Confidence that this page belongs to this ATS, in [0, 1].

        A host match is near-certain; DOM markers are weaker but cover embedded
        boards served from the company's own domain.
        """
        lowered_url = (url or "").lower()
        for pattern in self.host_patterns:
            if re.search(pattern, lowered_url):
                return 1.0
        lowered_html = (html or "")[:200_000].lower()
        hits = sum(1 for marker in self.dom_markers if marker.lower() in lowered_html)
        if hits:
            return min(0.5 + 0.2 * hits, 0.95)
        return 0.0

    def parse(self, payload: dict[str, Any], url: str) -> FormSpec:
        """Turn the raw gather into canonical fields."""
        spec = FormSpec(
            ats=self.name,
            url=payload.get("url") or url,
            title=payload.get("title") or "",
            submits=payload.get("submits") or [],
        )
        for raw in payload.get("fields") or []:
            label = str(raw.get("label") or "")
            kind_name = str(raw.get("kind") or "unknown")
            try:
                kind = FieldKind(kind_name)
            except ValueError:
                kind = FieldKind.UNKNOWN

            canonical = classify(label, kind)
            spec.fields.append(
                FormField(
                    label=label,
                    kind=kind_name,
                    selector=str(raw.get("selector") or ""),
                    canonical_field=canonical.id,
                    required=bool(raw.get("required")),
                    evidence=str(raw.get("evidence") or "none"),
                    value=str(raw.get("value") or ""),
                    placeholder=str(raw.get("placeholder") or ""),
                    options=list(raw.get("options") or []),
                    raw=raw,
                )
            )
        spec.multi_step = self.is_multi_step(spec)
        return spec

    def is_multi_step(self, spec: FormSpec) -> bool:
        """Most ATS application forms are one page. Workday is the notable
        exception and overrides this."""
        return False


class GreenhouseAdapter(BaseAdapter):
    name = "greenhouse"
    host_patterns = (r"greenhouse\.io", r"job-boards\.greenhouse\.io")
    dom_markers = ("greenhouse", "data-board-token", "grnhse")


class LeverAdapter(BaseAdapter):
    name = "lever"
    host_patterns = (r"jobs\.lever\.co", r"jobs\.eu\.lever\.co")
    dom_markers = ("lever-application", 'data-qa="application-form"', "lever.co")


class AshbyAdapter(BaseAdapter):
    name = "ashby"
    host_patterns = (r"jobs\.ashbyhq\.com", r"ashbyhq\.com")
    dom_markers = ("ashby", "_ashby_application", "data-ashby")


class WorkableAdapter(BaseAdapter):
    name = "workable"
    host_patterns = (r"apply\.workable\.com", r"workable\.com")
    dom_markers = ("workable", "whr-")


class SmartRecruitersAdapter(BaseAdapter):
    name = "smartrecruiters"
    host_patterns = (r"jobs\.smartrecruiters\.com", r"smartrecruiters\.com")
    dom_markers = ("smartrecruiters", "sr-application")


class WorkdayAdapter(BaseAdapter):
    """Workday needs an account per tenant, with email verification.

    That is a human step by design, so this adapter reads the form and then
    stops. Automating account creation is out of bounds — see
    docs/02-roadmap.md §1.
    """

    name = "workday"
    host_patterns = (r"myworkdayjobs\.com", r"wd\d+\.myworkday")
    dom_markers = ("workday", "wd-", "data-automation-id")
    requires_account = True

    def is_multi_step(self, spec: FormSpec) -> bool:
        return True


class GenericAdapter(BaseAdapter):
    """Fallback for an unrecognised application system.

    Detection returns a floor rather than zero so there is always something to
    read a form with — but because the ATS is unknown, the runner never
    auto-submits through this adapter regardless of autonomy level.
    """

    name = "generic"

    def detect(self, url: str, html: str) -> float:
        return 0.1


ADAPTERS: tuple[BaseAdapter, ...] = (
    GreenhouseAdapter(),
    LeverAdapter(),
    AshbyAdapter(),
    WorkableAdapter(),
    SmartRecruitersAdapter(),
    WorkdayAdapter(),
)

GENERIC = GenericAdapter()

ADAPTERS_BY_NAME: dict[str, BaseAdapter] = {a.name: a for a in (*ADAPTERS, GENERIC)}

# Adapters cleared for unattended submission at autonomy level 5. An ATS not
# listed here always stops for a human, however high the match score.
AUTO_SUBMIT_ALLOWED = frozenset({"greenhouse", "lever", "ashby"})


def detect_ats(url: str, html: str = "") -> tuple[BaseAdapter, float]:
    """Pick the adapter for this page, with its confidence."""
    best: BaseAdapter = GENERIC
    score = 0.0
    for adapter in ADAPTERS:
        confidence = adapter.detect(url, html)
        if confidence > score:
            best, score = adapter, confidence
    if score == 0.0:
        log.info("ats_unrecognised", url=url[:120])
        return GENERIC, GENERIC.detect(url, html)
    return best, score


def may_auto_submit(ats: str) -> bool:
    return ats in AUTO_SUBMIT_ALLOWED
