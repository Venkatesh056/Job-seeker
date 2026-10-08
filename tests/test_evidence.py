"""Known, unknown, and not applicable.

The same bug was found three times in this project, each time in different clothes: a
measurement that was never taken got written down as a measurement of zero, and nothing
downstream could tell the difference.

    company fit     an unreadable website scored 0.0, not "not assessed" — Swiggy sat at
                    0.00 until Apollo filled the gap, then at 0.87
    tech overlap    42 of 63 companies overlapped on nothing, because a marketing page
                    says "we deliver value", not "we use pandas"
    job description Apollo's 211 postings carry no body text, so all matched zero skills
                    and all scored reject, including a one-day-old Bangalore internship

These tests encode the rule that stops the fourth occurrence: an unknown component
surrenders its weight; it never contributes a zero.
"""

from __future__ import annotations

import pytest

from careeragent.models.evidence import (
    Known,
    combine,
    measured,
    not_applicable,
    unknown,
)

WEIGHTS = {
    "tech": 0.35,
    "ai": 0.18,
    "culture": 0.17,
    "team": 0.20,
    "location": 0.05,
    "industry": 0.05,
}


# ─── The rule ───────────────────────────────────────────────────────────────


def test_an_unknown_component_surrenders_its_weight_rather_than_scoring_zero():
    """The whole point. Two strong signals and three absences must not average down."""
    with_absences = combine(
        [
            measured("team", 1.0),
            measured("location", 1.0),
            unknown("tech", "their site rendered nothing readable"),
            unknown("ai", "no readable text"),
            unknown("culture", "no readable text"),
        ],
        WEIGHTS,
    )
    as_zeros = combine(
        [
            measured("team", 1.0),
            measured("location", 1.0),
            measured("tech", 0.0),
            measured("ai", 0.0),
            measured("culture", 0.0),
        ],
        WEIGHTS,
    )

    assert with_absences.score == 1.0
    assert as_zeros.score < 0.4
    assert with_absences.score > as_zeros.score


def test_a_measured_zero_is_not_the_same_as_an_absence():
    """ "We looked, and it is bad" must remain distinguishable from "we did not look"."""
    looked = combine([measured("tech", 0.0), measured("team", 1.0)], WEIGHTS)
    did_not = combine([unknown("tech", "no data"), measured("team", 1.0)], WEIGHTS)

    assert looked.score < did_not.score
    assert looked.unknown_fields == []
    assert did_not.unknown_fields == ["tech"]


def test_an_unknown_must_say_why():
    """An unexplained gap is as opaque to a user as a zero was."""
    observation = unknown("tech", "their site rendered nothing readable")
    assert observation.reason
    assert observation.state is Known.UNKNOWN
    assert observation.value is None


def test_an_unknown_never_carries_a_value():
    """So that nothing downstream can read one by mistake."""
    assert unknown("x", "why").value is None
    assert not_applicable("x", "why").value is None
    assert not unknown("x", "why").counts
    assert not not_applicable("x", "why").counts


# ─── Confidence and coverage ────────────────────────────────────────────────


def test_a_thin_score_reports_low_confidence():
    """Swiggy's real shape: one strong signal, most of the evidence missing."""
    thin = combine(
        [
            measured("team", 1.0),
            unknown("tech", "unreadable"),
            unknown("ai", "unreadable"),
            unknown("culture", "unreadable"),
        ],
        WEIGHTS,
    )

    assert thin.score == 1.0, "the measurement itself is still a 1.0"
    assert thin.confidence < 0.3, "but almost nothing backs it"
    assert thin.provisional


def test_a_well_evidenced_score_is_confident():
    thick = combine(
        [
            measured("tech", 0.2),
            measured("ai", 0.6),
            measured("culture", 0.5),
            measured("team", 1.0),
            measured("location", 0.35),
            measured("industry", 0.4),
        ],
        WEIGHTS,
    )

    assert thick.evidence_coverage == 1.0
    assert thick.confidence == 1.0
    assert not thick.provisional


def test_two_scores_of_equal_value_are_distinguishable_by_confidence():
    """The failure this prevents: a 0.9 from one component looking like a 0.9 from six."""
    sparse = combine([measured("team", 0.9), unknown("tech", "no data")], WEIGHTS)
    full = combine(
        [
            measured("tech", 0.9),
            measured("ai", 0.9),
            measured("culture", 0.9),
            measured("team", 0.9),
            measured("location", 0.9),
            measured("industry", 0.9),
        ],
        WEIGHTS,
    )

    assert sparse.score == pytest.approx(full.score, abs=0.01)
    assert sparse.confidence < full.confidence
    assert sparse.provisional and not full.provisional


def test_missing_a_heavy_component_costs_more_confidence_than_a_light_one():
    """Missing the 35% field should dent confidence more than missing the 5% one."""
    lost_heavy = combine(
        [measured("team", 0.8), measured("location", 0.8), unknown("tech", "no data")],
        WEIGHTS,
    )
    lost_light = combine(
        [measured("team", 0.8), measured("tech", 0.8), unknown("location", "no data")],
        WEIGHTS,
    )

    assert lost_heavy.confidence < lost_light.confidence


# ─── Not applicable ─────────────────────────────────────────────────────────


def test_not_applicable_does_not_depress_coverage():
    """A question that does not apply is not evidence we are missing."""
    inapplicable = combine(
        [measured("team", 0.8), not_applicable("industry", "no industry preference set")],
        WEIGHTS,
    )
    absent = combine([measured("team", 0.8), unknown("industry", "could not determine")], WEIGHTS)

    assert inapplicable.evidence_coverage > absent.evidence_coverage
    assert inapplicable.evidence_coverage == 1.0


def test_the_two_absences_are_reported_separately():
    """ "We could not find their tech stack" and "this role has no tech stack" deserve
    different sentences in front of a user."""
    result = combine(
        [
            measured("team", 0.8),
            unknown("tech", "site unreadable"),
            not_applicable("industry", "no preference set"),
        ],
        WEIGHTS,
    )

    assert result.unknown_fields == ["tech"]
    assert result.not_applicable_fields == ["industry"]


# ─── Nothing at all ─────────────────────────────────────────────────────────


def test_nothing_measurable_is_zero_confidence_not_a_bad_score():
    """A caller that reads confidence sees "not assessed" rather than "assessed badly"."""
    nothing = combine([unknown("tech", "no data"), unknown("team", "no data")], WEIGHTS)

    assert nothing.score == 0.0
    assert nothing.confidence == 0.0
    assert nothing.evidence_coverage == 0.0
    assert nothing.provisional


def test_an_empty_observation_list_does_not_divide_by_zero():
    result = combine([], WEIGHTS)
    assert result.score == 0.0
    assert result.confidence == 0.0


def test_everything_not_applicable_is_still_zero_confidence():
    result = combine([not_applicable("tech", "n/a"), not_applicable("team", "n/a")], WEIGHTS)
    assert result.confidence == 0.0
    assert result.unknown_fields == []


# ─── Arithmetic ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("value", "expected"), [(1.5, 1.0), (-0.5, 0.0), (0.5, 0.5), (0, 0.0), (1, 1.0)]
)
def test_a_measurement_is_clamped_to_a_fraction(value, expected):
    assert measured("x", value).value == expected


def test_a_component_with_no_declared_weight_contributes_nothing():
    """A typo in a component name must not silently become a free pass."""
    result = combine([measured("team", 1.0), measured("typo", 1.0)], WEIGHTS)
    assert result.score == 1.0


def test_the_serialised_form_carries_the_provenance():
    result = combine(
        [measured("team", 0.9), unknown("tech", "site unreadable")],
        WEIGHTS,
        reasons=["920 people work in engineering and data roles there"],
    )
    payload = result.to_dict()

    for key in ("score", "confidence", "evidence_coverage", "unknown_fields", "provisional"):
        assert key in payload
    assert payload["unknown_fields"] == ["tech"]
    assert payload["reasons"]
    assert payload["observations"][1]["reason"] == "site unreadable"
