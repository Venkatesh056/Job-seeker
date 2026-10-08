"""Master resume → Fact Bank.

Every fact produced here carries an `evidence_locator` pointing back at the
exact line it came from, so any claim the system later makes can be traced to
something the candidate actually wrote. Nothing is inferred, embellished or
filled in: a field the resume does not state comes back empty, and the gap is
the user's to close.

Extraction is deliberately non-LLM. A model asked to "extract skills" will
happily add adjacent ones it expects to see, and those become fabricated claims
on real applications.

Facts land with `verified=False`. They are usable for matching immediately, but
must not appear in a generated resume until a human confirms them — which is the
review step in the ingest flow.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from ..logging import get_logger
from ..models.candidate import Candidate, EvidenceSource, Fact, FactKind
from .taxonomy import (
    ALIAS_TO_CANONICAL,
    AMBIGUOUS,
    HEADING_TO_SECTION,
    SKILL_EXCLUDED_SECTIONS,
)

log = get_logger("resume.parser")

SUPPORTED_SUFFIXES = {".pdf", ".docx", ".txt", ".md"}

_EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.]{2,}\b")
# Phone numbers are matched loosely and then VALIDATED by digit count, rather
# than with a rigid grouping pattern. Real resumes write "+91 98765 43210",
# "(555) 123-4567" and "+44 7700 900123", and any fixed group structure drops
# digits from at least one of them — silently, which is the worst outcome for a
# field that goes into application forms.
_PHONE_CANDIDATE = re.compile(r"\+?\s?\(?\d[\d\s().-]{7,17}\d")
MIN_PHONE_DIGITS, MAX_PHONE_DIGITS = 10, 13

_URL = re.compile(r"\b(?:https?://|www\.)[^\s,;)\]]+", re.IGNORECASE)
_LINKEDIN = re.compile(r"linkedin\.com/in/([\w-]+)", re.IGNORECASE)
_GITHUB = re.compile(r"github\.com/([\w-]+)", re.IGNORECASE)

_MONTHS = (
    "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec|"
    "january|february|march|april|june|july|august|september|october|november|december"
)
_DATE_RANGE = re.compile(
    rf"(?P<start>(?:{_MONTHS})?\.?\s*\d{{4}})\s*(?:-|–|—|to)\s*(?P<end>present|current|now|(?:{_MONTHS})?\.?\s*\d{{4}})",
    re.IGNORECASE,
)
_YEAR = re.compile(r"\b(19[89]\d|20[0-4]\d)\b")

_MONTH_NUM = {
    "jan": 1,
    "january": 1,
    "feb": 2,
    "february": 2,
    "mar": 3,
    "march": 3,
    "apr": 4,
    "april": 4,
    "may": 5,
    "jun": 6,
    "june": 6,
    "jul": 7,
    "july": 7,
    "aug": 8,
    "august": 8,
    "sep": 9,
    "sept": 9,
    "september": 9,
    "oct": 10,
    "october": 10,
    "nov": 11,
    "november": 11,
    "dec": 12,
    "december": 12,
}

_BULLET = re.compile(r"^\s*[-•·▪◦*•●]\s*")

# A line consisting solely of a date or date range.
_DATE_ONLY = re.compile(
    rf"[\[(]?\s*(?:{_MONTHS})?\.?\s*['’]?\d{{2,4}}\s*(?:[-–—]|to)\s*"
    rf"(?:present|current|now|(?:{_MONTHS})?\.?\s*['’]?\d{{2,4}})\s*[\])]?",
    re.IGNORECASE,
)


@dataclass
class Line:
    text: str
    page: int
    number: int
    # Original leading whitespace. Resumes indent continuation lines under their
    # heading, and that is often the only signal that "CGPA 8.7/10" belongs to
    # the degree above rather than being a new entry.
    indent: int = 0

    def locator(self, filename: str) -> str:
        return f"{filename}#page={self.page},line={self.number}"


@dataclass
class ParsedResume:
    filename: str
    content_hash: str
    raw_text: str
    lines: list[Line] = field(default_factory=list)
    sections: dict[str, list[Line]] = field(default_factory=dict)
    contact: dict[str, str] = field(default_factory=dict)
    facts: list[Fact] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def facts_of(self, kind: FactKind) -> list[Fact]:
        return [f for f in self.facts if f.kind is kind]


# ─── Text extraction ────────────────────────────────────────────────────────


def extract_text(path: Path) -> tuple[str, list[Line], list[str]]:
    """Return raw text, page/line-addressed lines, and any embedded hyperlinks."""
    suffix = path.suffix.lower()
    links: list[str] = []
    if suffix == ".pdf":
        pages, links = _extract_pdf(path)
    elif suffix == ".docx":
        pages = [_extract_docx(path)]
    elif suffix in {".txt", ".md"}:
        pages = [path.read_text(encoding="utf-8", errors="ignore")]
    else:
        raise ValueError(
            f"Unsupported resume format '{suffix}'. Supported: {', '.join(sorted(SUPPORTED_SUFFIXES))}"
        )

    lines: list[Line] = []
    for page_no, page_text in enumerate(pages, start=1):
        for line_no, text in enumerate(page_text.splitlines(), start=1):
            cleaned = " ".join(text.split())
            if cleaned:
                indent = len(text) - len(text.lstrip())
                lines.append(Line(text=cleaned, page=page_no, number=line_no, indent=indent))

    return "\n".join(pages), lines, links


def _extract_pdf(path: Path) -> tuple[list[str], list[str]]:
    """Return page texts plus any hyperlink URIs embedded in the PDF.

    Lines are rebuilt from CHARACTER positions rather than taken from
    `extract_text`. Resumes are laid out in overlapping columns and text boxes,
    and extract_text merges rows that are merely close together — on a real
    resume that wove the word "SKILLS" through "MongoDB", producing
    "M SKoInLgLoSD B" and silently losing three database skills.

    Grouping characters into horizontal bands and sorting by x reconstructs what
    a human actually sees.
    """
    try:
        import pdfplumber
    except ImportError as exc:  # pragma: no cover - dependency is pinned
        raise ValueError("pdfplumber is required to read PDF resumes") from exc

    pages: list[str] = []
    links: list[str] = []
    try:
        with pdfplumber.open(path) as pdf:
            for page in pdf.pages:
                # Anchor text in a resume is usually a bare word ("LinkedIn"),
                # so the real URL only exists as a link annotation.
                for link in page.hyperlinks or []:
                    if uri := link.get("uri"):
                        links.append(uri)

                if page.chars:
                    pages.append(_lines_from_chars(page.chars))
                else:
                    pages.append(page.extract_text(layout=True) or "")
        if any(p.strip() for p in pages):
            return pages, links
        log.warning("pdfplumber_returned_empty", file=path.name)
    except Exception as exc:
        log.warning("pdfplumber_failed", file=path.name, error=str(exc)[:200])

    try:
        from pypdf import PdfReader

        return [(page.extract_text() or "") for page in PdfReader(str(path)).pages], links
    except Exception as exc:
        raise ValueError(
            f"Could not read text from {path.name}. If it is a scanned image, "
            "the text has to be OCR'd or the resume re-exported from its source."
        ) from exc


# Characters within this many points of each other vertically are one line.
LINE_BAND_POINTS = 2.0
# A horizontal gap wider than this is a real space rather than kerning.
WORD_GAP_POINTS = 1.2


def _lines_from_chars(chars: list[dict]) -> str:
    """Rebuild visual lines from character positions."""
    bands: dict[int, list[dict]] = {}
    for char in chars:
        bands.setdefault(round(char["top"] / LINE_BAND_POINTS), []).append(char)

    out: list[str] = []
    for key in sorted(bands):
        row = sorted(bands[key], key=lambda c: c["x0"])
        parts: list[str] = []
        previous = None
        for char in row:
            if previous is not None and char["x0"] - previous["x1"] > WORD_GAP_POINTS:
                parts.append(" ")
            parts.append(char["text"])
            previous = char
        line = "".join(parts)
        if line.strip():
            # Preserve the left offset as indentation so entry grouping can use it.
            indent = " " * int(min(c["x0"] for c in row) / 6)
            out.append(indent + line.strip())
    return "\n".join(out)


def _extract_docx(path: Path) -> str:
    from docx import Document

    document = Document(str(path))
    parts = [p.text for p in document.paragraphs]
    # Skills are very often laid out in a table, and skipping tables loses them.
    for table in document.tables:
        for row in table.rows:
            cells = [c.text.strip() for c in row.cells if c.text.strip()]
            if cells:
                parts.append(" | ".join(cells))
    return "\n".join(parts)


# ─── Structure ──────────────────────────────────────────────────────────────


def _normalize_heading(text: str) -> str:
    return re.sub(r"[^a-z& ]", "", text.lower()).strip()


def _looks_like_heading(text: str) -> bool:
    """Typographic test, applied before any name matching.

    Resume headings are set in caps. Requiring that keeps a sentence such as
    "Built things; experience with Docker" from opening an Experience section
    halfway through a project description.
    """
    letters = [c for c in text if c.isalpha()]
    if not letters or len(text) > 60:
        return False
    return sum(c.isupper() for c in letters) / len(letters) >= 0.8


def section_for_heading(text: str) -> str | None:
    """Map a heading line to a section, or None.

    Exact names match first. Real headings are often compound — "INDUSTRY
    EXPERIENCE / FREELANCE PROJECT", "ACHIEVEMENTS & CERTIFICATIONS" — so a
    typographic heading also matches on any alias it contains, longest first so
    "work experience" wins over "experience".
    """
    candidate = _normalize_heading(text)
    if not candidate:
        return None
    if candidate in HEADING_TO_SECTION:
        return HEADING_TO_SECTION[candidate]
    if not _looks_like_heading(text):
        return None
    # Score every alias that appears, then prefer the one starting earliest:
    # "ACHIEVEMENTS & CERTIFICATIONS" is an achievements section that also
    # lists certificates, and the leading word is what the writer meant.
    # Longer aliases break ties so "work experience" beats "experience".
    best = None
    for alias in HEADING_TO_SECTION:
        match = re.search(rf"\b{re.escape(alias)}\b", candidate)
        if match is None:
            continue
        key = (match.start(), -len(alias), alias)
        if best is None or key < best:
            best = key
    return HEADING_TO_SECTION[best[2]] if best else None


def detect_sections(lines: list[Line]) -> dict[str, list[Line]]:
    """Split the resume into sections by heading."""
    sections: dict[str, list[Line]] = {"header": []}
    current = "header"

    for line in lines:
        stripped = _BULLET.sub("", line.text)
        if not _BULLET.match(line.text) and (section := section_for_heading(stripped)):
            current = section
            sections.setdefault(current, [])
            continue
        sections.setdefault(current, []).append(line)

    return sections


def extract_contact(
    lines: list[Line], raw_text: str, pdf_links: list[str] | None = None
) -> dict[str, str]:
    contact: dict[str, str] = {}
    head = "\n".join(line.text for line in lines[:15])

    # Embedded link annotations first. A resume typically shows anchor text
    # ("LinkedIn", "PORTFOLIO") and keeps the real URL in the PDF annotation, so
    # scanning the visible text alone finds nothing at all.
    for uri in pdf_links or []:
        lowered = uri.lower()
        if lowered.startswith("mailto:"):
            contact.setdefault("email", uri[7:].split("?")[0])
        elif "linkedin.com/in/" in lowered:
            contact.setdefault("linkedin", uri.split("?")[0])
        elif "github.com/" in lowered:
            # Project repos appear too; the profile is the bare-username one.
            tail = uri.split("?")[0].rstrip("/").split("github.com/")[-1]
            if tail and "/" not in tail:
                contact.setdefault("github", uri.split("?")[0])
        elif lowered.startswith("http"):
            contact.setdefault("portfolio", uri.split("?")[0])

    if "email" not in contact and (m := _EMAIL.search(raw_text)):
        contact["email"] = m.group(0)
    if "linkedin" not in contact and (m := _LINKEDIN.search(raw_text)):
        contact["linkedin"] = f"https://linkedin.com/in/{m.group(1)}"
    if "github" not in contact and (m := _GITHUB.search(raw_text)):
        contact["github"] = f"https://github.com/{m.group(1)}"

    # Phone only from the header block: a "2019-2021" date elsewhere reads as a
    # phone number to any permissive pattern.
    for m in _PHONE_CANDIDATE.finditer(head):
        digits = re.sub(r"\D", "", m.group(0))
        if MIN_PHONE_DIGITS <= len(digits) <= MAX_PHONE_DIGITS:
            contact["phone"] = m.group(0).strip(" .-")
            break

    for m in _URL.finditer(raw_text):
        url = m.group(0).rstrip(".,;")
        if "linkedin.com" in url or "github.com" in url:
            continue
        contact.setdefault("portfolio", url)
        break

    # The name is the first substantial line that is not contact details.
    for line in lines[:6]:
        text = line.text.strip()
        if not text or _EMAIL.search(text) or _URL.search(text):
            continue
        words = text.split()
        if 1 < len(words) <= 5 and not any(ch.isdigit() for ch in text):
            contact["name"] = text
            break

    return contact


# ─── Dates ──────────────────────────────────────────────────────────────────


def parse_date_range(text: str) -> tuple[date | None, date | None]:
    """Pull a start/end from a line like 'Jun 2023 - Present'.

    Returns (None, None) when nothing parses. Callers must then say nothing
    about duration rather than guess — an invented date becomes an invented
    years-of-experience claim.
    """
    if m := _DATE_RANGE.search(text):
        return _to_date(m.group("start")), _to_date(m.group("end"), is_end=True)
    years = _YEAR.findall(text)
    if len(years) >= 2:
        return date(int(years[0]), 1, 1), date(int(years[1]), 12, 31)
    if len(years) == 1:
        return date(int(years[0]), 1, 1), None
    return None, None


def _to_date(token: str, is_end: bool = False) -> date | None:
    """A bare year resolves to the START of that year, or its END when it closes
    a range: "2021 - 2025" spans five years, not four years and a day. A token
    naming a month is precise enough to use the 1st either way."""
    token = token.strip().lower().replace(".", "")
    if token in {"present", "current", "now"}:
        return None  # open-ended; callers treat a None end as "to today"
    year_match = _YEAR.search(token)
    if not year_match:
        return None
    year = int(year_match.group(0))
    for name, number in _MONTH_NUM.items():
        if token.startswith(name):
            return date(year, number, 1)
    return date(year, 12, 31) if is_end else date(year, 1, 1)


# ─── Fact extraction ────────────────────────────────────────────────────────


def _find_skills(text: str) -> set[str]:
    """Canonical skills mentioned in a line.

    Short or ordinary-word skills ("R", "C", "Go") only count inside a
    delimited list, where a resume actually puts them. Matching them in prose
    turns every sentence containing "go" into an evidenced skill.
    """
    lowered = f" {text.lower()} "
    looks_like_list = lowered.count(",") >= 2 or "|" in lowered or "•" in text
    found: set[str] = set()

    for alias, canonical in ALIAS_TO_CANONICAL.items():
        if alias in AMBIGUOUS and not looks_like_list:
            continue
        pattern = rf"(?<![\w+#]){re.escape(alias)}(?![\w+#])"
        if re.search(pattern, lowered):
            found.add(canonical)
    return found


def extract_facts(parsed: ParsedResume) -> list[Fact]:
    facts: list[Fact] = []
    seen_skills: dict[str, Fact] = {}
    filename = parsed.filename

    def add(kind: FactKind, label: str, line: Line, body: str = "", **kwargs) -> Fact:
        fact = Fact(
            kind=kind,
            label=label.strip()[:300],
            body=body.strip(),
            evidence_source=EvidenceSource.MASTER_RESUME,
            evidence_locator=line.locator(filename),
            verified=False,
            **kwargs,
        )
        facts.append(fact)
        return fact

    # --- Skills, from anywhere in the document -----------------------------
    # A skill named only in a project bullet is still evidenced, and restricting
    # to the skills section loses most of the real signal.
    for section_name, lines in parsed.sections.items():
        if section_name in SKILL_EXCLUDED_SECTIONS:
            continue
        for line in lines:
            for canonical in _find_skills(line.text):
                if canonical in seen_skills:
                    continue
                aliases = [
                    a for a, c in ALIAS_TO_CANONICAL.items() if c == canonical and a != canonical
                ]
                seen_skills[canonical] = add(
                    FactKind.SKILL,
                    canonical,
                    line,
                    body=line.text if section_name != "skills" else "",
                    aliases=aliases,
                )

    # --- Experience and internships ----------------------------------------
    for section_name, kind in (
        ("experience", FactKind.EXPERIENCE),
        ("internships", FactKind.EXPERIENCE),
    ):
        for entry in _group_entries(parsed.sections.get(section_name, [])):
            start, end = parse_date_range(entry.heading.text)
            add(
                kind,
                entry.heading.text,
                entry.heading,
                body="\n".join(b.text for b in entry.bullets),
                first_used_at=start,
                last_used_at=end,
            )

    # --- Projects -----------------------------------------------------------
    for entry in _group_entries(parsed.sections.get("projects", [])):
        start, _ = parse_date_range(entry.heading.text)
        add(
            FactKind.PROJECT,
            entry.heading.text,
            entry.heading,
            body="\n".join(b.text for b in entry.bullets),
            first_used_at=start,
        )

    # --- Education ----------------------------------------------------------
    for entry in _group_entries(parsed.sections.get("education", [])):
        start, end = parse_date_range(entry.heading.text)
        add(
            FactKind.EDUCATION,
            entry.heading.text,
            entry.heading,
            body="\n".join(b.text for b in entry.bullets),
            first_used_at=start,
            last_used_at=end,
        )

    # --- Certifications and achievements ------------------------------------
    for line in parsed.sections.get("certifications", []):
        text = _BULLET.sub("", line.text)
        if len(text) > 3:
            add(FactKind.CERTIFICATION, text, line)

    for line in parsed.sections.get("achievements", []):
        text = _BULLET.sub("", line.text)
        if len(text) > 3:
            add(FactKind.ACHIEVEMENT, text, line)

    # --- Links --------------------------------------------------------------
    for key, value in parsed.contact.items():
        if key in {"linkedin", "github", "portfolio"} and parsed.lines:
            add(FactKind.LINK, f"{key}: {value}", parsed.lines[0])

    return facts


@dataclass
class Entry:
    heading: Line
    bullets: list[Line] = field(default_factory=list)


def _group_entries(lines: list[Line]) -> list[Entry]:
    """Group a section into heading + bullet blocks.

    A bullet-marked line, or a lowercase continuation, belongs to the entry
    above it; anything else starts a new one.
    """
    entries: list[Entry] = []
    for line in lines:
        is_bullet = bool(_BULLET.match(line.text))
        starts_lower = line.text[:1].islower()
        is_indented = bool(entries) and line.indent > entries[-1].heading.indent
        # Right-aligned dates land on their own line once lines are rebuilt from
        # character positions. "Sep 2025 - Oct 2025" is not a new entry.
        is_bare_date = bool(_DATE_ONLY.fullmatch(line.text.strip()))
        if entries and is_bare_date:
            entries[-1].heading = Line(
                text=f"{entries[-1].heading.text} {line.text}".strip(),
                page=entries[-1].heading.page,
                number=entries[-1].heading.number,
                indent=entries[-1].heading.indent,
            )
            continue
        if entries and (is_bullet or starts_lower or is_indented):
            entries[-1].bullets.append(
                Line(
                    text=_BULLET.sub("", line.text),
                    page=line.page,
                    number=line.number,
                    indent=line.indent,
                )
            )
        else:
            entries.append(Entry(heading=line))
    return entries


# ─── Entry point ────────────────────────────────────────────────────────────


def parse_resume(path: str | Path) -> ParsedResume:
    path = Path(path)
    if not path.exists():
        raise FileNotFoundError(f"Resume not found: {path}")

    raw_bytes = path.read_bytes()
    raw_text, lines, pdf_links = extract_text(path)

    parsed = ParsedResume(
        filename=path.name,
        content_hash=hashlib.sha256(raw_bytes).hexdigest(),
        raw_text=raw_text,
        lines=lines,
    )

    if not lines:
        parsed.warnings.append(
            "No text could be extracted. A scanned or image-only PDF needs OCR first."
        )
        return parsed

    parsed.sections = detect_sections(lines)
    parsed.contact = extract_contact(lines, raw_text, pdf_links)
    parsed.facts = extract_facts(parsed)

    known = set(parsed.sections) - {"header"}
    for expected in ("experience", "education", "skills", "projects"):
        if expected not in known:
            parsed.warnings.append(
                f"No '{expected}' section found — its heading may be worded unusually. "
                f"Facts from it can be added by hand."
            )
    if not parsed.contact.get("email"):
        parsed.warnings.append("No email address found; application forms will need it.")

    log.info(
        "resume_parsed",
        file=path.name,
        lines=len(lines),
        sections=sorted(known),
        facts=len(parsed.facts),
    )
    return parsed


def to_candidate(parsed: ParsedResume) -> Candidate:
    """Build a Candidate from the parse. Fields the resume does not state stay
    empty rather than being guessed."""
    contact = parsed.contact
    links = {k: v for k, v in contact.items() if k in {"linkedin", "github", "portfolio"}}
    return Candidate(
        name=contact.get("name") or "Unknown",
        email=contact.get("email"),
        phone=contact.get("phone"),
        links=links,
        facts=parsed.facts,
    )
