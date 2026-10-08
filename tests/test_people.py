"""Who at a company is worth writing to, and what may be said about how to reach them.

Two properties matter more than everything else here.

**A contact detail cannot exist without a source.** Given "Rahul Kumar" at `acme.com`,
`rahul.kumar@acme.com` is a plausible guess, and a plausible guess sitting beside real data
is indistinguishable from it. The tests below assert that constructing a detail without
provenance *raises*, because a rule enforced by a type cannot be forgotten the way a rule
written in a docstring can.

**A CEO is not automatically the best contact.** The tests encode the case that makes this
concrete: for an entry-level ML opening at a 3,000-person company the engineering manager
outranks the CEO by a wide margin, and at a 12-person startup the same CEO is the hiring
manager and should rank well.
"""

from __future__ import annotations

import pytest

from careeragent.models.person import (
    ContactDetail,
    EmailKind,
    Person,
    SourceKind,
    Verification,
    classify_email,
    is_person_profile,
    merge,
    normalize_name,
    same_person,
)
from careeragent.people import (
    career_family,
    extract_people,
    function_of,
    has_hiring_authority,
    looks_like_a_name,
    looks_like_a_title,
    rank,
    score_person,
    seniority_of,
)
from careeragent.people.discover import (
    _blocks,
    _names_another_employer,
    _same_page,
    people_links,
)

TEAM_PAGE = "https://acme.com/team"


def detail(value: str, *, verified: bool = True, kind: SourceKind = SourceKind.TEAM_PAGE):
    return ContactDetail(
        value=value,
        kind=kind,
        source_url=TEAM_PAGE,
        verification=Verification.VERIFIED if verified else Verification.UNVERIFIED,
        confidence=0.9 if verified else 0.4,
    )


def person(name="R Kumar", title="", *, email=None, linkedin=None, verified=True, **kwargs):
    return Person(
        name=name,
        title=title,
        company=kwargs.pop("company", "Acme"),
        email=detail(email, verified=verified) if email else None,
        linkedin=detail(linkedin, verified=verified) if linkedin else None,
        **kwargs,
    )


# ─── A detail cannot exist without a source ─────────────────────────────────


def test_a_contact_detail_without_a_source_cannot_be_constructed():
    """The rule made structural. There is nowhere in this codebase to put an email
    address that nobody published."""
    with pytest.raises(ValueError, match="needs the URL"):
        ContactDetail(value="guess@acme.com", kind=SourceKind.TEAM_PAGE, source_url="")


def test_a_user_entered_detail_needs_no_url():
    """The one exception: the user typed it, so they are the source."""
    typed = ContactDetail(value="someone@acme.com", kind=SourceKind.USER_ENTERED, source_url="")
    assert typed.value == "someone@acme.com"


def test_an_empty_detail_is_refused():
    with pytest.raises(ValueError, match="needs a value"):
        ContactDetail(value="  ", kind=SourceKind.USER_ENTERED, source_url="")


def test_only_a_verified_detail_is_safe_to_contact():
    assert detail("a@acme.com", verified=True).safe_to_contact
    assert not detail("a@acme.com", verified=False).safe_to_contact

    inferred = ContactDetail(
        value="a@acme.com",
        kind=SourceKind.ENRICHMENT_PROVIDER,
        source_url="https://provider.example",
        verification=Verification.INFERRED,
    )
    assert not inferred.safe_to_contact, "an inferred address is never sent to automatically"


def test_a_company_published_source_outranks_a_provider_on_the_same_fact():
    """The company is the authority on who works there."""
    first_party = person(email="a@acme.com")
    provider = Person(
        name="R Kumar",
        company="Acme",
        email=ContactDetail(
            value="other@acme.com",
            kind=SourceKind.ENRICHMENT_PROVIDER,
            source_url="https://provider.example",
            verification=Verification.VERIFIED,
            confidence=0.99,
        ),
    )

    merged = merge(first_party, provider)
    assert merged.email is not None
    assert merged.email.value == "a@acme.com"


# ─── Whose inbox is it ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "address",
    ["careers@acme.com", "hr@acme.com", "jobs@acme.com", "talent@acme.com", "info@acme.com"],
)
def test_a_department_address_is_recognised_as_one(address):
    assert classify_email(address) is EmailKind.GENERIC_COMPANY


@pytest.mark.parametrize("address", ["careers-india@acme.com", "hr.team@acme.com"])
def test_a_decorated_department_address_is_still_a_department(address):
    assert classify_email(address) is EmailKind.GENERIC_COMPANY


@pytest.mark.parametrize("address", ["rahul.kumar@acme.com", "r.iyer@acme.com"])
def test_a_named_address_is_a_person(address):
    assert classify_email(address) is EmailKind.PERSONAL_WORK


def test_something_that_is_not_an_address_is_unknown():
    assert classify_email("not an email") is EmailKind.UNKNOWN
    assert classify_email("") is EmailKind.UNKNOWN


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://linkedin.com/in/rahul-kumar", True),
        ("https://www.linkedin.com/in/rahul-kumar/", True),
        ("https://in.linkedin.com/in/rahul", True),
        ("https://linkedin.com/company/acme", False),
        ("https://linkedin.com/posts/something", False),
        ("https://acme.com/team", False),
        ("", False),
    ],
)
def test_only_a_personal_linkedin_url_counts_as_a_profile(url, expected):
    assert is_person_profile(url) is expected


# ─── Reading a title ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("AI Engineering Manager", "ai_ml"),
        ("Head of Machine Learning", "ai_ml"),
        ("Senior NLP Engineer", "ai_ml"),
        ("Head of Data", "data"),
        ("Data Science Lead", "data"),
        ("VP Engineering", "engineering"),
        ("Staff Software Engineer", "engineering"),
        ("Technical Recruiter", "recruiting"),
        ("Talent Acquisition Manager", "recruiting"),
        ("Finance Manager", "finance"),
        ("Chief Executive Officer", "executive"),
        ("Product Manager", "product"),
        ("", ""),
    ],
)
def test_a_title_is_read_as_a_function(title, expected):
    assert function_of(title) == expected


def test_a_specific_function_wins_over_a_general_one():
    """ "Head of AI Engineering" is AI work, not generic engineering."""
    assert function_of("Head of AI Engineering") == "ai_ml"
    assert function_of("VP of Data Science") == "data"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Chief Executive Officer", 5),
        ("Founder", 5),
        ("VP Engineering", 4),
        ("Head of Data", 4),
        ("Engineering Director", 3),
        ("Engineering Manager", 2),
        ("Senior Data Scientist", 1),
        ("Data Scientist", 0),
    ],
)
def test_a_title_is_read_as_a_level(title, expected):
    assert seniority_of(title) == expected


@pytest.mark.parametrize(
    "title", ["Technical Recruiter", "Engineering Manager", "Head of AI", "CTO", "Founder"]
)
def test_titles_that_can_start_a_hiring_conversation(title):
    assert has_hiring_authority(title)


@pytest.mark.parametrize("title", ["Data Scientist", "Software Engineer", "Analyst"])
def test_titles_that_cannot(title):
    assert not has_hiring_authority(title)


def test_the_career_family_drives_who_is_prioritised():
    assert career_family("Machine Learning Engineer") == "ai_ml"
    assert career_family("Data Analyst") == "data"
    assert career_family("Civil Site Engineer") == "engineering"


# ─── Who is actually worth writing to ───────────────────────────────────────


def _ml_rank(people, size=3000, junior=True):
    return rank(
        people,
        target_role="Machine Learning Engineer",
        job_title="ML Engineer",
        company_size=size,
        role_is_junior=junior,
    )


def test_the_engineering_manager_beats_the_ceo_at_a_large_company():
    """The case the whole module exists for. A CEO of 3,000 people will not read this and
    could not act on it; the manager who owns the team has the problem the candidate
    solves."""
    results = _ml_rank(
        [
            person("A", "Chief Executive Officer", email="a@acme.com"),
            person("B", "AI Engineering Manager", email="b@acme.com"),
        ]
    )

    assert results[0].person.title == "AI Engineering Manager"
    assert results[0].out_of_100 > 90
    assert results[1].out_of_100 < 30


def test_the_founder_of_a_tiny_company_is_the_hiring_manager():
    """At twelve people there is no separate hiring function."""
    big = _ml_rank([person("A", "Chief Executive Officer", email="a@acme.com")], size=3000)
    small = _ml_rank([person("A", "Chief Executive Officer", email="a@acme.com")], size=12)

    assert small[0].out_of_100 > big[0].out_of_100 * 2


def test_an_unrelated_department_scores_near_zero_however_senior():
    """A finance manager has all the seniority of an engineering manager and none of the
    ability to hire an ML engineer. Before this gate existed they scored 45/100."""
    results = _ml_rank(
        [
            person("B", "AI Engineering Manager", email="b@acme.com"),
            person("D", "Finance Manager", email="d@acme.com"),
            person("G", "Marketing Director", email="g@acme.com"),
        ]
    )

    by_title = {r.person.title: r.out_of_100 for r in results}
    assert by_title["Finance Manager"] < 15
    assert by_title["Marketing Director"] < 15
    assert by_title["AI Engineering Manager"] > 90


def test_a_recruiter_ranks_well_because_receiving_this_is_their_job():
    results = _ml_rank(
        [
            person("C", "Technical Recruiter", email="c@acme.com"),
            person("D", "Finance Manager", email="d@acme.com"),
        ]
    )
    assert results[0].person.title == "Technical Recruiter"
    assert results[0].out_of_100 > 50


def test_the_priority_order_follows_the_candidates_career():
    """An ML candidate wants the AI lead; a sales candidate wants the sales lead. The same
    people, ranked differently."""
    people = [
        person("B", "AI Engineering Manager", email="b@acme.com"),
        person("S", "Sales Director", email="s@acme.com"),
    ]

    for_ml = rank(people, target_role="Machine Learning Engineer", company_size=500)
    for_sales = rank(people, target_role="Account Executive", company_size=500)

    assert for_ml[0].person.title == "AI Engineering Manager"
    assert for_sales[0].person.title == "Sales Director"


# ─── What we would actually do about it ─────────────────────────────────────


def test_a_verified_address_is_recommended_for_email():
    result = score_person(
        person("B", "AI Engineering Manager", email="b@acme.com"),
        target_role="Machine Learning Engineer",
    )
    assert "email" in result.recommended_channel
    assert result.person.contactable_now


def test_an_unverified_address_is_never_recommended_for_automatic_sending():
    result = score_person(
        person("B", "AI Engineering Manager", email="b@acme.com", verified=False),
        target_role="Machine Learning Engineer",
    )
    assert "approval" in result.recommended_channel
    assert not result.person.contactable_now


def test_linkedin_alone_means_the_user_sends_it():
    """A LinkedIn profile is a reference, not a transport. This project does not send
    LinkedIn messages."""
    result = score_person(
        person("E", "VP Engineering", linkedin="https://linkedin.com/in/e"),
        target_role="Machine Learning Engineer",
    )
    assert "you send" in result.recommended_channel
    assert not result.person.contactable_now


def test_somebody_with_no_channel_is_not_recommended():
    result = score_person(
        person("X", "AI Engineering Manager"), target_role="Machine Learning Engineer"
    )
    assert result.recommended_channel == "none"
    assert not result.worth_contacting


def test_an_opt_out_ends_the_conversation():
    target = person("B", "AI Engineering Manager", email="b@acme.com")
    result = score_person(target, target_role="Machine Learning Engineer", opted_out=True)

    assert result.score == 0.0
    assert "Do not contact" in result.recommendation


def test_someone_already_contacted_is_held_back_rather_than_re_ranked_to_the_top():
    target = person("B", "AI Engineering Manager", email="b@acme.com")
    fresh = score_person(target, target_role="Machine Learning Engineer")
    again = score_person(target, target_role="Machine Learning Engineer", previously_contacted=True)

    assert again.score < fresh.score
    assert "Already contacted" in again.recommendation


def test_someone_who_replied_is_a_conversation_not_a_new_message():
    result = score_person(
        person("B", "AI Engineering Manager", email="b@acme.com"),
        target_role="Machine Learning Engineer",
        replied=True,
    )
    assert "existing thread" in result.recommended_channel


# ─── Unknown is not zero, here too ──────────────────────────────────────────


def test_a_person_with_no_title_is_unknown_not_irrelevant():
    result = score_person(
        person("X", "", email="x@acme.com"), target_role="Machine Learning Engineer"
    )

    assert "function" in result.scored.unknown_fields
    assert "authority" in result.scored.unknown_fields
    assert result.scored.confidence < 1.0


def test_the_list_is_capped_because_fifty_names_are_not_an_answer():
    many = [person(f"P{n}", "AI Engineering Manager", email=f"p{n}@acme.com") for n in range(40)]
    assert len(_ml_rank(many)) <= 12


# ─── Is this the same human being ───────────────────────────────────────────


def test_two_records_with_the_same_linkedin_are_one_person():
    a = person("Rahul Kumar", "Engineer", linkedin="https://linkedin.com/in/rahul-kumar")
    b = person("Rahul K.", "Senior Engineer", linkedin="https://linkedin.com/in/rahul-kumar/")
    assert same_person(a, b)


def test_two_records_with_the_same_work_email_are_one_person():
    a = person("Rahul Kumar", email="r.kumar@acme.com")
    b = person("R Kumar", email="r.kumar@acme.com")
    assert same_person(a, b)


def test_a_matching_name_alone_is_not_enough():
    """Two people called Rahul Kumar can work at the same company, and a wrong merge means
    a message addressed to one describing the other's job."""
    a = person("Rahul Kumar", "Engineering Manager")
    b = person("Rahul Kumar", "Finance Analyst")
    assert not same_person(a, b)


def test_a_matching_name_with_an_agreeing_title_is_enough():
    a = person("Rahul Kumar", "Engineering Manager")
    b = person("Rahul Kumar", "Senior Engineering Manager")
    assert same_person(a, b)


def test_the_same_name_at_different_companies_is_not_one_person():
    a = person("Rahul Kumar", "Engineering Manager", company="Acme")
    b = person("Rahul Kumar", "Engineering Manager", company="Beta")
    assert not same_person(a, b)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Dr. Rahul Kumar", "rahul kumar"),
        ("RAHUL KUMAR", "rahul kumar"),
        ("Rahul  Kumar Jr.", "rahul kumar"),
        ("Mr Rahul Kumar", "rahul kumar"),
    ],
)
def test_a_name_normalises_for_comparison_only(raw, expected):
    assert normalize_name(raw) == expected


def test_merging_keeps_the_better_sourced_detail_and_fills_the_gaps():
    primary = person("Rahul Kumar", "Engineering Manager", email="r@acme.com")
    other = Person(
        name="Rahul Kumar",
        company="Acme",
        department="AI Platform",
        location="Bangalore",
        linkedin=ContactDetail(
            value="https://linkedin.com/in/rahul",
            kind=SourceKind.TEAM_PAGE,
            source_url=TEAM_PAGE,
            verification=Verification.VERIFIED,
        ),
    )

    merged = merge(primary, other)

    assert merged.email is not None and merged.email.value == "r@acme.com"
    assert merged.linkedin is not None
    assert merged.department == "AI Platform"
    assert merged.location == "Bangalore"


# ─── Reading a team page ────────────────────────────────────────────────────
#
# Measured against five real company sites, an earlier version of this parser returned
# "Zoho Expense", "Agent Mode", "Partner Awards" and "Postman MCP Server" as people. A
# person who does not exist is worse than a person not found: they go into a contact list
# and somebody writes to them. These tests encode each rule that closed one of those holes.


TEAM_HTML = """
<nav><a href="/about">About Us</a><a href="/contact">Contact Us</a></nav>
<div class="team">
  <div class="card"><h3>Priya Sharma</h3><p>VP Engineering</p>
    <a href="https://linkedin.com/in/priya-sharma">LinkedIn</a>
    <a href="mailto:priya@acme.com">email</a></div>
  <div class="card"><h3>Arjun Mehta</h3><p>Head of Machine Learning</p>
    <a href="https://linkedin.com/in/arjun-mehta">in</a></div>
  <div class="card"><h3>Ravi Nair</h3><p>Chief Financial Officer</p></div>
  <div class="card"><h3>Read More</h3><p>Learn More</p></div>
</div>
<footer>info@acme.com | Privacy Policy | All Rights Reserved</footer>
"""


def _extract(html=TEAM_HTML, company="Acme"):
    return extract_people(
        html, company=company, source_url="https://acme.com/team", kind=SourceKind.TEAM_PAGE
    )


def test_a_team_page_yields_its_people_with_the_right_titles():
    found = {p.name: p.title for p in _extract()}

    assert found["Priya Sharma"] == "VP Engineering"
    assert found["Arjun Mehta"] == "Head of Machine Learning"
    assert found["Ravi Nair"] == "Chief Financial Officer"


def test_navigation_and_footer_text_is_not_a_person():
    names = {p.name for p in _extract()}
    for phrase in ("Read More", "About Us", "Contact Us", "Privacy Policy", "All Rights"):
        assert phrase not in names


@pytest.mark.parametrize(
    "phrase",
    ["Zoho Expense", "Agent Mode", "Partner Awards", "Postman MCP Server", "Industry Leadership"],
)
def test_a_product_or_marketing_name_is_not_a_person(phrase):
    """Every one of these was returned as a person by an earlier version."""
    assert not looks_like_a_name(phrase)


def test_the_companys_own_name_is_not_a_person():
    assert not looks_like_a_name("Acme Analytics", company="Acme")


@pytest.mark.parametrize("phrase", ["AI Agents Engineered", "Automate API workflows", "Read More"])
def test_a_headline_that_mentions_the_field_is_not_a_job_title(phrase):
    """ "AI Agents Engineered" and "Head of AI" both contain "ai". Only one is a job."""
    assert not looks_like_a_title(phrase)


@pytest.mark.parametrize(
    "title",
    ["VP Engineering", "Head of Machine Learning", "Technical Recruiter", "Data Science Lead"],
)
def test_a_real_job_title_is_recognised(title):
    assert looks_like_a_title(title)


def test_a_title_belongs_to_one_person():
    """Without this, a card whose own title is unrecognised reaches backwards and steals
    its neighbour's — which put one title on three different people."""
    titles = [p.title for p in _extract()]
    assert len(titles) == len(set(titles))


def test_only_an_email_that_matches_the_name_is_attached():
    """`info@acme.com` in the footer is the company's switchboard. Attributing it to a
    person would turn a real address into a false claim about who reads it."""
    people = {p.name: p for p in _extract()}

    assert people["Priya Sharma"].email is not None
    assert people["Priya Sharma"].email.value == "priya@acme.com"
    assert people["Ravi Nair"].email is None


def test_a_person_from_a_page_carries_the_page_they_came_from():
    person = _extract()[0]
    assert person.sources
    assert person.sources[0]["url"] == "https://acme.com/team"
    if person.email:
        assert person.email.source_url == "https://acme.com/team"


def test_corroboration_is_recorded_rather_than_required():
    """Measured on real sites: requiring a LinkedIn URL or matching email found nobody,
    because the pages that name people do not link their profiles. So an uncorroborated
    person is kept and marked, not dropped."""
    people = {p.name: p for p in _extract()}

    assert people["Priya Sharma"].corroborated
    assert not people["Ravi Nair"].corroborated
    assert not people["Ravi Nair"].contactable_now, "and is never contacted automatically"


def test_requiring_corroboration_drops_the_uncorroborated():
    strict = extract_people(
        TEAM_HTML,
        company="Acme",
        source_url="https://acme.com/team",
        kind=SourceKind.TEAM_PAGE,
        require_corroboration=True,
    )
    assert "Ravi Nair" not in {p.name for p in strict}


@pytest.mark.parametrize(
    ("title", "elsewhere"),
    [
        ("President & CEO, Employers' Forum of Indiana", True),
        ("VP of Engineering, Acme", False),
        ("VP of Engineering", False),
        ("Engineering Manager at Beta Corp", True),
    ],
)
def test_a_testimonial_names_another_employer(title, elsewhere):
    """Quantiphi's leadership page carries a customer quote — a real person at a real
    company, neither of which is Quantiphi."""
    assert _names_another_employer(title, "Acme") is elsewhere


def test_html_entities_are_decoded():
    """A partial replacement table left "Employers&#x27; Forum" inside a title."""
    assert _blocks("<p>Employers&#x27; Forum &amp; Co</p>") == ["Employers' Forum & Co"]


def test_an_empty_page_yields_nobody_rather_than_noise():
    assert _extract("<html><body><p>Nothing here.</p></body></html>") == []


# ─── Finding the page, rather than guessing at it ───────────────────────────
#
# Measured across five real company sites: every guessed path either 404'd or silently
# redirected to the homepage, and the crawler then parsed the homepage and reported its
# marketing copy as staff. Reading the site's own anchors found nothing either, because
# modern navigation is a JavaScript dropdown whose links are `href="#"`.
#
# Asking a search provider works, because search engines have already rendered these sites:
# it returns `quantiphi.com/company/executive-team` and `razorpay.com/newsroom/leadership-team`,
# neither of which any path list would produce. That took discovery from 2 people across 8
# companies to 15 across 6.


def test_a_redirect_to_the_homepage_is_not_the_page_we_asked_for():
    """The bug that made a crawler report marketing copy as staff: `/leadership` does not
    exist at Quantiphi and 302s to `/`, which answers 200 with text/html."""
    assert not _same_page("https://quantiphi.com/", "https://quantiphi.com/leadership")


@pytest.mark.parametrize(
    ("landed", "asked"),
    [
        ("https://acme.com/team", "https://acme.com/team"),
        ("https://acme.com/team/", "https://acme.com/team"),
        ("https://www.acme.com/team", "http://acme.com/team"),
    ],
)
def test_a_harmless_redirect_is_still_the_same_page(landed, asked):
    """http to https, and a trailing slash, are not a different page."""
    assert _same_page(landed, asked)


def test_links_the_site_itself_calls_its_people_are_followed():
    html = """
      <nav>
        <a href="/company/executive-team">Leadership</a>
        <a href="/pricing">Pricing</a>
        <a href="/ebooks">Thought Leadership</a>
        <a href="https://twitter.com/acme">About us</a>
      </nav>
    """
    links = people_links(html, "https://acme.com")

    assert "https://acme.com/company/executive-team" in links
    assert not any("pricing" in link for link in links)
    assert not any("twitter" in link for link in links), "an off-site link is not their team"


def test_a_link_back_to_the_homepage_is_not_a_team_page():
    html = '<a href="/">About</a><a href="/leadership">Our team</a>'
    links = people_links(html, "https://acme.com")

    assert links == ["https://acme.com/leadership"]


def test_the_most_specific_link_is_tried_first():
    html = '<a href="/about">About</a><a href="/leadership">Leadership</a>'
    assert people_links(html, "https://acme.com")[0].endswith("/leadership")


@pytest.mark.parametrize("label", ["View More", "Show More", "Top Trending", "Press Releases"])
def test_interface_furniture_is_not_a_person(label):
    """Every one of these was extracted as a person from a real executive-team page."""
    assert not looks_like_a_name(label)


def test_one_person_written_two_ways_is_one_person():
    """Sigmoid's page lists Mayur Rustagi as both "CTO and Co-Founder" and "Chief
    Technology Officer and Co-Founder". String containment split him into two records."""
    a = person("Mayur Rustagi", "CTO and Co-Founder")
    b = person("Mayur Rustagi", "Chief Technology Officer and Co-Founder")

    assert same_person(a, b)


def test_the_same_name_in_genuinely_different_jobs_is_still_two_people():
    """The guard this must not break."""
    a = person("Rahul Kumar", "Engineering Manager")
    b = person("Rahul Kumar", "Chief Financial Officer")

    assert not same_person(a, b)


# ─── Short markers need a boundary at both ends ─────────────────────────────
#
# Found on live data: "ai" matched the start of "Aiven", so a title of "Chief Financial
# Officer, Aiven" read as an AI/ML role and Aiven's CFO scored 85/100 for a machine-learning
# candidate. The same class of bug as "cto" matching inside "director" — a leading boundary
# was added then, and this is the trailing half of the same fix.


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("Chief Financial Officer, Aiven", "finance"),
        ("VP Engineering, Aiven", "engineering"),
        ("Head of AI", "ai_ml"),
        ("AI Engineer", "ai_ml"),
        ("Senior AI/ML Lead", "ai_ml"),
    ],
)
def test_a_company_name_does_not_change_what_a_title_means(title, expected):
    assert function_of(title) == expected


@pytest.mark.parametrize(
    "marker_inside_a_word",
    ["Aiven", "Airtable", "Directory", "Octopus", "Vphase", "Chromatic"],
)
def test_a_short_marker_does_not_match_inside_a_longer_word(marker_inside_a_word):
    """ai, cto, vp, cro — each is a real marker and each hides inside a real word."""
    assert function_of(marker_inside_a_word) == ""


def test_a_prefix_marker_still_reaches_its_longer_forms():
    """The trailing boundary applies only to short markers, because several longer ones
    are deliberately prefixes."""
    assert function_of("Technical Recruiter") == "recruiting"
    assert function_of("Recruiting Manager") == "recruiting"
    assert function_of("Financial Controller") == "finance"
    assert function_of("Data Scientist") == "data"


@pytest.mark.parametrize(
    "title", ["Chief Strategy Officer", "Chief People Officer", "Chief of Staff"]
)
def test_a_job_title_is_never_stored_as_a_persons_name(title):
    """These were stored as employees of Freshworks. Their subject matter is in no function
    vocabulary, so the function test alone did not catch them."""
    assert not looks_like_a_name(title)


def test_a_real_name_still_reads_as_one():
    for name in ("Kenneth Chen", "Priya Sharma", "Mayur Rustagi", "R. Kumar"):
        assert looks_like_a_name(name), name


# ─── A heading is not a person ──────────────────────────────────────────────
#
# "Our Leaders" was stored as an employee of Kredx and "Additional Information" as an
# employee of Druva. Both are two capitalised words, both pass every structural test a name
# passes, and neither is anybody. The opening word is what gives them away.


@pytest.mark.parametrize(
    "heading",
    [
        "Our Leaders",
        "Additional Information",
        "The Team",
        "More Stories",
        "Other Resources",
        "Meet Priya",
        "Latest News",
        "Your Account",
    ],
)
def test_a_section_heading_is_not_a_person(heading):
    assert not looks_like_a_name(heading)


@pytest.mark.parametrize(
    "name",
    [
        "Kenneth Chen",
        "Mirza Aasim Baig",
        "Prakhyath Hegde",
        "Harshil Mathur",
        "Mayur Rustagi",
        "R. Kumar",
        "Anton B.",
    ],
)
def test_the_real_people_found_on_real_sites_still_read_as_names(name):
    """Every one of these is a genuine executive found on their own company page."""
    assert looks_like_a_name(name)
