"""Writing to one person, for one job.

The property worth defending is that the message could not have been sent to anyone else.
A template is what recruiters delete, so the tests here are mostly about the three things
that are allowed to change it — who is reading, which channel, and what is actually true —
and about the claims that must never appear whatever the model produces.
"""

from __future__ import annotations

import pytest

from careeragent.models.candidate import Candidate, EvidenceSource, Fact, FactKind
from careeragent.models.person import Person
from careeragent.outreach.compose import (
    Audience,
    Channel,
    audience_for,
    check,
    signature,
    subject_candidates,
    tidy,
)


def _person(title: str, name: str = "Priya Sharma") -> Person:
    return Person(name=name, title=title, company="Acme")


# ─── Who is reading ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "size", "expected"),
    [
        ("Technical Recruiter", 500, Audience.RECRUITER),
        ("Talent Acquisition Manager", 500, Audience.RECRUITER),
        ("AI Engineering Manager", 500, Audience.ENGINEERING_LEADER),
        ("Head of Data", 500, Audience.ENGINEERING_LEADER),
        ("Data Scientist", 500, Audience.PEER),
        ("Chief Executive Officer", 3000, Audience.EXECUTIVE),
        ("Chief Financial Officer", 500, Audience.EXECUTIVE),
    ],
)
def test_a_title_decides_what_the_message_leads_with(title, size, expected):
    assert audience_for(_person(title), company_size=size) is expected


def test_a_founder_at_a_tiny_company_is_read_as_the_engineering_lead():
    """At twelve people the founder *is* the engineering lead, and a message pitched at a
    distant executive would be the wrong register entirely."""
    assert audience_for(_person("Founder"), company_size=12) is Audience.ENGINEERING_LEADER
    assert audience_for(_person("Founder"), company_size=2000) is Audience.EXECUTIVE


def test_every_audience_says_what_to_lead_with_and_how_to_open():
    for audience in Audience:
        assert audience.emphasis()
        assert audience.opening_rule()


def test_the_audiences_do_not_share_their_emphasis():
    """If two audiences ask for the same thing, the distinction is decoration."""
    emphases = {audience.emphasis() for audience in Audience}
    assert len(emphases) == len(Audience)


# ─── Which channel ──────────────────────────────────────────────────────────


def test_email_and_linkedin_are_not_the_same_medium():
    assert Channel.EMAIL.word_limit > Channel.LINKEDIN.word_limit
    assert Channel.EMAIL.carries_attachment
    assert not Channel.LINKEDIN.carries_attachment


def test_a_linkedin_message_has_no_subject_line():
    """Offering one invites the user to write something that is silently discarded."""
    assert Channel.EMAIL.has_subject
    assert not Channel.LINKEDIN.has_subject


# ─── What must never be written ─────────────────────────────────────────────


@pytest.mark.parametrize(
    "opening",
    [
        "I hope this finds you well, and I wanted to reach out about the role.",
        "I came across your company while researching AI teams.",
        "My name is Venkatesh and I am a data scientist.",
        "To whom it may concern, I am writing about the opening.",
    ],
)
def test_a_template_opening_is_flagged(opening):
    assert any("template" in w for w in check(opening, Channel.EMAIL))


@pytest.mark.parametrize(
    "claim",
    [
        "As we discussed at the conference, I would like to apply.",
        "Our mutual connection suggested I reach out.",
        "I was referred to you by a colleague.",
        "I have been following your work for a while.",
        "Your recent post about scaling inference was excellent.",
    ],
)
def test_a_relationship_that_does_not_exist_is_flagged(claim):
    """Each of these is a lie, and each is what a model reaches for when it has nothing
    specific to say."""
    warnings = check(claim, Channel.EMAIL)
    assert any("relationship that does not exist" in w for w in warnings)
    assert any("removed, not softened" in w for w in warnings)


def test_a_linkedin_message_may_not_mention_an_attachment():
    warnings = check("My resume is attached for your review.", Channel.LINKEDIN)
    assert any("carry none" in w for w in warnings)


def test_the_same_sentence_is_fine_in_an_email():
    assert check("My resume is attached for your review.", Channel.EMAIL) == []


def test_a_message_over_the_channel_limit_is_flagged():
    long_message = "word " * 200
    assert any("under 80" in w for w in check(long_message, Channel.LINKEDIN))
    assert any("under 150" in w for w in check(long_message, Channel.EMAIL))


def test_an_empty_message_is_not_silently_acceptable():
    assert check("", Channel.EMAIL) == ["the message is empty"]


def test_a_specific_message_passes_every_check():
    body = (
        "Priya, I built a multimodal biometric system in Python and TensorFlow that "
        "combines face, voice and gesture recognition. Acme's work on data tooling is "
        "the kind of problem I want to work on next. Is the ML Engineer role still open?"
    )
    assert check(body, Channel.EMAIL) == []


def test_a_hand_edited_message_is_checked_by_the_same_rules():
    """A user's edit is not more trustworthy than a model's draft because a human made it."""
    edited = "I hope this finds you well. Here is my CV."
    assert check(edited, Channel.EMAIL)


# ─── Subject lines ──────────────────────────────────────────────────────────


def test_several_subjects_are_offered_not_one():
    """A subject line is read by everyone who receives it and rewritten by nobody, so
    offering one is offering too few."""
    options = subject_candidates(
        candidate_name="Venkatesh D",
        role="ML Engineer",
        company="Acme",
        audience=Audience.RECRUITER,
    )
    assert len(options) >= 2
    assert len(set(options)) == len(options)


def test_a_recruiter_subject_names_the_role_first():
    options = subject_candidates(
        candidate_name="Venkatesh D",
        role="ML Engineer",
        company="Acme",
        audience=Audience.RECRUITER,
    )
    assert options[0].startswith("Application — ML Engineer")


def test_a_subject_fits_in_an_inbox():
    options = subject_candidates(
        candidate_name="A Candidate With A Very Long Name Indeed",
        role="Senior Staff Machine Learning Infrastructure Engineer",
        company="A Company With An Extremely Long Name Limited",
        audience=Audience.RECRUITER,
    )
    assert all(len(option) <= 78 for option in options)


def test_a_subject_is_produced_even_with_nothing_to_go_on():
    assert subject_candidates(
        candidate_name="Venkatesh D", role="", company="", audience=Audience.PEER
    )


# ─── The signature ──────────────────────────────────────────────────────────


def _candidate(**kwargs) -> Candidate:
    facts = kwargs.pop("facts", [])
    return Candidate(name="Venkatesh D", email="v@example.com", facts=facts, **kwargs)


def test_a_signature_contains_only_details_the_candidate_has():
    """A signature with an empty line where a phone number should be is worse than a
    shorter one, and a fabricated one is worse than either."""
    signed = signature(_candidate())

    assert "Venkatesh D" in signed
    assert "v@example.com" in signed
    assert "None" not in signed
    assert not any(line.strip() == "" for line in signed.split("\n"))


def test_links_from_the_fact_bank_reach_the_signature():
    link = Fact(
        kind=FactKind.LINK,
        label="https://github.com/venkatesh",
        evidence_source=EvidenceSource.MASTER_RESUME,
        evidence_locator="cv.pdf#page=1",
    )
    assert "github.com/venkatesh" in signature(_candidate(facts=[link]))


# ─── Formatting ─────────────────────────────────────────────────────────────


def test_spacing_is_normalised_without_touching_the_words():
    messy = "Priya,   I  built   a  system.\n\n\n\nIt worked well.   "
    tidied = tidy(messy)

    assert "  " not in tidied
    assert "\n\n\n" not in tidied
    assert tidied.startswith("Priya, I built a system.")
    assert "It worked well." in tidied


def test_tidying_nothing_yields_nothing():
    assert tidy("") == ""
    assert tidy("   \n\n  ") == ""
