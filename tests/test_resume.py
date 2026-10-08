"""Resume parsing and Fact Bank extraction.

The safety property under test is the one the whole product rests on: every
extracted fact traces to a line the candidate actually wrote, and nothing is
invented to fill a gap.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from careeragent.models.candidate import FactKind
from careeragent.resume.parser import (
    Line,
    detect_sections,
    extract_contact,
    parse_date_range,
    parse_resume,
    to_candidate,
)

RESUME = """Asha Raman
Bangalore, India | asha.raman@example.com | +91 98765 43210
linkedin.com/in/asharaman | github.com/asharaman

EDUCATION
B.E. Computer Science, PES University, Bangalore | 2021 - 2025
  CGPA 8.7/10. Coursework: Machine Learning, Statistics.

EXPERIENCE
Machine Learning Intern, Nexora Labs | Jun 2024 - Dec 2024
  - Fine-tuned Whisper for Indic speech recognition.
  - Built pipelines in Python and Apache Airflow.

PROJECTS
Resume Ranker | 2024
  - RAG pipeline using LangChain and ChromaDB.

TECHNICAL SKILLS
Languages: Python, SQL, C++
ML/AI: PyTorch, scikit-learn, Transformers, NLP
Cloud: AWS, Docker, Git

CERTIFICATIONS
AWS Certified Cloud Practitioner

ACHIEVEMENTS
Winner, Smart India Hackathon 2024
"""


@pytest.fixture
def resume_file(tmp_path: Path) -> Path:
    path = tmp_path / "sample_resume.txt"
    path.write_text(RESUME, encoding="utf-8")
    return path


def line(text: str, indent: int = 0) -> Line:
    return Line(text=text, page=1, number=1, indent=indent)


# ─── Contact ────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    ["+91 98765 43210", "(555) 123-4567", "+44 7700 900123", "9876543210", "+1-415-555-0132"],
)
def test_phone_formats(raw: str) -> None:
    """Regression: a fixed group pattern silently dropped the last digit of
    '+91 98765 43210'. A wrong phone number on an application is unrecoverable."""
    assert extract_contact([line(raw)], raw)["phone"].strip("() ") in raw


def test_year_range_is_not_a_phone_number() -> None:
    assert "phone" not in extract_contact([line("2021 - 2025")], "2021 - 2025")


def test_extracts_email_and_profiles() -> None:
    contact = extract_contact(
        [line("a.b@example.com linkedin.com/in/abc github.com/abc")],
        "a.b@example.com linkedin.com/in/abc github.com/abc",
    )
    assert contact["email"] == "a.b@example.com"
    assert contact["linkedin"] == "https://linkedin.com/in/abc"
    assert contact["github"] == "https://github.com/abc"


# ─── Sections ───────────────────────────────────────────────────────────────


def test_detect_sections() -> None:
    lines = [line("EDUCATION"), line("B.E. CS"), line("TECHNICAL SKILLS"), line("Python")]
    sections = detect_sections(lines)
    assert [x.text for x in sections["education"]] == ["B.E. CS"]
    assert [x.text for x in sections["skills"]] == ["Python"]


def test_prose_mentioning_a_section_name_is_not_a_heading() -> None:
    """'my experience with Docker' must not start an Experience section."""
    lines = [line("EDUCATION"), line("Built things; experience with Docker and Python")]
    sections = detect_sections(lines)
    assert len(sections["education"]) == 1
    assert "experience" not in sections


# ─── Dates ──────────────────────────────────────────────────────────────────


def test_parse_date_range() -> None:
    assert parse_date_range("Jun 2024 - Dec 2024") == (date(2024, 6, 1), date(2024, 12, 1))
    assert parse_date_range("2021 - 2025") == (date(2021, 1, 1), date(2025, 12, 31))


def test_open_ended_range_has_no_end_date() -> None:
    start, end = parse_date_range("Jan 2023 - Present")
    assert start == date(2023, 1, 1)
    assert end is None


def test_unparseable_dates_return_none_rather_than_guessing() -> None:
    assert parse_date_range("some time ago") == (None, None)


# ─── Fact extraction ────────────────────────────────────────────────────────


def test_parse_resume_end_to_end(resume_file: Path) -> None:
    parsed = parse_resume(resume_file)
    assert parsed.contact["name"] == "Asha Raman"
    assert parsed.contact["email"] == "asha.raman@example.com"
    assert parsed.contact["phone"] == "+91 98765 43210"
    assert not parsed.warnings

    skills = {f.label for f in parsed.facts_of(FactKind.SKILL)}
    assert {"python", "pytorch", "sql", "aws", "docker", "whisper"} <= skills
    assert len(parsed.facts_of(FactKind.EXPERIENCE)) == 1
    assert len(parsed.facts_of(FactKind.PROJECT)) == 1
    assert len(parsed.facts_of(FactKind.CERTIFICATION)) == 1


def test_every_fact_has_resolvable_evidence(resume_file: Path) -> None:
    """The core anti-fabrication invariant: no fact without a source line."""
    parsed = parse_resume(resume_file)
    assert parsed.facts
    for fact in parsed.facts:
        assert fact.evidence_locator.startswith("sample_resume.txt#page=")
        assert "line=" in fact.evidence_locator
        assert fact.evidence_source is not None


def test_facts_are_unverified_until_a_human_confirms(resume_file: Path) -> None:
    """Parsed facts may be used for matching, but must not reach a generated
    resume before review."""
    assert all(not f.verified for f in parse_resume(resume_file).facts)


def test_does_not_invent_skills_absent_from_the_resume(resume_file: Path) -> None:
    skills = {f.label for f in parse_resume(resume_file).facts_of(FactKind.SKILL)}
    for absent in ("kubernetes", "tensorflow", "spark", "tableau", "rust"):
        assert absent not in skills, f"invented '{absent}'"


def test_indented_continuation_stays_with_its_entry(resume_file: Path) -> None:
    """Regression: 'CGPA 8.7/10...' became a second education entry."""
    education = parse_resume(resume_file).facts_of(FactKind.EDUCATION)
    assert len(education) == 1
    assert "CGPA" in education[0].body


def test_ambiguous_short_skills_need_a_list_context() -> None:
    """'C' and 'R' and 'Go' must not match ordinary prose, or the Fact Bank
    fills with nonsense that later becomes a resume claim."""
    from careeragent.resume.parser import _find_skills

    assert "c" not in _find_skills("We go to the office and see results")
    assert "r" not in _find_skills("Go and read the report")
    assert {"c", "r"} <= _find_skills("Languages: C, R, Python, Java")


def test_experience_dates_drive_truthful_duration(resume_file: Path) -> None:
    candidate = to_candidate(parse_resume(resume_file))
    years = candidate.total_years_experience()
    assert years is not None and 0.4 <= years <= 0.6  # Jun–Dec 2024


def test_candidate_supports_only_evidenced_claims(resume_file: Path) -> None:
    candidate = to_candidate(parse_resume(resume_file))
    assert candidate.supports("pytorch") is not None
    assert candidate.supports("kubernetes") is None


def test_unsupported_format_is_rejected_clearly(tmp_path: Path) -> None:
    bad = tmp_path / "resume.rtf"
    bad.write_text("x", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported resume format"):
        parse_resume(bad)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_resume(tmp_path / "nope.pdf")


def test_empty_resume_warns_instead_of_crashing(tmp_path: Path) -> None:
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n\n", encoding="utf-8")
    parsed = parse_resume(empty)
    assert parsed.facts == []
    assert any("OCR" in w for w in parsed.warnings)


def test_missing_sections_are_reported_not_fabricated(tmp_path: Path) -> None:
    thin = tmp_path / "thin.txt"
    thin.write_text("Jane Doe\njane@example.com\n\nSKILLS\nPython, SQL\n", encoding="utf-8")
    parsed = parse_resume(thin)
    assert any("experience" in w for w in parsed.warnings)
    assert parsed.facts_of(FactKind.EXPERIENCE) == []


# ─── Regressions found on a real resume ─────────────────────────────────────


def test_award_names_do_not_become_skills(tmp_path: Path) -> None:
    """'CIT SPARK GRANT 2025' is an award, not Apache Spark. Award and
    competition names collide constantly with product names, so the
    achievements section is excluded from skill extraction."""
    path = tmp_path / "r.txt"
    path.write_text(
        "Jane Doe\njane@example.com\n\n"
        "SKILLS\nPython, SQL\n\n"
        "ACHIEVEMENTS\n"
        "- CIT SPARK GRANT 2025 - Finalist among 25 teams\n"
        "- DJANGO UNCHAINED QUIZ 2024 - Winner\n",
        encoding="utf-8",
    )
    skills = {f.label for f in parse_resume(path).facts_of(FactKind.SKILL)}
    assert "spark" not in skills
    assert "django" not in skills
    assert {"python", "sql"} <= skills


def test_shell_companies_do_not_become_a_shell_skill(tmp_path: Path) -> None:
    """'identify shell entities' is fraud-detection domain language, not Bash."""
    path = tmp_path / "r.txt"
    path.write_text(
        "Jane Doe\njane@example.com\n\nPROJECTS\n"
        "Fraud Detection | 2024\n"
        "  - Analysed GST records to identify shell entities and circular trading.\n",
        encoding="utf-8",
    )
    assert "bash" not in {f.label for f in parse_resume(path).facts_of(FactKind.SKILL)}


def test_compound_all_caps_headings_are_recognised(tmp_path: Path) -> None:
    """Real headings are rarely one tidy word: 'INDUSTRY EXPERIENCE / FREELANCE
    PROJECT' and 'ACHIEVEMENTS & CERTIFICATIONS' both appear on real resumes."""
    from careeragent.resume.parser import section_for_heading

    assert section_for_heading("INDUSTRY EXPERIENCE / FREELANCE PROJECT") == "experience"
    assert section_for_heading("ACHIEVEMENTS & CERTIFICATIONS") == "achievements"
    assert section_for_heading("CO-CURRICULAR ACTIVITIES") == "achievements"
    assert section_for_heading("TECHNICAL SKILLS") == "skills"


def test_lowercase_prose_is_never_a_compound_heading() -> None:
    """The guard that keeps the compound matcher from eating body text."""
    from careeragent.resume.parser import section_for_heading

    assert section_for_heading("Built things; experience with Docker and Python") is None
    assert section_for_heading("my projects include a chatbot") is None
