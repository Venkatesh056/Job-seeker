"""Rendering a tailored resume to HTML, text and PDF.

Single-column, no tables, no graphics, real text — because the reader that
matters first is a parser, not a person. Every layout decision here is made
against `plain_text()` being recoverable from the PDF.

PDF generation goes through Playwright's Chromium, which the project already
depends on for the browser agent. That avoids WeasyPrint's GTK/Pango native
dependencies, which on Windows are a genuine obstacle rather than an inconvenience.

Rendering is deliberately gated: `render_pdf` refuses a document that has not
passed validation. There is no flag to override it, because a validated document
is the only kind that should ever reach a recruiter.
"""

from __future__ import annotations

import html
import re
from pathlib import Path

from ..logging import get_logger
from .documents import TailoredResume
from .validate import Report

log = get_logger("resume.render")

CSS = """
  @page { size: A4; margin: 14mm 15mm; }
  * { box-sizing: border-box; }
  body {
    font: 10.5pt/1.42 "Calibri", "Carlito", "Helvetica Neue", Arial, sans-serif;
    color: #111; margin: 0;
  }
  h1 { font-size: 19pt; margin: 0 0 2pt; letter-spacing: -.2pt; }
  .contact { font-size: 9pt; color: #333; margin-bottom: 10pt; }
  .contact span + span::before { content: " · "; color: #999; }
  h2 {
    font-size: 9.5pt; text-transform: uppercase; letter-spacing: .8pt;
    margin: 13pt 0 5pt; padding-bottom: 2pt; border-bottom: .8pt solid #222;
  }
  .summary { margin: 0 0 2pt; text-align: left; }
  .skills { margin: 0; }
  .entry { margin-bottom: 8pt; page-break-inside: avoid; }
  .entry-head {
    display: flex; justify-content: space-between; gap: 10pt; align-items: baseline;
  }
  .entry-title { font-weight: 700; }
  .entry-sub { font-style: italic; color: #333; }
  .entry-dates { font-size: 9pt; color: #555; white-space: nowrap; }
  ul { margin: 3pt 0 0; padding-left: 14pt; }
  li { margin-bottom: 2pt; }
"""

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><title>{name} — {title}</title>
<style>{css}</style></head>
<body>
<h1>{name}</h1>
<div class="contact">{contact}</div>
{sections}
</body></html>"""


def _esc(text: str) -> str:
    return html.escape(text or "", quote=False)


def _entries_html(heading: str, entries) -> str:
    if not entries:
        return ""
    blocks = []
    for entry in entries:
        head = f'<span class="entry-title">{_esc(entry.title)}</span>'
        if entry.subtitle:
            head += f' <span class="entry-sub">{_esc(entry.subtitle)}</span>'
        dates = f'<span class="entry-dates">{_esc(entry.dates)}</span>' if entry.dates else ""
        bullets = "".join(f"<li>{_esc(b.text)}</li>" for b in entry.bullets)
        blocks.append(
            f'<div class="entry"><div class="entry-head"><div>{head}</div>{dates}</div>'
            f"{f'<ul>{bullets}</ul>' if bullets else ''}</div>"
        )
    return f"<h2>{_esc(heading)}</h2>{''.join(blocks)}"


def _bullets_html(heading: str, bullets) -> str:
    if not bullets:
        return ""
    items = "".join(f"<li>{_esc(b.text)}</li>" for b in bullets)
    return f"<h2>{_esc(heading)}</h2><ul>{items}</ul>"


def to_html(resume: TailoredResume) -> str:
    """Single-column HTML. What the PDF is printed from."""
    contact = "".join(
        f"<span>{_esc(value)}</span>"
        for key, value in resume.contact.items()
        if (value and key != "location") or (key == "location" and value)
    )

    sections = []
    if resume.summary:
        sections.append(f'<h2>Summary</h2><p class="summary">{_esc(resume.summary)}</p>')
    if resume.skills:
        sections.append(f'<h2>Skills</h2><p class="skills">{_esc(" · ".join(resume.skills))}</p>')
    sections.append(_entries_html("Experience", resume.experience))
    sections.append(_entries_html("Projects", resume.projects))
    sections.append(_entries_html("Education", resume.education))
    sections.append(_bullets_html("Certifications", resume.certifications))
    sections.append(_bullets_html("Achievements", resume.achievements))

    return TEMPLATE.format(
        name=_esc(resume.candidate_name),
        title=_esc(resume.job_title or resume.archetype),
        css=CSS,
        contact=contact,
        sections="\n".join(s for s in sections if s),
    )


def to_text(resume: TailoredResume) -> str:
    return resume.plain_text()


def safe_filename(resume: TailoredResume) -> str:
    """A filename a recruiter will read, and a filesystem will accept."""
    parts = [resume.candidate_name or "resume"]
    if resume.company:
        parts.append(resume.company)
    if resume.job_title:
        parts.append(resume.job_title)
    slug = re.sub(r"[^\w\s-]", "", " ".join(parts)).strip()
    return re.sub(r"[\s_-]+", "_", slug)[:120] or "resume"


class NotValidated(RuntimeError):
    """Raised rather than warned. A document with evidence violations must not be
    written to a file that could then be attached to an application."""


async def render_pdf(
    resume: TailoredResume,
    report: Report,
    output_dir: Path,
    *,
    filename: str | None = None,
) -> Path:
    """Write a PDF, refusing anything the validator did not pass.

    Uses Chromium's own print pipeline, so the PDF carries real selectable text
    and an ATS parser recovers the same content `plain_text()` produces.
    """
    if not report.ok:
        raise NotValidated(
            "resume has evidence violations and will not be rendered: "
            + "; ".join(str(v) for v in report.violations[:3])
        )

    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{filename or safe_filename(resume)}.pdf"

    from playwright.async_api import async_playwright

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content(to_html(resume), wait_until="load")
            await page.pdf(
                path=str(target),
                format="A4",
                print_background=False,
                margin={"top": "14mm", "bottom": "14mm", "left": "15mm", "right": "15mm"},
            )
        finally:
            await browser.close()

    log.info("resume_rendered", path=str(target), archetype=resume.archetype)
    return target


def write_html(resume: TailoredResume, output_dir: Path, filename: str | None = None) -> Path:
    """HTML alongside the PDF — reviewable without a browser launch, and the
    fallback when Chromium is not installed."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{filename or safe_filename(resume)}.html"
    target.write_text(to_html(resume), encoding="utf-8")
    return target


def write_text(resume: TailoredResume, output_dir: Path, filename: str | None = None) -> Path:
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    target = output_dir / f"{filename or safe_filename(resume)}.txt"
    target.write_text(to_text(resume), encoding="utf-8")
    return target
