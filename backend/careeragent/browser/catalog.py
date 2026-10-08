"""Canonical form-field vocabulary.

The abstraction that keeps ATS adapters small. One vocabulary sits between the
candidate knowledge base and every ATS: each canonical field is resolved exactly
twice — once from the Fact Bank (`resolver.py`, pure, no LLM) and once per ATS
to a selector (each adapter's `extract`).

Without this, every adapter ends up with its own field-name guessing, and adding
an ATS means re-solving a problem that was already solved.

Derived from the field catalog in `neonwatty/job-apply-plugin` (MIT); see NOTICE.
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field


class FieldKind(enum.StrEnum):
    TEXT = "text"
    TEXTAREA = "textarea"
    EMAIL = "email"
    PHONE = "phone"
    URL = "url"
    SELECT = "select"
    COMBOBOX = "combobox"
    RADIO = "radio"
    CHECKBOX = "checkbox"
    FILE = "file"
    DATE = "date"
    UNKNOWN = "unknown"


class Sensitivity(enum.StrEnum):
    """How a field may be filled."""

    SAFE = "safe"
    # Demographics, veteran status, disability, sponsorship declarations, access
    # codes. Never auto-filled at any autonomy level: a guessed answer here is
    # both wrong and, for the demographic questions, offensive. Always human.
    NEVER_FILL = "never_fill"
    # Answerable, but only from evidence — never invented.
    NEEDS_EVIDENCE = "needs_evidence"


@dataclass(frozen=True)
class CanonicalField:
    id: str
    kind: FieldKind
    label: str
    sensitivity: Sensitivity = Sensitivity.SAFE
    patterns: tuple[str, ...] = field(default_factory=tuple)


def _f(
    id_: str,
    kind: FieldKind,
    label: str,
    *patterns: str,
    sensitivity: Sensitivity = Sensitivity.SAFE,
) -> CanonicalField:
    return CanonicalField(id_, kind, label, sensitivity, tuple(patterns))


CATALOG: tuple[CanonicalField, ...] = (
    # ─── Contact ────────────────────────────────────────────────────────────
    _f("contact.first_name", FieldKind.TEXT, "First name", r"first\s*name", r"^given\s*name"),
    _f(
        "contact.last_name",
        FieldKind.TEXT,
        "Last name",
        r"last\s*name",
        r"surname",
        r"family\s*name",
    ),
    _f(
        "contact.full_name", FieldKind.TEXT, "Full name", r"^full\s*name", r"^name$", r"your\s*name"
    ),
    _f("contact.preferred_name", FieldKind.TEXT, "Preferred name", r"preferred\s*(first\s*)?name"),
    _f("contact.email", FieldKind.EMAIL, "Email", r"e-?mail"),
    _f("contact.phone", FieldKind.PHONE, "Phone", r"phone", r"mobile", r"contact\s*number"),
    _f("contact.phone_country", FieldKind.SELECT, "Phone country", r"country\s*code"),
    _f(
        "contact.location",
        FieldKind.COMBOBOX,
        "Location",
        r"^location",
        r"current\s*(location|city|residence)",
        r"where.*(based|located)",
        r"city\s*(and|,)\s*(state|country)",
    ),
    _f("contact.city", FieldKind.TEXT, "City", r"^city"),
    _f("contact.state", FieldKind.TEXT, "State", r"^state", r"province", r"region"),
    _f("contact.country", FieldKind.SELECT, "Country", r"^country$"),
    _f("contact.postal_code", FieldKind.TEXT, "Postal code", r"postal|zip\s*code|pin\s*code"),
    _f("contact.address", FieldKind.TEXT, "Address", r"street|address\s*line"),
    # ─── Files ──────────────────────────────────────────────────────────────
    _f("resume.file", FieldKind.FILE, "Resume", r"resume|cv\b|curriculum"),
    _f("cover_letter.file", FieldKind.FILE, "Cover letter", r"cover\s*letter"),
    _f(
        "cover_letter.text",
        FieldKind.TEXTAREA,
        "Cover letter text",
        r"cover\s*letter",
        r"anything else.*(share|add|know)",
        r"additional\s*(information|comments)",
        sensitivity=Sensitivity.NEEDS_EVIDENCE,
    ),
    # ─── Profiles ───────────────────────────────────────────────────────────
    _f("profile.linkedin", FieldKind.URL, "LinkedIn", r"linked\s*-?in"),
    _f("profile.github", FieldKind.URL, "GitHub", r"git\s*hub"),
    _f("profile.portfolio", FieldKind.URL, "Portfolio", r"portfolio|personal\s*(site|website)"),
    _f("profile.website", FieldKind.URL, "Website", r"^website", r"^url$"),
    _f("profile.other", FieldKind.URL, "Other profile", r"other\s*(website|link|profile)"),
    # ─── Employment ─────────────────────────────────────────────────────────
    _f(
        "employment.current_company",
        FieldKind.TEXT,
        "Current company",
        r"current\s*(company|employer)",
    ),
    _f(
        "employment.current_title",
        FieldKind.TEXT,
        "Current title",
        r"current\s*(title|role|position)",
    ),
    _f(
        "experience.years_total",
        FieldKind.TEXT,
        "Years of experience",
        r"years?\s*of\s*(relevant\s*)?experience",
        r"how\s*many\s*years",
        sensitivity=Sensitivity.NEEDS_EVIDENCE,
    ),
    _f("education.degree", FieldKind.SELECT, "Degree", r"degree|qualification\s*level"),
    _f("education.school", FieldKind.COMBOBOX, "School", r"school|university|college|institution"),
    _f(
        "education.discipline",
        FieldKind.COMBOBOX,
        "Discipline",
        r"discipline|major|field\s*of\s*study",
    ),
    # ─── Logistics ──────────────────────────────────────────────────────────
    _f(
        "availability.start_date",
        FieldKind.DATE,
        "Start date",
        r"start\s*date",
        r"when\s*can\s*you\s*start",
        r"notice\s*period",
        sensitivity=Sensitivity.NEEDS_EVIDENCE,
    ),
    _f(
        "compensation.expected",
        FieldKind.TEXT,
        "Expected compensation",
        r"salary|compensation\s*expectation|expected\s*ctc",
        sensitivity=Sensitivity.NEEDS_EVIDENCE,
    ),
    _f(
        "preference.relocate",
        FieldKind.RADIO,
        "Willing to relocate",
        r"relocat",
        sensitivity=Sensitivity.NEEDS_EVIDENCE,
    ),
    _f("preference.remote", FieldKind.RADIO, "Remote preference", r"remote\s*(work|preference)"),
    _f("preference.top_choice", FieldKind.CHECKBOX, "Top choice", r"top\s*choice"),
    _f(
        "referral.source",
        FieldKind.SELECT,
        "How did you hear about us",
        r"how did you (hear|find)",
        r"source|referr",
    ),
    # ─── Never filled ───────────────────────────────────────────────────────
    # Work authorisation and sponsorship are legal declarations. A wrong answer
    # is a misrepresentation on a real application, so the agent never guesses.
    _f(
        "authorization.work_authorized",
        FieldKind.RADIO,
        "Work authorised",
        r"authoriz\w*\s*to\s*work",
        r"work\s*authoriz",
        r"legally.*(work|authorized)",
        r"right to work",
        r"eligible to work",
        sensitivity=Sensitivity.NEVER_FILL,
    ),
    _f(
        "authorization.sponsorship",
        FieldKind.RADIO,
        "Requires sponsorship",
        r"sponsor",
        r"visa",
        r"work permit",
        sensitivity=Sensitivity.NEVER_FILL,
    ),
    _f(
        "demographic.gender",
        FieldKind.SELECT,
        "Gender",
        r"\bgender\b",
        sensitivity=Sensitivity.NEVER_FILL,
    ),
    _f(
        "demographic.race",
        FieldKind.SELECT,
        "Race / ethnicity",
        r"\brace\b",
        r"ethnic",
        r"hispanic|latino",
        sensitivity=Sensitivity.NEVER_FILL,
    ),
    _f(
        "demographic.veteran",
        FieldKind.SELECT,
        "Veteran status",
        r"veteran|protected\s*vet",
        sensitivity=Sensitivity.NEVER_FILL,
    ),
    _f(
        "demographic.disability",
        FieldKind.SELECT,
        "Disability status",
        r"disab",
        sensitivity=Sensitivity.NEVER_FILL,
    ),
    _f(
        "demographic.pronouns",
        FieldKind.SELECT,
        "Pronouns",
        r"pronoun",
        sensitivity=Sensitivity.NEVER_FILL,
    ),
    _f(
        "security.access_code",
        FieldKind.TEXT,
        "Access code",
        r"access\s*code|password|security\s*code|pin\b",
        sensitivity=Sensitivity.NEVER_FILL,
    ),
    _f(
        "legal.previous_employment",
        FieldKind.RADIO,
        "Worked here before",
        r"worked (for|at) (this|us|our)",
        r"previously employed",
        sensitivity=Sensitivity.NEVER_FILL,
    ),
    _f(
        "legal.non_compete",
        FieldKind.RADIO,
        "Non-compete",
        r"non-?compete|restrictive covenant",
        sensitivity=Sensitivity.NEVER_FILL,
    ),
    _f(
        "legal.background_check",
        FieldKind.CHECKBOX,
        "Background check consent",
        r"background check|consent to",
        sensitivity=Sensitivity.NEVER_FILL,
    ),
)

BY_ID: dict[str, CanonicalField] = {f.id: f for f in CATALOG}

FREEFORM_PREFIX = "question.freeform:"


def _normalize(label: str) -> str:
    return re.sub(r"\s+", " ", label.lower().replace("*", " ").replace("_", " ")).strip()


def classify(label: str, kind: FieldKind = FieldKind.UNKNOWN) -> CanonicalField:
    """Map a form label onto a canonical field.

    Order matters: NEVER_FILL patterns are tested first, so a field like
    "Are you authorized to work in the US?" can never be mistaken for a generic
    radio and auto-answered. Failing safe is the whole point.

    Anything unrecognised becomes a stable `question.freeform:<hash>` id, which
    routes to the question answerer and keeps its answer reusable across
    applications rather than being regenerated each time.
    """
    normalized = _normalize(label)
    if not normalized:
        return CanonicalField(
            f"{FREEFORM_PREFIX}unlabeled", kind, label, Sensitivity.NEEDS_EVIDENCE
        )

    # NEVER_FILL is tested first and alone, so a sensitive field can never be
    # captured by some other rule that happens to match. This ordering is the
    # safety invariant, not a convenience.
    for canonical in CATALOG:
        if canonical.sensitivity is Sensitivity.NEVER_FILL and any(
            re.search(p, normalized) for p in canonical.patterns
        ):
            return canonical

    # Everything else matches in catalog order, so declaration order expresses
    # precedence. Where several entries share a label ("Cover letter" is both a
    # file upload and a textarea), the control's own kind decides.
    matches = [
        canonical
        for canonical in CATALOG
        if canonical.sensitivity is not Sensitivity.NEVER_FILL
        and any(re.search(p, normalized) for p in canonical.patterns)
    ]
    if matches:
        if kind is not FieldKind.UNKNOWN:
            for canonical in matches:
                if canonical.kind is kind:
                    return canonical
        return matches[0]

    import hashlib

    digest = hashlib.sha256(normalized.encode()).hexdigest()[:12]
    return CanonicalField(f"{FREEFORM_PREFIX}{digest}", kind, label, Sensitivity.NEEDS_EVIDENCE)


def is_freeform(field_id: str) -> bool:
    return field_id.startswith(FREEFORM_PREFIX)


def never_fill(field_id: str) -> bool:
    """True when the agent must leave this to a human, at any autonomy level."""
    canonical = BY_ID.get(field_id)
    return canonical is not None and canonical.sensitivity is Sensitivity.NEVER_FILL
