"""The named traversals over people, outreach and outcomes — addendum §18.

These four questions are the ones a job hunt actually asks out loud, and each is tested
against the thing that would make it dangerous rather than merely wrong:

* **Uncontacted people** must never list somebody who asked not to be contacted. A stale
  suggestion wastes a minute; a suggestion to re-contact an opt-out is the one outreach
  mistake that cannot be walked back.
* **Outcome rates** must refuse to quote a percentage they cannot support. "100% response
  rate" from a single application is the weakest evidence in the system wearing the
  clothes of the strongest.

Rows are built directly rather than through the pipeline. The pipeline has its own tests;
what is under test here is the traversal, and a seeding bug that produced no people would
otherwise pass as "nobody to contact".
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from careeragent.knowledge import questions

pytestmark = pytest.mark.usefixtures("isolated_db")


async def _seed() -> str:
    """One matched company with two people, one unmatched company with one.

    Acme has an open role scored 0.80 and two staff; Quiet Ltd has a role scored 0.10 and a
    perfectly relevant person nobody should be told about, because there is nothing to write
    to them about.
    """
    from careeragent.storage import init_db, session_scope
    from careeragent.storage import repository as repo
    from careeragent.storage.models import Company, Job, JobScore, PersonRecord

    await init_db(drop=True)
    async with session_scope() as session:
        candidate = await repo.get_or_create_candidate(session)
        candidate_id = candidate.id

        session.add_all(
            [
                Company(
                    id="c-acme",
                    name="Acme",
                    normalized_name="acme",
                    domain="acme.example",
                ),
                Company(
                    id="c-quiet",
                    name="Quiet Ltd",
                    normalized_name="quiet ltd",
                    domain="quiet.example",
                ),
            ]
        )
        session.add_all(
            [
                Job(
                    id="j-acme",
                    company_id="c-acme",
                    company="Acme",
                    title="Machine Learning Engineer",
                    normalized_title="machine learning engineer",
                    description="Python, PyTorch and Kubernetes. " * 6,
                    status="open",
                ),
                Job(
                    id="j-quiet",
                    company_id="c-quiet",
                    company="Quiet Ltd",
                    title="Sales Lead",
                    normalized_title="sales lead",
                    description="Quota carrying enterprise sales. " * 6,
                    status="open",
                ),
            ]
        )
        session.add_all(
            [
                JobScore(job_id="j-acme", candidate_id=candidate_id, final_score=0.80),
                JobScore(job_id="j-quiet", candidate_id=candidate_id, final_score=0.10),
            ]
        )
        session.add_all(
            [
                PersonRecord(
                    id="p-hiring",
                    company_id="c-acme",
                    name="Ada Rao",
                    normalized_name="ada rao",
                    title="Engineering Manager, ML",
                    function="engineering",
                    hiring_authority=True,
                    linkedin_url="https://www.linkedin.com/in/ada-rao",
                    relevance=88,
                    recommended_channel="linkedin",
                    relevance_reasons=["hires for the function you are targeting"],
                ),
                PersonRecord(
                    id="p-optout",
                    company_id="c-acme",
                    name="Bo Lin",
                    normalized_name="bo lin",
                    title="Director of Engineering",
                    function="engineering",
                    hiring_authority=True,
                    email="bo@acme.example",
                    email_verification="verified",
                    relevance=80,
                    recommended_channel="email",
                    relevance_reasons=["senior in the function you are targeting"],
                ),
                PersonRecord(
                    id="p-quiet",
                    company_id="c-quiet",
                    name="Cy Mehta",
                    normalized_name="cy mehta",
                    title="VP Engineering",
                    function="engineering",
                    hiring_authority=True,
                    relevance=90,
                    relevance_reasons=["senior in the function you are targeting"],
                ),
            ]
        )
    return candidate_id


# ─── Who has not heard from me ──────────────────────────────────────────────


async def test_it_lists_relevant_people_only_where_there_is_a_role_to_write_about():
    """A 90-relevance VP at a company whose only opening scores 0.10 is not a lead.

    Relevance answers "would this person be the right one to ask"; it says nothing about
    whether there is anything to ask. Both have to be true.
    """
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    async with session_scope() as session:
        result = await questions.uncontacted_at_matched_companies(session, candidate_id)

    names = {p["name"] for p in result["people"]}
    assert names == {"Ada Rao", "Bo Lin"}
    assert "Cy Mehta" not in names


async def test_somebody_who_opted_out_is_dropped_rather_than_flagged():
    """The one outreach mistake that cannot be walked back.

    A suppressed person returned with `suppressed: true` is a row a client can render and
    somebody can click send on. So they do not come back at all, and the count of what was
    withheld is reported separately.
    """
    from careeragent.outreach import suppression
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    async with session_scope() as session:
        await suppression.suppress(
            session,
            reason=suppression.Reason.OPTED_OUT,
            email="bo@acme.example",
            person_id="p-optout",
            note="replied asking to stop",
        )

    async with session_scope() as session:
        result = await questions.uncontacted_at_matched_companies(session, candidate_id)

    assert {p["name"] for p in result["people"]} == {"Ada Rao"}
    assert result["suppressed_skipped"] == 1


async def test_a_sent_message_removes_the_person_from_the_list():
    """The whole question is the gap. Somebody who has heard from you is not in it."""
    from careeragent.storage import session_scope
    from careeragent.storage.models import Outreach, OutreachContact

    candidate_id = await _seed()
    async with session_scope() as session:
        session.add(
            OutreachContact(
                id="oc-1",
                company_id="c-acme",
                name="Bo Lin",
                address="bo@acme.example",
                source="https://acme.example/team",
            )
        )
        await session.flush()
        session.add(
            Outreach(
                id="o-1",
                candidate_id=candidate_id,
                company_id="c-acme",
                contact_id="oc-1",
                status="sent",
                sent_at=datetime.now(UTC),
                transport="gmail",
                transport_message_id="msg-1",
            )
        )

    async with session_scope() as session:
        result = await questions.uncontacted_at_matched_companies(session, candidate_id)

    assert {p["name"] for p in result["people"]} == {"Ada Rao"}


async def test_a_draft_does_not_count_as_having_written_to_someone():
    """A draft is a decision the sender has not finished making. The recipient has
    experienced nothing of it, so the gap is still open."""
    from careeragent.storage import session_scope
    from careeragent.storage.models import Outreach, OutreachContact

    candidate_id = await _seed()
    async with session_scope() as session:
        session.add(
            OutreachContact(
                id="oc-2",
                company_id="c-acme",
                name="Bo Lin",
                address="bo@acme.example",
                source="https://acme.example/team",
            )
        )
        await session.flush()
        session.add(
            Outreach(
                id="o-2",
                candidate_id=candidate_id,
                company_id="c-acme",
                contact_id="oc-2",
                status="drafted",
            )
        )

    async with session_scope() as session:
        result = await questions.uncontacted_at_matched_companies(session, candidate_id)

    assert "Bo Lin" in {p["name"] for p in result["people"]}


async def test_reachable_is_not_the_same_claim_as_contactable():
    """Ada has a LinkedIn profile and no address. She is reachable by hand and by nothing
    the system can send on its own, and the row has to be able to say so."""
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    async with session_scope() as session:
        result = await questions.uncontacted_at_matched_companies(session, candidate_id)

    ada = next(p for p in result["people"] if p["name"] == "Ada Rao")
    assert ada["reachable"] is True
    assert ada["channel"] == "linkedin"


async def test_no_matched_company_says_so_rather_than_returning_an_empty_list():
    """An empty list reads as "there is nobody to write to". The truth is "there is nothing
    to write to them about yet", which points at a different fix."""
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    async with session_scope() as session:
        result = await questions.uncontacted_at_matched_companies(
            session, candidate_id, min_job_score=0.99
        )

    assert result["people"] == []
    assert "open role" in result["note"]


# ─── Where my strongest skills lead ─────────────────────────────────────────


async def test_companies_are_ranked_by_how_many_of_my_skills_they_want():
    """One role wanting four of your skills beats nine roles wanting one."""
    from careeragent.models.candidate import EvidenceSource, Fact, FactKind
    from careeragent.storage import repository as repo
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    facts = [
        Fact(
            kind=FactKind.SKILL,
            label=label,
            strength=5,
            evidence_source=EvidenceSource.MASTER_RESUME,
            evidence_locator="cv.pdf#page=1",
        )
        for label in ("python", "pytorch", "kubernetes")
    ]
    async with session_scope() as session:
        await repo.replace_facts(session, candidate_id, facts)

    async with session_scope() as session:
        rows = await questions.companies_for_my_strongest_skills(session, candidate_id)

    assert rows, "the Acme role names three of the candidate's skills verbatim"
    top = rows[0]
    assert top["company"] == "Acme"
    assert set(top["skills_matched"]) == {"python", "pytorch", "kubernetes"}
    # The openings come back inside the company rather than as a second answer that could
    # drift away from the first.
    assert top["openings"][0]["title"] == "Machine Learning Engineer"
    assert set(top["openings"][0]["asks_for"]) == set(top["skills_matched"])


# ─── What actually worked ───────────────────────────────────────────────────


async def _application(candidate_id: str, app_id: str, status: str, render: str) -> None:
    """One application, against a job of its own.

    A job of its own because the schema enforces one application per job per candidate,
    which is correct — applying twice to the same opening is a bug, not a strategy.
    """
    from careeragent.storage import session_scope
    from careeragent.storage.models import Application, Job

    async with session_scope() as session:
        session.add(
            Job(
                id=f"j-{app_id}",
                company_id="c-acme",
                company="Acme",
                title=f"Role {app_id}",
                normalized_title=f"role {app_id}",
                status="open",
            )
        )
        # Flushed before the application that points at it. Foreign keys are enforced now
        # (`PRAGMA foreign_keys=ON`), and relying on the unit of work to order two
        # unrelated inserts correctly is relying on something nobody declared.
        await session.flush()
        session.add(
            Application(
                id=app_id,
                job_id=f"j-{app_id}",
                candidate_id=candidate_id,
                status=status,
                resume_render_id=render,
                submitted_at=datetime.now(UTC),
                final_score=0.8,
            )
        )


async def test_one_response_out_of_one_application_is_not_a_hundred_percent():
    """The number that would make the weakest evidence in the system look like the
    strongest. It is withheld, and the reason is stated."""
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    await _application(candidate_id, "a-1", "interview", "r-1")

    async with session_scope() as session:
        result = await questions.applications_that_went_somewhere(session, candidate_id)
        versions = await questions.resume_versions_by_outcome(session, candidate_id)

    assert result["responded"] == 1
    assert result["response_rate"] is None
    assert "Too few" in result["note"]

    assert versions["comparable"] == 0
    assert versions["versions"][0]["rate"] is None
    assert "too few" in versions["versions"][0]["note"]


async def test_a_rate_appears_once_there_is_enough_behind_it():
    """Five submissions is the floor, and at the floor the rate is real arithmetic."""
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    for i, status in enumerate(["interview", "rejected_by_company", "applied", "applied", "offer"]):
        await _application(candidate_id, f"a-{i}", status, "r-1")

    async with session_scope() as session:
        result = await questions.applications_that_went_somewhere(session, candidate_id)
        versions = await questions.resume_versions_by_outcome(session, candidate_id)

    assert result["submitted"] == 5
    assert result["responded"] == 3
    assert result["response_rate"] == 0.6
    # An offer and an interview; the rejection is a response but not an interview.
    assert result["interviews"] == 2
    assert versions["versions"][0]["rate"] == 0.6


async def test_a_rejection_counts_as_a_response():
    """It is not the outcome anybody wants, but it is evidence the application was read,
    which is exactly what silence is not."""
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    await _application(candidate_id, "a-r", "rejected_by_company", "r-1")

    async with session_scope() as session:
        result = await questions.applications_that_went_somewhere(session, candidate_id)

    assert result["responded"] == 1
    assert result["interviews"] == 0


async def test_an_unrated_resume_version_sorts_below_a_rated_one():
    """A version with no rate is not a version with a perfect one, and a list that puts it
    on top says otherwise to anyone reading quickly."""
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    for i, status in enumerate(["interview", "rejected_by_company", "applied", "applied", "offer"]):
        await _application(candidate_id, f"a-{i}", status, "r-good")
    await _application(candidate_id, "a-new", "applied", "r-new")

    async with session_scope() as session:
        versions = await questions.resume_versions_by_outcome(session, candidate_id)

    assert [v["resume_render_id"] for v in versions["versions"]] == ["r-good", "r-new"]
    assert versions["versions"][1]["rate"] is None


async def test_nothing_applied_to_yet_says_so():
    """An empty table is a state, not a zero percent response rate."""
    from careeragent.storage import session_scope

    candidate_id = await _seed()
    async with session_scope() as session:
        result = await questions.applications_that_went_somewhere(session, candidate_id)
        versions = await questions.resume_versions_by_outcome(session, candidate_id)

    assert result["response_rate"] is None
    assert result["submitted"] == 0
    assert versions["versions"] == []
