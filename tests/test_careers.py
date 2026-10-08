"""Generalized career taxonomy and field detection.

The property that matters: this has to work for a resume in a field nobody anticipated.
The system began with eight AI/ML archetypes, and a civil engineer's resume scored
against "PyTorch, Transformers, RAG" was rejected by its own relevance gate. These tests
pin the generalization so it cannot quietly regress back to software-only.
"""

from __future__ import annotations

from datetime import date

import pytest

from careeragent.careers import detect
from careeragent.careers import families as fam
from careeragent.careers.detect import MIN_CONFIDENT_SCORE, archetype_for
from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind


def skill(name: str) -> Fact:
    return Fact(
        kind=FactKind.SKILL,
        label=name,
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#1",
    )


def experience(label: str, body: str = "") -> Fact:
    return Fact(
        kind=FactKind.EXPERIENCE,
        label=label,
        body=body,
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#2",
        first_used_at=date(2022, 1, 1),
    )


def candidate_with(*facts: Fact) -> Candidate:
    return Candidate(name="Test", facts=list(facts))


# ─── Taxonomy integrity ─────────────────────────────────────────────────────


def test_families_span_more_than_software():
    """The whole point of the module. Software-only is the bug it fixes."""
    groups = {f.group for f in fam.FAMILIES}
    assert {"Technology", "Engineering", "Business", "Healthcare"} <= groups
    assert len(fam.FAMILIES) >= 40


def test_family_ids_are_unique():
    ids = [f.id for f in fam.FAMILIES]
    assert len(ids) == len(set(ids))


def test_every_related_id_exists():
    """A dangling `related` id would silently drop a family from expansion."""
    for family in fam.FAMILIES:
        for related in family.related:
            assert fam.get(related) is not None, f"{family.id} → {related}"


def test_every_family_has_queries_and_markers():
    for family in fam.FAMILIES:
        if family.id == "custom":
            continue
        assert family.queries, family.id
        assert family.title_markers, family.id


def test_every_group_is_declared():
    for family in fam.FAMILIES:
        assert family.group in fam.GROUPS, f"{family.id} has group {family.group!r}"


def test_archetypes_referenced_actually_exist():
    from careeragent.resume import archetypes as arch

    for family in fam.FAMILIES:
        assert arch.get(family.archetype).id == family.archetype, family.id


# ─── Title matching ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Machine Learning Engineer", "machine_learning"),
        ("Senior Data Analyst", "data_analytics"),
        ("Civil Engineer - Site", "civil_engineering"),
        ("Quality Control Analyst", "quality_engineering"),
        ("Staff Nurse", "healthcare_clinical"),
        ("Chartered Accountant", "finance_accounting"),
        ("UX Designer", "ux_design"),
        ("VLSI Physical Design Engineer", "semiconductor"),
        ("Digital Marketing Manager", "marketing"),
        ("Supply Chain Analyst", "supply_chain"),
    ],
)
def test_for_title(title, expected):
    family = fam.for_title(title)
    assert family is not None and family.id == expected


def test_longest_marker_wins_across_families():
    """ "machine learning engineer" must beat the bare "engineer"-shaped markers."""
    assert fam.for_title("Machine Learning Engineer").id == "machine_learning"
    assert fam.for_title("Embedded Software Engineer").id == "embedded"


def test_unknown_title_returns_none():
    assert fam.for_title("Chief Vibes Officer") is None


# ─── Expansion ──────────────────────────────────────────────────────────────


def test_expansion_adds_related_families():
    """The "and then more related jobs" behaviour the product asks for."""
    expanded = [f.id for f in fam.expand(["data_science"])]
    assert "data_science" in expanded
    assert "data_analytics" in expanded
    assert "machine_learning" in expanded


def test_expansion_keeps_explicit_choices_first():
    """A result cap must bite on the related families, not on what the user picked."""
    expanded = [f.id for f in fam.expand(["data_analytics", "nlp"])]
    assert expanded[:2] == ["data_analytics", "nlp"]


def test_expansion_can_be_turned_off():
    assert [f.id for f in fam.expand(["nlp"], include_related=False)] == ["nlp"]


def test_expansion_does_not_cross_sectors():
    """Picking Data Science must not drag in Civil Engineering."""
    expanded = {f.id for f in fam.expand(["data_science"])}
    assert "civil_engineering" not in expanded
    assert "healthcare_clinical" not in expanded


def test_expansion_deduplicates():
    expanded = [f.id for f in fam.expand(["data_science", "data_analytics"])]
    assert len(expanded) == len(set(expanded))


def test_search_queries_are_capped_and_deduplicated():
    """Every query is a call against a metered free tier."""
    queries = fam.search_queries(fam.expand(["data_science"]), limit_per_family=2)
    assert len(queries) == len({q.lower() for q in queries})
    assert len(queries) <= 2 * len(fam.expand(["data_science"]))


# ─── Detection across fields ────────────────────────────────────────────────


def test_detects_machine_learning():
    detection = detect(
        candidate_with(
            skill("pytorch"), skill("tensorflow"), skill("scikit-learn"), skill("python")
        )
    )
    assert detection.confident
    assert detection.primary.id in {"machine_learning", "ai_engineering"}


def test_detects_civil_engineering():
    """The resume that the AI/ML-only system got wrong."""
    detection = detect(
        candidate_with(
            skill("autocad"),
            skill("staad pro"),
            skill("revit"),
            skill("surveying"),
            skill("structural analysis"),
            experience("Site Engineer, L&T Construction"),
        )
    )
    assert detection.confident
    assert detection.primary.id == "civil_engineering"
    assert detection.group == "Engineering"


def test_detects_pharma():
    detection = detect(
        candidate_with(
            skill("hplc"),
            skill("gmp"),
            skill("regulatory affairs"),
            skill("method validation"),
            skill("pharmacovigilance"),
        )
    )
    assert detection.confident
    assert detection.primary.id == "pharma"


def test_detects_finance():
    detection = detect(
        candidate_with(
            skill("tally"),
            skill("gaap"),
            skill("auditing"),
            skill("bank reconciliation"),
            experience("Accounts Executive, Deloitte"),
        )
    )
    assert detection.confident
    assert detection.primary.id in {"finance_accounting", "investment_banking"}


def test_detects_design():
    detection = detect(
        candidate_with(
            skill("figma"),
            skill("wireframing"),
            skill("prototyping"),
            skill("user research"),
            experience("Product Designer, Swiggy"),
        )
    )
    assert detection.confident
    assert detection.primary.id == "ux_design"


def test_an_empty_resume_is_not_confident():
    """The honest answer when there is nothing to go on."""
    detection = detect(Candidate(name="Nobody"))
    assert not detection.confident
    assert detection.primary is None
    assert "Pick the job families yourself" in detection.to_dict()["note"]


def test_detection_explains_itself():
    """A wrong answer has to be *visibly* wrong, not merely disagreed with."""
    detection = detect(candidate_with(skill("autocad"), skill("staad pro")))
    assert detection.matches
    assert detection.matches[0].reasons
    assert "autocad" in detection.matches[0].reasons[0]


def test_evidence_outweighs_a_generic_title():
    """A "Software Engineer" whose every skill is AutoCAD is a civil engineer."""
    detection = detect(
        candidate_with(
            skill("autocad"),
            skill("staad pro"),
            skill("revit"),
            skill("concrete technology"),
            skill("estimation"),
            skill("surveying"),
            experience("Software Engineer, Generic Corp"),
        )
    )
    assert detection.primary.id == "civil_engineering"


def test_confidence_threshold_is_meaningful():
    """One weak signal must not read as a confident detection."""
    detection = detect(candidate_with(skill("excel")))
    assert not detection.confident or detection.matches[0].score >= MIN_CONFIDENT_SCORE


# ─── Archetype selection ────────────────────────────────────────────────────


def test_archetype_from_families():
    assert archetype_for(["nlp"]) == "nlp_engineer"
    assert archetype_for(["data_analytics"]) == "data_analyst"


def test_non_software_field_falls_back_to_custom():
    """Inventing an emphasis for a field with no archetype is worse than applying none."""
    assert archetype_for(["civil_engineering"]) == "custom"
    assert archetype_for(["pharma", "healthcare_clinical"]) == "custom"


def test_archetype_skips_custom_families_to_find_a_real_one():
    assert archetype_for(["civil_engineering", "data_analytics"]) == "data_analyst"


def test_every_family_skill_is_a_canonical_taxonomy_name():
    """The structural version of a bug that cost pharma detection entirely.

    A family listing "regulatory" when the taxonomy canonicalises to "regulatory
    affairs" can never match anything — silently, for every resume in that field.
    The parser only ever produces canonical labels, so a family skill that is not one
    is dead weight that looks like coverage.
    """
    from careeragent.resume.taxonomy import ALIAS_TO_CANONICAL

    canonical = set(ALIAS_TO_CANONICAL.values())
    unknown: list[str] = []
    for family in fam.FAMILIES:
        for name in family.skills:
            if name not in canonical:
                unknown.append(f"{family.id}: {name!r}")
    assert not unknown, "family skills that no resume can ever match:\n" + "\n".join(unknown)
